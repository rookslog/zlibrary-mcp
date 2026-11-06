"""Packaging falsifiers: stale locks, missing notices, omitted transitive packages."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/generate-third-party-notices.py"


def module():
    assert SCRIPT.is_file(), "notice generator must exist"
    spec = importlib.util.spec_from_file_location("notices", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_locked_runtime_union_keeps_conditional_versions_and_excludes_dev():
    mod = module()
    lock = {
        "package": [
            {
                "name": "zlibrary-mcp",
                "version": "2",
                "dependencies": [{"name": "dep"}],
                "dev-dependencies": {"dev": [{"name": "test-only"}]},
            },
            {
                "name": "dep",
                "version": "1",
                "dependencies": [
                    {"name": "conditional", "version": "1"},
                    {"name": "conditional", "version": "2"},
                ],
            },
            {"name": "conditional", "version": "1"},
            {"name": "conditional", "version": "2"},
            {"name": "test-only", "version": "1"},
        ]
    }
    assert [(p["name"], p["version"]) for p in mod.runtime_records(lock)] == [
        ("conditional", "1"),
        ("conditional", "2"),
        ("dep", "1"),
    ]


def test_nested_notice_bytes_and_missing_notice_fail_closed(tmp_path):
    mod = module()
    directory = tmp_path / "node_modules/example"
    directory.mkdir(parents=True)
    (directory / "package.json").write_text(
        '{"name":"example","version":"1","license":"MIT"}'
    )
    (directory / "LICENSE").write_bytes(b"Original copyright\r\nPermission.\r\n")
    (directory / "vendor").mkdir()
    (directory / "vendor/NOTICE.txt").write_bytes(b"Additional bundled attribution\n")
    lock = {
        "packages": {
            "": {},
            "node_modules/example": {"version": "1", "license": "MIT"},
            "node_modules/test-only": {"version": "1", "dev": True},
        }
    }
    records = mod.collect_node(tmp_path, lock)
    assert len(records) == 1
    assert records[0]["notices"] == {
        "LICENSE": "Original copyright\r\nPermission.\r\n",
        "vendor/NOTICE.txt": "Additional bundled attribution\n",
    }
    (directory / "LICENSE").unlink()
    (directory / "vendor/NOTICE.txt").unlink()
    with pytest.raises(ValueError, match="notice"):
        mod.collect_node(tmp_path, lock)


def test_committed_notices_match_locks_and_preserved_licences():
    module()
    result = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(SCRIPT), "--check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_notice_output_and_lock_drift_are_rejected(tmp_path):
    mod = module()
    for name in ["package-lock.json", "uv.lock", "THIRD-PARTY-NOTICES.md"]:
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "scripts/notices").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/notices/metadata.json",
        tmp_path / "scripts/notices/metadata.json",
    )
    (tmp_path / "zlibrary").mkdir()
    shutil.copyfile(ROOT / "zlibrary/setup.cfg", tmp_path / "zlibrary/setup.cfg")
    shutil.copyfile(
        ROOT / "scripts/notices/supplements.json",
        tmp_path / "scripts/notices/supplements.json",
    )
    assert mod.render(tmp_path) == (tmp_path / "THIRD-PARTY-NOTICES.md").read_bytes()
    (tmp_path / "uv.lock").write_bytes((tmp_path / "uv.lock").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="lock"):
        mod.render(tmp_path)


def test_npm_payload_contains_unchanged_notices(tmp_path):
    module()
    packed = subprocess.run(
        ["npm", "pack", "--dry-run", "--json", "--ignore-scripts"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "npm_config_cache": str(tmp_path / "npm-cache")},
    )
    names = {p["path"] for p in json.loads(packed.stdout)[0]["files"]}
    assert "THIRD-PARTY-NOTICES.md" in names


def test_plural_license_inventory_includes_bundled_library_terms(tmp_path):
    mod = module()
    directory = tmp_path / "node_modules/native-wrapper"
    directory.mkdir(parents=True)
    (directory / "package.json").write_text(
        '{"name":"native-wrapper","version":"1","license":"BSD-3-Clause"}'
    )
    (directory / "LICENSE").write_bytes(b"Wrapper notice\n")
    (directory / "LICENSES.txt").write_bytes(
        b"Bundled library copyright\r\nPermission grant\r\n"
    )
    records = mod.collect_node(
        tmp_path, {"packages": {"node_modules/native-wrapper": {"version": "1"}}}
    )
    assert (
        records[0]["notices"]["LICENSES.txt"]
        == "Bundled library copyright\r\nPermission grant\r\n"
    )


def test_lxml_native_notice_is_shipped_verbatim():
    mod = module()
    import importlib.metadata

    distribution = importlib.metadata.distribution("lxml")
    source = next(
        Path(distribution.locate_file(p))
        for p in distribution.files
        if str(p).endswith("/LICENSES.txt")
    )
    assert source.read_bytes() in mod.render(ROOT)


def test_bundled_cares_notices_include_nested_spdx_text_and_check_hash():
    import hashlib
    import io
    import tarfile

    mod = module()
    assert hasattr(mod, "source_notices"), (
        "bundled native source notices must be collected"
    )
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, raw in [
            ("pycares/deps/c-ares/LICENSE.md", b"c-ares copyright\nPermission\n"),
            (
                "pycares/deps/c-ares/LICENSES/BSD-2-Clause.txt",
                b"Original BSD notice\r\n",
            ),
            ("pycares/deps/c-ares/src/ares.c", b"source code"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    raw = stream.getvalue()
    package = {"sdist": {"hash": "sha256:" + hashlib.sha256(raw).hexdigest()}}
    assert mod.source_notices(raw, package) == {
        "sdist/pycares/deps/c-ares/LICENSE.md": "c-ares copyright\nPermission\n",
        "sdist/pycares/deps/c-ares/LICENSES/BSD-2-Clause.txt": "Original BSD notice\r\n",
    }
    with pytest.raises(ValueError, match="hash"):
        mod.source_notices(raw + b"tamper", package)


@pytest.mark.parametrize(
    "filename",
    ["LICENSE", "zlibrary/LICENSE", "README.md", "package.json", "pyproject.toml"],
)
def test_licence_freeze_rejects_each_protected_surface(tmp_path, filename):
    mod = module()
    baseline = ROOT / "scripts/notices/license-freeze.json"
    frozen = json.loads(baseline.read_bytes())
    for name in list(frozen["files"]) + [
        "package.json",
        "pyproject.toml",
        "scripts/notices/license-freeze.json",
    ]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    mod.check_freeze(tmp_path)
    target = tmp_path / filename
    raw = target.read_bytes()
    if filename == "package.json":
        raw = raw.replace(b'"license": "MIT"', b'"license": "Changed"')
    elif filename == "pyproject.toml":
        raw = raw.replace(
            b'license = { text = "MIT" }', b'license = { text = "Changed" }'
        )
    else:
        raw += b"\nchanged\n"
    target.write_bytes(raw)
    with pytest.raises(ValueError, match="Protected licence"):
        mod.check_freeze(tmp_path)


def test_resource_notices_preserve_comments_without_copying_stylesheet_code():
    mod = module()
    assert hasattr(mod, "resource_notices"), (
        "resource notices must exclude executable stylesheet bodies"
    )
    text = "<xsl:stylesheet><!-- Copyright A\r\nPermission granted.\r\n--><xsl:template/><!-- implementation note --></xsl:stylesheet>"
    assert mod.resource_notices(text) == [
        "<!-- Copyright A\r\nPermission granted.\r\n-->"
    ]


@pytest.mark.parametrize("filename", ["RNG2Schtrn.xsl", "XSD2Schtrn.xsl"])
def test_named_lxml_resource_attribution_is_preserved(filename):
    import importlib.metadata
    import re

    mod = module()
    distribution = importlib.metadata.distribution("lxml")
    source = Path(
        distribution.locate_file("lxml/isoschematron/resources/xsl/" + filename)
    )
    original = source.read_bytes().decode("utf-8")
    header = re.search(r"<!--[\s\S]*?-->", original).group()
    assert header in mod.resource_notices(original)
    assert header.encode() in mod.render(ROOT)
