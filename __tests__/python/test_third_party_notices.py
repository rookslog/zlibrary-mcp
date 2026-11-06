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


def test_notice_whitespace_passes_but_runtime_lock_drift_is_rejected(tmp_path):
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
    assert mod.render(tmp_path) == (tmp_path / "THIRD-PARTY-NOTICES.md").read_bytes()
    lock = (tmp_path / "uv.lock").read_text()
    (tmp_path / "uv.lock").write_text(lock.replace("sha256:", "sha256:changed", 1))
    with pytest.raises(
        ValueError,
        match="lock drift.*npm ci.*uv sync.*--refresh.*scripts/notices/README.md",
    ):
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
    package = {
        "name": "pycares",
        "sdist": {"hash": "sha256:" + hashlib.sha256(raw).hexdigest()},
    }
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
        "README.md",
        "package.json",
        "pyproject.toml",
        "scripts/notices/license-freeze.json",
        "package-lock.json",
        "zlibrary/setup.cfg",
        "zlibrary/pyproject.toml",
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
    elif filename == "README.md":
        raw = raw.replace(b"MIT", b"Changed")
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


@pytest.fixture
def frozen_checkout(tmp_path):
    frozen = json.loads((ROOT / "scripts/notices/license-freeze.json").read_bytes())
    for name in set(frozen["files"]) | {
        "README.md",
        "package.json",
        "pyproject.toml",
        "scripts/notices/license-freeze.json",
        "package-lock.json",
        "zlibrary/setup.cfg",
        "zlibrary/pyproject.toml",
    }:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_unrelated_readme_and_doc_edits_pass_freeze(frozen_checkout):
    mod = module()
    for filename in ["README.md", "docs/api.md"]:
        path = frozen_checkout / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b"Routine contributor explanation.\n\n"
            + (path.read_bytes() if path.exists() else b"")
        )
    mod.check_freeze(frozen_checkout)


@pytest.mark.parametrize("filename", ["package.json", "pyproject.toml"])
def test_missing_licence_fields_fail_clearly(frozen_checkout, filename):
    import re

    path = frozen_checkout / filename
    pattern = (
        rb'"license"\s*:\s*"[^"]*",?'
        if filename.endswith("json")
        else rb"^license\s*=.*$"
    )
    path.write_bytes(re.sub(pattern, b"", path.read_bytes(), flags=re.M))
    with pytest.raises(ValueError, match="Protected licence field missing"):
        module().check_freeze(frozen_checkout)


def test_new_project_licence_classifier_is_rejected(frozen_checkout):
    path = frozen_checkout / "pyproject.toml"
    path.write_text(
        path.read_text().replace(
            'license = { text = "MIT" }',
            'license = { text = "MIT" }\nclassifiers = ["License :: OSI Approved :: MIT License"]',
            1,
        )
    )
    with pytest.raises(ValueError, match="Protected licence classifiers"):
        module().check_freeze(frozen_checkout)


def notice_checkout(tmp_path):
    for name in [
        "package-lock.json",
        "uv.lock",
        "zlibrary/setup.cfg",
        "scripts/notices/metadata.json",
        "scripts/notices/supplements.json",
    ]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_dev_bumps_root_versions_and_setup_prose_do_not_stale_notices(tmp_path):
    mod = module()
    root = notice_checkout(tmp_path)
    expected = mod.render(root)
    lock = json.loads((root / "package-lock.json").read_bytes())
    lock["version"] = lock["packages"][""]["version"] = "99.0.0"
    lock["packages"]["node_modules/prettier"]["version"] = "99.0.0"
    lock["packages"]["node_modules/prettier"]["integrity"] = "dev-only"
    (root / "package-lock.json").write_text(json.dumps(lock))
    path = root / "uv.lock"
    path.write_text(
        path.read_text().replace(
            'name = "pytest"\nversion = "', 'name = "pytest"\nversion = "99.', 1
        )
    )
    path = root / "zlibrary/setup.cfg"
    path.write_text(
        path.read_text().replace(
            "description = Unofficial Z-Library API",
            "description = Updated description",
        )
    )
    assert mod.render(root) == expected


@pytest.mark.parametrize(
    "target", ["node-integrity", "python-hash", "vendor-classifier"]
)
def test_production_notice_input_changes_need_actionable_refresh(tmp_path, target):
    mod = module()
    root = notice_checkout(tmp_path)
    if target == "node-integrity":
        path = root / "package-lock.json"
        lock = json.loads(path.read_bytes())
        lock["packages"]["node_modules/python-shell"]["integrity"] = "changed"
        path.write_text(json.dumps(lock))
    elif target == "python-hash":
        path = root / "uv.lock"
        path.write_text(path.read_text().replace("sha256:", "sha256:changed", 1))
    else:
        path = root / "zlibrary/setup.cfg"
        path.write_text(path.read_text().replace("(GPLv3)", "(Changed)"))
    with pytest.raises(
        ValueError,
        match="lock drift.*npm ci.*uv sync.*--refresh.*scripts/notices/README.md",
    ):
        mod.render(root)


def test_extras_requested_after_first_visit_are_traversed_and_cycles_terminate():
    lock = {
        "package": [
            {
                "name": "zlibrary-mcp",
                "dependencies": [{"name": "requester"}, {"name": "dep"}],
            },
            {
                "name": "requester",
                "version": "1",
                "dependencies": [{"name": "dep", "extra": ["socks"]}],
            },
            {
                "name": "dep",
                "version": "1",
                "optional-dependencies": {
                    "socks": [{"name": "socksio", "extra": ["nested"]}],
                    "unused": [{"name": "unused"}],
                },
            },
            {
                "name": "socksio",
                "version": "1",
                "optional-dependencies": {
                    "nested": [{"name": "dep", "extra": ["socks"]}]
                },
            },
            {"name": "unused", "version": "1"},
        ]
    }
    assert [p["name"] for p in module().runtime_records(lock)] == [
        "dep",
        "requester",
        "socksio",
    ]


def test_ujson_bundled_double_conversion_source_is_retained():
    import io
    import tarfile

    mod = module()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        raw = b"V8 project authors\r\nOriginal permission\r\n"
        info = tarfile.TarInfo("ujson/src/ujson/deps/double-conversion/LICENSE")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    raw = stream.getvalue()
    assert mod.source_notices(
        raw, {"name": "ujson", "sdist": {"hash": "sha256:" + mod.sha(raw)}}
    ) == {
        "sdist/ujson/src/ujson/deps/double-conversion/LICENSE": "V8 project authors\r\nOriginal permission\r\n"
    }


def test_unmapped_native_libraries_fail_refresh(monkeypatch, tmp_path):
    import importlib.metadata

    distribution = importlib.metadata.distribution("ujson")

    class WithUnknownLibrary:
        metadata = distribution.metadata
        version = distribution.version
        files = [*distribution.files, Path("ujson.libs/libunknown.so.1")]
        locate_file = distribution.locate_file
        read_text = distribution.read_text

    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda: [WithUnknownLibrary()]
    )
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "ujson"}]},
            {"name": "ujson", "version": distribution.version},
        ]
    }
    with pytest.raises(ValueError, match="Unmapped bundled native library.*libunknown"):
        module().collect_python(ROOT, lock)


def test_bs4_metadata_is_raw_upstream_text():
    import importlib.metadata

    distribution = importlib.metadata.distribution("bs4")
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "bs4"}]},
            {"name": "bs4", "version": distribution.version},
        ]
    }
    records = module().collect_python(ROOT, lock)
    assert list(records[0]["notices"].values()) == [distribution.read_text("METADATA")]


def test_vendored_declaration_uses_setup_classifier():
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "zlibrary"}]},
            {"name": "zlibrary", "source": {"editable": "zlibrary"}},
        ]
    }
    records = module().collect_python(ROOT, lock)
    assert (
        records[0]["license"]
        == "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"
    )


@pytest.mark.parametrize(
    "library", ["libstdc++-5d72f927.so.6.0.33", "libgcc_s-0cd532bd.so.1"]
)
def test_bundled_gcc_libraries_require_both_original_texts(library):
    mod = module()
    supplements = json.loads((ROOT / "scripts/notices/supplements.json").read_bytes())
    path = "ujson.libs/" + library
    mod.validate_native_notices([path], supplements)
    for missing in ["GCC runtime GPLv3", "GCC runtime exception"]:
        with pytest.raises(ValueError, match="Missing mapped native notice"):
            mod.validate_native_notices(
                [path], [s for s in supplements if s["component"] != missing]
            )


def test_native_supplements_are_rendered_without_text_changes():
    output = module().render(ROOT)
    supplements = json.loads((ROOT / "scripts/notices/supplements.json").read_bytes())
    by_name = {s["component"]: s for s in supplements}
    for name in [
        "libffi bundled in cffi",
        "GCC runtime GPLv3",
        "GCC runtime exception",
        "zlib bundled in lxml",
    ]:
        assert by_name[name]["text"].encode() in output


def test_bs4_metadata_from_locked_wheel_stays_raw_without_installed_distribution(
    monkeypatch, tmp_path
):
    import importlib.metadata
    import zipfile

    mod = module()
    cache = tmp_path / "notice-wheels"
    cache.mkdir()
    wheel = cache / "bs4.whl"
    raw = b"Metadata-Version: 2.1\r\nName: bs4\r\nVersion: 1\r\n\r\nUpstream description.\r\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("bs4-1.dist-info/METADATA", raw)
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [])
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "bs4"}]},
            {
                "name": "bs4",
                "version": "1",
                "wheels": [
                    {
                        "url": "https://unit.invalid/bs4.whl",
                        "hash": "sha256:" + mod.sha(wheel.read_bytes()),
                    }
                ],
            },
        ]
    }
    records = mod.collect_python(ROOT, lock)
    assert list(records[0]["notices"].values()) == [raw.decode()]


@pytest.mark.parametrize(
    "tag,reject",
    [
        ("cp314-cp314-musllinux_1_2_x86_64", True),
        ("cp310-abi3-musllinux_1_2_x86_64", True),
        ("cp313-cp313-musllinux_1_2_x86_64", False),
        ("cp314-cp314t-musllinux_1_2_x86_64", False),
        ("cp314-cp314-musllinux_1_2_armv7l", False),
        ("cp314-cp314-manylinux_2_28_x86_64", False),
    ],
)
def test_refresh_checks_image_wheel_from_lock_not_host_distribution(
    tmp_path, monkeypatch, tag, reject
):
    import importlib.metadata
    import zipfile

    mod = module()
    distribution = importlib.metadata.distribution("bs4")
    filename = f"bs4-{distribution.version}-{tag}.whl"
    cache = tmp_path / "notice-wheels"
    cache.mkdir()
    wheel = cache / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("bs4.libs/libnew.so", b"synthetic binary")
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("unit test must never fetch an artifact")

    monkeypatch.setattr(mod.urllib.request, "urlopen", forbidden)
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "bs4"}]},
            {
                "name": "bs4",
                "version": distribution.version,
                "wheels": [
                    {
                        "url": "https://files.pythonhosted.org/fixture/" + filename,
                        "hash": "sha256:" + mod.sha(wheel.read_bytes()),
                    }
                ],
            },
        ]
    }
    if reject:
        with pytest.raises(ValueError, match="Unmapped bundled native library.*libnew"):
            mod.collect_python(ROOT, lock)
    else:
        assert mod.collect_python(ROOT, lock)[0]["name"] == "bs4"


def test_image_wheel_hash_is_checked_before_inspecting_native_files(
    tmp_path, monkeypatch
):
    import importlib.metadata

    mod = module()
    distribution = importlib.metadata.distribution("bs4")
    filename = f"bs4-{distribution.version}-cp314-cp314-musllinux_1_2_x86_64.whl"
    cache = tmp_path / "notice-wheels"
    cache.mkdir()
    (cache / filename).write_bytes(b"tampered archive")
    monkeypatch.setattr(mod.tempfile, "gettempdir", lambda: str(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("unit test must never fetch an artifact")

    monkeypatch.setattr(mod.urllib.request, "urlopen", forbidden)
    lock = {
        "package": [
            {"name": "zlibrary-mcp", "dependencies": [{"name": "bs4"}]},
            {
                "name": "bs4",
                "version": distribution.version,
                "wheels": [
                    {
                        "url": "https://files.pythonhosted.org/fixture/" + filename,
                        "hash": "sha256:expected",
                    }
                ],
            },
        ]
    }
    with pytest.raises(ValueError, match="Wheel hash mismatch"):
        mod.collect_python(ROOT, lock)


@pytest.mark.parametrize(
    "target",
    [
        "root-license-files",
        "root-licenses",
        "vendor-setup-license",
        "vendor-project-license",
        "lock-license",
        "lock-entry-license",
        "new-copying",
    ],
)
def test_additional_public_licence_surfaces_are_protected(frozen_checkout, target):
    if target == "root-license-files":
        path = frozen_checkout / "pyproject.toml"
        path.write_text(
            path.read_text().replace(
                "[project]", '[project]\nlicense-files = ["COPYING"]', 1
            )
        )
    elif target == "root-licenses":
        path = frozen_checkout / "package.json"
        data = json.loads(path.read_bytes())
        data["licenses"] = [{"type": "Changed"}]
        path.write_text(json.dumps(data))
    elif target == "vendor-setup-license":
        path = frozen_checkout / "zlibrary/setup.cfg"
        path.write_text(
            path.read_text().replace("[metadata]", "[metadata]\nlicense = Changed", 1)
        )
    elif target == "vendor-project-license":
        path = frozen_checkout / "zlibrary/pyproject.toml"
        path.write_text(
            path.read_text().replace("[project]", '[project]\nlicense = "Changed"', 1)
        )
    elif target in {"lock-license", "lock-entry-license"}:
        path = frozen_checkout / "package-lock.json"
        data = json.loads(path.read_bytes())
        (data if target == "lock-license" else data["packages"][""])["license"] = (
            "Changed"
        )
        path.write_text(json.dumps(data))
    else:
        (frozen_checkout / "COPYING").write_text("New public licence text")
    with pytest.raises(ValueError, match="Protected licence"):
        module().check_freeze(frozen_checkout)


def test_unrelated_licence_words_outside_readme_licence_section_pass(frozen_checkout):
    path = frozen_checkout / "README.md"
    path.write_text(
        path.read_text().replace(
            "## License",
            "This tool does not bypass any licence restriction.\nSee dependency licences for upstream terms.\n\n## License",
            1,
        )
    )
    module().check_freeze(frozen_checkout)


def test_new_readme_licence_section_statement_is_protected(frozen_checkout):
    path = frozen_checkout / "README.md"
    path.write_text(path.read_text() + "\nAdditional project terms apply.\n")
    with pytest.raises(ValueError, match="Protected licence"):
        module().check_freeze(frozen_checkout)
