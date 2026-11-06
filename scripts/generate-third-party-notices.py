#!/usr/bin/env python3
"""Render frozen dependency notices; --refresh reads installed metadata.

Refresh requires npm ci and uv sync --frozen. Conditional Python records absent
from this interpreter are read from hash-checked locked wheels. Normal generation
and --check are offline and use the retained metadata in scripts/notices/.
"""

import argparse
from email.parser import BytesParser
import hashlib
import importlib.metadata
import json
import io
from pathlib import Path
import re
import sys
import tempfile
import tarfile
import urllib.request
import zipfile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
INPUT = Path("scripts/notices/metadata.json")
OUTPUT = Path("THIRD-PARTY-NOTICES.md")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def runtime_records(lock):
    packages = [dict(p, version=p.get("version", "editable")) for p in lock["package"]]
    pending = [p for p in packages if p["name"] == "zlibrary-mcp"]
    found = {}
    while pending:
        package = pending.pop()
        key = package["name"], package["version"]
        if key in found:
            continue
        found[key] = package
        for dependency in package.get("dependencies", []):
            matches = [
                p
                for p in packages
                if p["name"] == dependency["name"]
                and (
                    not dependency.get("version")
                    or p["version"] == dependency["version"]
                )
            ]
            if not matches:
                raise ValueError(f"Unresolved lock dependency: {dependency}")
            pending.extend(matches)
    return [p for key, p in sorted(found.items()) if key[0] != "zlibrary-mcp"]


def is_notice(path):
    return bool(
        re.match(
            r"(?i)^(licen[cs]es?|copying|copyright|notice|authors)([._-]|$)",
            Path(path).name,
        )
    )


def read_text(path):
    return path.read_bytes().decode("utf-8")


def collect_node(root, lock):
    records = []
    for location, package in sorted(lock["packages"].items()):
        if not location or package.get("dev"):
            continue
        directory = root / location
        metadata = json.loads((directory / "package.json").read_bytes())
        if metadata["version"] != package["version"]:
            raise ValueError(f"Installed Node version differs from lock: {location}")
        notices = {
            str(p.relative_to(directory)): read_text(p)
            for p in sorted(directory.rglob("*"))
            if p.is_file()
            and is_notice(p)
            and "node_modules" not in p.relative_to(directory).parts
        }
        if not notices:
            for readme in sorted(directory.glob("README*")):
                content = read_text(readme)
                section = re.search(r"(?im)^#{1,3} licen[cs]e[^\r\n]*", content)
                if section:
                    notices[readme.name + " (licence section)"] = content[
                        section.start() :
                    ]
        if not notices:
            raise ValueError(f"Missing full notice: {location}")
        records.append(
            {
                "kind": "Node",
                "name": metadata["name"],
                "version": package["version"],
                "location": location,
                "license": metadata.get(
                    "license", package.get("license", "UNDECLARED")
                ),
                "notices": notices,
            }
        )
    return records


def locked_wheel(package):
    # Prefer a universal wheel, then a Linux x86_64 wheel. This supplies only
    # records excluded by the current interpreter, never substitutes versions.
    wheels = sorted(
        package.get("wheels", []),
        key=lambda w: (
            "none-any.whl" not in w["url"],
            "manylinux" not in w["url"],
            "x86_64" not in w["url"],
            w["url"],
        ),
    )
    if not wheels:
        raise ValueError(f"No locked wheel for {package['name']}")
    wheel = wheels[0]
    cache = Path(tempfile.gettempdir()) / "notice-wheels"
    cache.mkdir(exist_ok=True)
    dest = cache / wheel["url"].split("/")[-1]
    if not dest.exists():
        with urllib.request.urlopen(wheel["url"], timeout=90) as response:
            dest.write_bytes(response.read())
    if "sha256:" + sha(dest.read_bytes()) != wheel["hash"]:
        raise ValueError(f"Wheel hash mismatch: {dest.name}")
    with zipfile.ZipFile(dest) as archive:
        metadata = BytesParser().parsebytes(
            archive.read(
                next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
            )
        )
        notices = {
            n: archive.read(n).decode("utf-8")
            for n in sorted(archive.namelist())
            if not n.endswith("/") and is_notice(n)
        }
    return metadata, notices, {"url": wheel["url"], "sha256": wheel["hash"]}


def resource_notices(text):
    """Return original attribution/licence comments, never stylesheet code."""
    return [
        comment
        for comment in re.findall(r"<!--[\s\S]*?-->", text)
        if re.search(
            r"copyright|licen[cs]e|author|permission|warranty|all rights reserved|created by|original transform by",
            comment,
            re.I,
        )
    ]


def source_notices(raw, package):
    """Retain native c-ares notices from its hash-pinned parent sdist."""
    if "sha256:" + sha(raw) != package["sdist"]["hash"]:
        raise ValueError("Native source archive hash mismatch")
    notices = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            path = Path(member.name)
            if not member.isfile() or "deps/c-ares/" not in member.name:
                continue
            if is_notice(path) or any(is_notice(part) for part in path.parts[:-1]):
                notices["sdist/" + member.name] = (
                    archive.extractfile(member).read().decode("utf-8")
                )
    if not notices:
        raise ValueError("Missing bundled c-ares notices in locked source archive")
    return notices


def collect_python(root, lock):
    records = []
    installed = {
        re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d
        for d in importlib.metadata.distributions()
    }
    for package in runtime_records(lock):
        name, version = package["name"], package["version"]
        lock_version = version
        distribution = installed.get(name)
        if name == "zlibrary":
            metadata = (
                BytesParser().parsebytes(
                    (root / "zlibrary/src/zlibrary.egg-info/PKG-INFO").read_bytes()
                )
                if (root / "zlibrary/src/zlibrary.egg-info/PKG-INFO").exists()
                else distribution.metadata
            )
            notices = {"zlibrary/LICENSE": read_text(root / "zlibrary/LICENSE")}
            version = metadata["Version"]
            origin = {
                "source": "zlibrary/",
                "sha256": sha((root / "zlibrary/LICENSE").read_bytes()),
            }
        elif distribution and distribution.version == version:
            metadata = distribution.metadata
            notices = {
                str(p): read_text(Path(distribution.locate_file(p)))
                for p in sorted(distribution.files or [], key=str)
                if is_notice(p) and Path(distribution.locate_file(p)).is_file()
            }
            origin = {
                "source": "installed .dist-info and package notices",
                "metadata_sha256": sha(distribution.read_text("METADATA").encode()),
            }
            # Include resource notices at their original locators, without
            # pretending an attribution is a permission grant.
            if name == "lxml":
                base = Path(distribution.locate_file("lxml/isoschematron/resources"))
                for p in sorted(base.rglob("*")):
                    if p.is_file() and p.suffix in (".xsl", ".rng"):
                        for index, comment in enumerate(
                            resource_notices(read_text(p)), 1
                        ):
                            notices[
                                "lxml/isoschematron/resources/"
                                + str(p.relative_to(base))
                                + f" (notice comment {index})"
                            ] = comment
        else:
            metadata, notices, origin = locked_wheel(package)
        if name == "pycares":
            source = package["sdist"]
            cache = Path(tempfile.gettempdir()) / (name + "-" + version + ".tar.gz")
            if not cache.exists():
                with urllib.request.urlopen(source["url"], timeout=90) as response:
                    cache.write_bytes(response.read())
            notices.update(source_notices(cache.read_bytes(), package))
            origin["native_source"] = source
        if metadata["Version"] != version:
            raise ValueError(f"Python metadata version differs from lock: {name}")
        if not notices:
            # bs4 is an empty alias distribution; its wheel has no notice file.
            # Retain the entire metadata rather than inventing upstream text.
            if name != "bs4":
                raise ValueError(f"Missing full notice: {name}")
            notices = {"METADATA (no separate notice supplied upstream)": str(metadata)}
        license_name = (
            metadata.get("License-Expression")
            or metadata.get("License")
            or "; ".join(
                c
                for c in metadata.get_all("Classifier", [])
                if c.startswith("License ::")
            )
            or "UNDECLARED"
        )
        records.append(
            {
                "kind": "Python",
                "name": name,
                "version": version,
                "location": name,
                "lock_version": lock_version,
                "license": license_name,
                "origin": origin,
                "notices": notices,
            }
        )
    return records


def render(root):
    data = json.loads((root / INPUT).read_bytes())
    for name, digest in data["locks"].items():
        if sha((root / name).read_bytes()) != digest:
            raise ValueError(
                f"{name} lock drift: refresh metadata and regenerate notices"
            )
    node = json.loads((root / "package-lock.json").read_bytes())
    want_node = {
        (p, d["version"]) for p, d in node["packages"].items() if p and not d.get("dev")
    }
    want_python = {
        (p["name"], p["version"])
        for p in runtime_records(tomllib.loads(read_text(root / "uv.lock")))
    }
    for kind, expected in [("Node", want_node), ("Python", want_python)]:
        actual = {
            (p["location"], p.get("lock_version", p["version"]))
            for p in data["components"]
            if p["kind"] == kind
        }
        if actual != expected:
            raise ValueError(f"{kind} component coverage differs from lock")
    chunks = [
        "# Third-party notices\n\n",
        "Generated by `scripts/generate-third-party-notices.py`. Third-party components only.\n\n",
        "Scope: the locked production Node closure and runtime Python version union. "
        "npm installs dependency ranges separately; Python dependencies are installed separately. "
        "OS packages, the Node/Python interpreters and inherited container tools retain their own notices in the image. "
        "Metadata and original notice text are retained in `scripts/notices/metadata.json`; "
        "platform-specific native contents require the corresponding artifact inventory. "
        "The bs4 alias wheel supplies metadata but no separate full notice file.\n\n",
    ]
    for component in data["components"]:
        chunks += [
            f"## {component['kind']}: {component['name']} {component['version']}\n\n",
            f"Location: `{component['location']}`\n\n",
            f"Declared licence: {component['license']}\n\n",
        ]
        for filename, content in sorted(component["notices"].items()):
            # The bytes inside each fence are original: no trimming or newline
            # normalization, including CRLF. Pick a fence absent from the text.
            fence = "`" * max(
                3, 1 + max((len(x) for x in re.findall(r"`+", content)), default=0)
            )
            chunks += [
                f"### {filename}\n\n{fence}text\n",
                content,
                "" if content.endswith("\n") else "\n",
                f"{fence}\n\n",
            ]
    for supplement in json.loads(
        (root / "scripts/notices/supplements.json").read_bytes()
    ):
        if sha(supplement["text"].encode()) != supplement["sha256"]:
            raise ValueError("Supplement notice hash mismatch")
        chunks += [
            f"## Additional third-party text: {supplement['component']}\n\n",
            f"Licence: {supplement['license']}\n\nSource: {supplement['origin']}\n\n```text\n",
            supplement["text"],
            "\n```\n\n",
        ]
    return "".join(chunks).encode("utf-8")


def check_freeze(root):
    frozen = json.loads((root / "scripts/notices/license-freeze.json").read_bytes())
    for name, digest in frozen["files"].items():
        if sha((root / name).read_bytes()) != digest:
            raise ValueError(f"Protected licence/document changed: {name}")
    for name, pattern in [
        ("package.json", rb'"license"\s*:\s*"[^"]*"'),
        ("pyproject.toml", rb"^license\s*=.*$"),
    ]:
        field = re.search(pattern, (root / name).read_bytes(), re.M).group()
        if sha(field) != frozen["fields"][name]:
            raise ValueError(f"Protected licence field changed: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    check_freeze(ROOT)
    if args.refresh:
        data = {
            "locks": {
                name: sha((ROOT / name).read_bytes())
                for name in ["package-lock.json", "uv.lock", "zlibrary/setup.cfg"]
            },
            "components": collect_node(
                ROOT, json.loads((ROOT / "package-lock.json").read_bytes())
            )
            + collect_python(ROOT, tomllib.loads(read_text(ROOT / "uv.lock"))),
        }
        (ROOT / INPUT).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    expected = render(ROOT)
    if args.check:
        if (ROOT / OUTPUT).read_bytes() != expected:
            raise ValueError("THIRD-PARTY-NOTICES.md drift: regenerate notices")
    else:
        (ROOT / OUTPUT).write_bytes(expected)
    print(
        f"Notices {'checked' if args.check else 'generated'}: {len(expected)} bytes; licence freeze checked"
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
