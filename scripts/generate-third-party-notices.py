#!/usr/bin/env python3
"""Render frozen dependency notices; --refresh reads installed metadata.

Refresh requires npm ci and uv sync --frozen. Conditional Python records absent
from this interpreter are read from hash-checked locked wheels. Normal generation
and --check are offline and use the retained metadata in scripts/notices/.
"""

import argparse
from email.parser import BytesParser
import configparser
import fnmatch
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
    pending = [(p, ()) for p in packages if p["name"] == "zlibrary-mcp"]
    visited = set()
    found = {}
    while pending:
        package, extras = pending.pop()
        key = package["name"], package["version"]
        state = (*key, tuple(sorted(extras)))
        if state in visited:
            continue
        visited.add(state)
        found[key] = package
        dependencies = list(package.get("dependencies", []))
        for extra in extras:
            optional = package.get("optional-dependencies", {})
            if extra not in optional:
                raise ValueError(f"Unresolved lock extra: {key} [{extra}]")
            dependencies.extend(optional[extra])
        for dependency in dependencies:
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
            pending.extend((p, dependency.get("extra", ())) for p in matches)
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


# Bundled sources whose notices are absent from wheel metadata.
BUNDLED_SOURCES = {
    "pycares": "deps/c-ares/",
    "ujson": "src/ujson/deps/double-conversion/",
}
# Auditwheel libraries need explicitly retained terms. Unknown libraries fail
# refresh rather than silently inheriting the wrapper package's declaration.
BUNDLED_LIBRARIES = {
    "ujson.libs/libstdc++*.so*": ("GCC runtime GPLv3", "GCC runtime exception"),
    "ujson.libs/libgcc_s*.so*": ("GCC runtime GPLv3", "GCC runtime exception"),
}


def validate_native_notices(paths, supplements):
    available = {s["component"] for s in supplements}
    for path in paths:
        name = str(path)
        if not any(part.endswith(".libs") for part in Path(name).parts[:-1]):
            continue
        required = next(
            (
                texts
                for pattern, texts in BUNDLED_LIBRARIES.items()
                if fnmatch.fnmatchcase(name, pattern)
            ),
            None,
        )
        if required is None:
            raise ValueError(f"Unmapped bundled native library: {name}")
        if not set(required) <= available:
            raise ValueError(f"Missing mapped native notice: {name}")


def vendor_notice_fields(root):
    config = configparser.ConfigParser(interpolation=None)
    config.read(root / "zlibrary/setup.cfg", encoding="utf-8")
    metadata = config["metadata"]
    return {
        "name": metadata["name"],
        "version": metadata["version"],
        "classifiers": [
            line.strip()
            for line in metadata.get("classifiers", "").splitlines()
            if line.strip().startswith("License ::")
        ],
    }


def lock_digests(root):
    node = json.loads((root / "package-lock.json").read_bytes())
    python = runtime_records(tomllib.loads(read_text(root / "uv.lock")))
    records = {
        "package-lock.json": [
            (path, p["version"], p.get("integrity"))
            for path, p in sorted(node["packages"].items())
            if path and not p.get("dev")
        ],
        "uv.lock": [
            (
                p["name"],
                p["version"],
                sorted(
                    a["hash"]
                    for a in [
                        *p.get("wheels", []),
                        *([p["sdist"]] if p.get("sdist") else []),
                    ]
                ),
            )
            for p in python
        ],
        "zlibrary/setup.cfg": vendor_notice_fields(root),
    }
    return {
        name: sha(json.dumps(value, sort_keys=True).encode())
        for name, value in records.items()
    }


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


def cached_wheel(wheel):
    cache = Path(tempfile.gettempdir()) / "notice-wheels"
    cache.mkdir(exist_ok=True)
    dest = cache / wheel["url"].split("/")[-1]
    if not dest.exists():
        with urllib.request.urlopen(wheel["url"], timeout=90) as response:
            dest.write_bytes(response.read())
    if "sha256:" + sha(dest.read_bytes()) != wheel["hash"]:
        raise ValueError(f"Wheel hash mismatch: {dest.name}")
    return dest


def image_python(root):
    match = re.search(
        rb"\bUV_PYTHON=(\d+)\.(\d+)\b", (root / "docker/Dockerfile").read_bytes()
    )
    if match is None:
        raise ValueError("Cannot determine Docker CPython version from UV_PYTHON")
    return tuple(map(int, match.groups()))


def image_wheels(package, python):
    """Locked wheels compatible with the regular CPython linux/amd64 image.

    musllinux 1.x through 1.2, older abi3 and ABI-independent Python 3 tags
    are compatible. Free-threaded,
    debug, other-interpreter and other-architecture wheels are not selected.
    """
    target = "cp" + "".join(map(str, python))
    for wheel in package.get("wheels", []):
        filename = wheel["url"].rsplit("/", 1)[-1]
        if "musllinux" not in filename:
            continue
        parts = filename.removesuffix(".whl").rsplit("-", 3)
        if len(parts) != 4:
            raise ValueError(f"Invalid locked wheel filename: {filename}")
        _, interpreters, abis, platforms = parts
        abi_tags = set(abis.split("."))
        compatible = target in interpreters.split(".") and bool(
            {target, "none"} & abi_tags
        )
        if "none" in abi_tags:
            compatible = compatible or any(
                (match := re.fullmatch(r"py(\d)(\d*)", tag))
                and int(match[1]) == python[0]
                and (not match[2] or int(match[2]) <= python[1])
                for tag in interpreters.split(".")
            )
        if "abi3" in abi_tags:
            compatible = compatible or any(
                (match := re.fullmatch(r"cp(3)(\d+)", tag))
                and (int(match[1]), int(match[2])) <= python
                for tag in interpreters.split(".")
            )
        if not compatible:
            continue
        for platform in platforms.split("."):
            match = re.fullmatch(r"musllinux_(\d+)_(\d+)_x86_64", platform)
            if match and int(match[1]) == 1 and int(match[2]) <= 2:
                yield wheel
                break


def validate_image_wheels(package, python, supplements):
    checked = []
    for wheel in image_wheels(package, python):
        with zipfile.ZipFile(cached_wheel(wheel)) as archive:
            paths = [name for name in archive.namelist() if not name.endswith("/")]
            validate_native_notices(paths, supplements)
        checked.append(
            {
                "url": wheel["url"],
                "sha256": wheel["hash"],
                "native_files": [
                    name
                    for name in paths
                    if any(part.endswith(".libs") for part in Path(name).parts[:-1])
                ],
            }
        )
    return checked


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
    dest = cached_wheel(wheel)
    with zipfile.ZipFile(dest) as archive:
        validate_native_notices(
            [n for n in archive.namelist() if not n.endswith("/")],
            json.loads((ROOT / "scripts/notices/supplements.json").read_bytes()),
        )
        raw_metadata = archive.read(
            next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        )
        metadata = BytesParser().parsebytes(raw_metadata)
        notices = {
            n: archive.read(n).decode("utf-8")
            for n in sorted(archive.namelist())
            if not n.endswith("/") and is_notice(n)
        }
        if not notices and package["name"] == "bs4":
            notices = {
                "METADATA (no separate notice supplied upstream)": raw_metadata.decode(
                    "utf-8"
                )
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
    """Retain mapped native notices from a hash-pinned parent sdist."""
    prefix = BUNDLED_SOURCES[package["name"]]
    if "sha256:" + sha(raw) != package["sdist"]["hash"]:
        raise ValueError("Native source archive hash mismatch")
    notices = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            path = Path(member.name)
            if not member.isfile() or prefix not in member.name:
                continue
            if is_notice(path) or any(is_notice(part) for part in path.parts[:-1]):
                notices["sdist/" + member.name] = (
                    archive.extractfile(member).read().decode("utf-8")
                )
    if not notices:
        raise ValueError(
            f"Missing bundled {package['name']} notices in locked source archive"
        )
    return notices


def collect_python(root, lock):
    records = []
    supplements = json.loads((root / "scripts/notices/supplements.json").read_bytes())
    installed = {
        re.sub(r"[-_.]+", "-", d.metadata["Name"]).lower(): d
        for d in importlib.metadata.distributions()
    }
    python = image_python(root)
    for package in runtime_records(lock):
        image_artifacts = validate_image_wheels(package, python, supplements)
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
            validate_native_notices(distribution.files or [], supplements)
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
        if name in BUNDLED_SOURCES:
            source = package["sdist"]
            cache = Path(tempfile.gettempdir()) / (name + "-" + version + ".tar.gz")
            if not cache.exists():
                with urllib.request.urlopen(source["url"], timeout=90) as response:
                    cache.write_bytes(response.read())
            notices.update(source_notices(cache.read_bytes(), package))
            origin["native_source"] = source
        if image_artifacts:
            origin["image_wheels"] = image_artifacts
        if metadata["Version"] != version:
            raise ValueError(f"Python metadata version differs from lock: {name}")
        if not notices:
            # bs4 is an empty alias distribution; its wheel has no notice file.
            # Retain the entire metadata rather than inventing upstream text.
            if name != "bs4":
                raise ValueError(f"Missing full notice: {name}")
            notices = {
                "METADATA (no separate notice supplied upstream)": distribution.read_text(
                    "METADATA"
                )
            }
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
        if name == "zlibrary":
            license_name = (
                "; ".join(vendor_notice_fields(root)["classifiers"]) or "UNDECLARED"
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
    current = lock_digests(root)
    for name, digest in data["locks"].items():
        if current[name] != digest:
            raise ValueError(
                f"{name} lock drift: run `npm ci`, `uv sync --frozen --group dev`, "
                "then `uv run --frozen python scripts/generate-third-party-notices.py --refresh`; "
                "see scripts/notices/README.md"
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
        "The Alpine base image records OS package licence identifiers but does not include their licence texts. "
        "The CPython licence file in the image does not include the texts of its embedded libraries. "
        "Node and inherited container tools retain their own notice files. "
        "Metadata and original notice text are retained in `scripts/notices/metadata.json`; "
        "Native contents vary by platform. "
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


def readme_licence_text(root):
    raw = (root / "README.md").read_bytes()
    badge = re.findall(rb"(?im)^\[!\[Licen[cs]e:[^\r\n]*$", raw)
    section = re.search(rb"(?ims)^## licen[cs]e[ \t]*\r?\n.*?(?=^##[ \t]|\Z)", raw)
    if len(badge) != 1 or section is None:
        raise ValueError("Protected licence section or badge missing: README.md")
    return badge[0] + b"\n" + section.group()


def licence_metadata(root):
    def fields(data):
        return {
            key: value
            for key, value in data.items()
            if key.lower().replace("_", "-") in {"license", "licenses", "license-files"}
        }

    package = json.loads((root / "package.json").read_bytes())
    lock = json.loads((root / "package-lock.json").read_bytes())
    config = configparser.ConfigParser(interpolation=None)
    config.read(root / "zlibrary/setup.cfg", encoding="utf-8")
    result = {
        "package.json": fields(package),
        "package-lock.json": {
            "root": fields(lock),
            "package": fields(lock["packages"][""]),
        },
        "zlibrary/setup.cfg": {
            **fields(dict(config["metadata"])),
            "licence-classifiers": vendor_notice_fields(root)["classifiers"],
        },
    }
    for name in ["pyproject.toml", "zlibrary/pyproject.toml"]:
        project = tomllib.loads(read_text(root / name))["project"]
        result[name] = {
            **fields(project),
            "licence-classifiers": [
                c for c in project.get("classifiers", []) if c.startswith("License ::")
            ],
        }
    return {
        name: sha(json.dumps(value, sort_keys=True).encode())
        for name, value in result.items()
    }


def check_freeze(root):
    frozen = json.loads((root / "scripts/notices/license-freeze.json").read_bytes())
    for name, digest in frozen["files"].items():
        if sha((root / name).read_bytes()) != digest:
            raise ValueError(f"Protected licence/document changed: {name}")
    readme_lines = readme_licence_text(root)
    if sha(readme_lines) != frozen["readme_licence_lines"]:
        raise ValueError("Protected licence section or badge changed: README.md")
    project = tomllib.loads(read_text(root / "pyproject.toml"))["project"]
    classifiers = [
        c for c in project.get("classifiers", []) if c.startswith("License ::")
    ]
    if sha(json.dumps(classifiers).encode()) != frozen["project_licence_classifiers"]:
        raise ValueError("Protected licence classifiers changed: pyproject.toml")
    for name, pattern in [
        ("package.json", rb'"license"\s*:\s*"[^"]*"'),
        ("pyproject.toml", rb"^license\s*=.*$"),
    ]:
        match = re.search(pattern, (root / name).read_bytes(), re.M)
        if match is None:
            raise ValueError(f"Protected licence field missing: {name}")
        field = match.group()
        if sha(field) != frozen["fields"][name]:
            raise ValueError(f"Protected licence field changed: {name}")

    current = licence_metadata(root)
    for name, digest in frozen["metadata_fields"].items():
        if current[name] != digest:
            raise ValueError(f"Protected licence metadata changed: {name}")
    root_files = sorted(
        p.name
        for p in root.iterdir()
        if p.is_file() and re.match(r"(?i)^(licen[cs]e|copying)([._-]|$)", p.name)
    )
    if root_files != frozen["root_licence_files"]:
        raise ValueError("Protected licence file set changed at project root")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    check_freeze(ROOT)
    if args.refresh:
        data = {
            "locks": lock_digests(ROOT),
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
