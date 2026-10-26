"""Missing navigation, wrong hierarchy and unbounded ZIP/XML must be visible."""

import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from lib import chat_files

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "epub_fixture", ROOT / "__tests__/fixtures/generate-chat-epubs.py"
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
EXPECTED = [
    {
        "id": "1",
        "title": "Part One",
        "level": 1,
        "children": [
            {"id": "1.1", "title": "Chapter 1 & start", "level": 2, "children": []},
            {"id": "1.2", "title": "Chapter 2", "level": 2, "children": []},
        ],
    },
    {"id": "2", "title": "End", "level": 1, "children": []},
]


def read_entries(contents):
    stream = io.BytesIO()
    fixture.write(stream, contents)
    stream.seek(0)
    return chat_files.epub_sections(stream)


@pytest.mark.parametrize("version", [2, 3])
def test_authored_epubs_keep_hierarchy_without_pdf_fields(version):
    # Removing NCX fallback or flattening ol/navPoint nodes breaks this contract.
    with (ROOT / f"test_files/epub-nav/epub{version}.epub").open("rb") as stream:
        assert chat_files.epub_sections(stream) == EXPECTED


def test_epub3_toc_precedes_ncx_but_missing_toc_falls_back():
    contents = fixture.entries(3, both=True)
    contents["OPS/outline/book.ncx"] = fixture.NCX.replace("Part One", "NCX Part")
    assert read_entries(contents) == EXPECTED
    contents["OPS/nav/toc.xhtml"] = fixture.NAV.replace(
        'epub:type="toc"', 'epub:type="page-list"'
    )
    assert read_entries(contents)[0]["title"] == "NCX Part"


@pytest.mark.parametrize(
    "path", ["META-INF/container.xml", "OPS/package.opf", "OPS/nav/toc.xhtml"]
)
@pytest.mark.parametrize(
    "payload",
    [
        "<broken>",
        '<!DOCTYPE x [<!ENTITY a "expanded">]><x>&a;</x>',
        '<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>',
    ],
)
def test_bad_xml_and_entities_retain_empty_sections(path, payload):
    contents = fixture.entries()
    contents[path] = payload
    assert read_entries(contents) == []


def test_utf16_entity_declaration_is_rejected_before_expansion():
    contents = fixture.entries()
    contents["OPS/nav/toc.xhtml"] = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE html [<!ENTITY x "secret">]>'
        + fixture.NAV.replace("<!DOCTYPE html>", "").replace("Part <em>One</em>", "&x;")
    ).encode("utf-16")
    assert read_entries(contents) == []


@pytest.mark.parametrize(
    "href",
    [
        "../../../escape",
        "/etc/passwd",
        "https://example.invalid/nav",
        "//example.invalid/nav",
        "%2e%2e/%2e%2e/escape",
        "..\\escape",
        "C:/escape",
    ],
)
def test_manifest_targets_cannot_escape_archive(href):
    contents = fixture.entries()
    contents["OPS/package.opf"] = contents["OPS/package.opf"].replace(
        "nav/toc.xhtml", href
    )
    assert read_entries(contents) == []


@pytest.mark.parametrize(
    "member", ["../outside", "/outside", "OPS/../../outside", "OPS\\outside"]
)
def test_unsafe_zip_member_is_rejected_even_when_unused(member):
    contents = fixture.entries()
    contents[member] = "not extracted"
    assert read_entries(contents) == []


@pytest.mark.parametrize(
    "path", ["META-INF/container.xml", "OPS/package.opf", "OPS/nav/toc.xhtml"]
)
def test_missing_navigation_resources_return_empty_sections(path):
    contents = fixture.entries()
    del contents[path]
    assert read_entries(contents) == []


@pytest.mark.parametrize("version", [2, 3])
def test_deep_outline_returns_empty_without_partial_results(version):
    contents = fixture.entries(version)
    if version == 3:
        contents["OPS/nav/toc.xhtml"] = fixture.NAV.replace(
            "Chapter 2</a>",
            "Chapter 2</a>" + "<ol><li><span>Deep</span>" * 1000 + "</li></ol>" * 1000,
        )
    else:
        contents["OPS/outline/book.ncx"] = fixture.NCX.replace(
            "<navMap>",
            "<navMap>" + "<navPoint><navLabel><text>Deep</text></navLabel>" * 1000,
        ).replace("</navMap>", "</navPoint>" * 1000 + "</navMap>")
    assert read_entries(contents) == []


def test_oversized_xml_and_compression_bomb_are_not_parsed():
    contents = fixture.entries()
    contents["OPS/nav/toc.xhtml"] = fixture.NAV.replace(
        "Part <em>One</em>", "x" * (2 * 1024 * 1024)
    )
    assert read_entries(contents) == []


def test_excessive_member_count_and_duplicate_names_return_empty():
    contents = fixture.entries()
    contents.update({f"unused/{n}": "" for n in range(5000)})
    assert read_entries(contents) == []
    stream = io.BytesIO()
    fixture.write(stream, fixture.entries())
    with zipfile.ZipFile(stream, "a") as archive:
        with pytest.warns(UserWarning):
            archive.writestr("OPS/nav/toc.xhtml", fixture.NAV)
    stream.seek(0)
    assert chat_files.epub_sections(stream) == []


@pytest.mark.parametrize("data", [b"not a zip", b"PK\x03\x04broken"])
def test_invalid_zip_returns_empty(data):
    assert chat_files.epub_sections(io.BytesIO(data)) == []


def test_isolated_epub_command_uses_stdlib_without_site_packages():
    # -S removes site packages, so a stray fitz dependency makes this fail.
    with (ROOT / "test_files/epub-nav/epub3.epub").open("rb") as source:
        # Use a wrapper to lend fd 3 exactly as the Node bridge does.
        wrapper = 'import os,runpy,sys; os.dup2(int(sys.argv[1]),3); sys.argv=[sys.argv[2],"epub-map"]; runpy.run_path(sys.argv[0],run_name="__main__")'
        reply = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                wrapper,
                str(source.fileno()),
                str(ROOT / "lib/chat_files.py"),
            ],
            input="{}",
            text=True,
            capture_output=True,
            pass_fds=(source.fileno(),),
            timeout=5,
        )
    assert reply.returncode == 0
    import json

    assert json.loads(reply.stdout) == {"sections": EXPECTED}


def test_unsupported_zip_compression_is_not_used_for_xml():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_LZMA) as archive:
        for name, content in fixture.entries().items():
            archive.writestr(name, content)
    stream.seek(0)
    assert chat_files.epub_sections(stream) == []


def test_zip64_metadata_is_rejected_before_directory_allocation(monkeypatch):
    # ZIP64 can override ordinary end-record bounds, even without sentinel values.
    import struct

    stream = io.BytesIO()
    fixture.write(stream, fixture.entries())
    data = stream.getvalue()
    end = data[-22:]
    fields = struct.unpack("<4s4H2LH", end)
    record = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        fields[4],
        fields[4],
        fields[5],
        fields[6],
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(data) - 22, 1)
    data = data[:-22] + record + locator + end

    def unexpected_directory_allocation(*args, **kwargs):
        pytest.fail("ZIP64 bounds must be rejected before ZipFile allocates metadata")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_directory_allocation)
    assert chat_files.epub_sections(io.BytesIO(data)) == []


def test_corrupt_deflate_data_returns_empty_sections():
    import struct

    stream = io.BytesIO()
    fixture.write(stream, fixture.entries())
    with zipfile.ZipFile(stream) as archive:
        offset = archive.getinfo("META-INF/container.xml").header_offset
    data = bytearray(stream.getvalue())
    name_length, extra_length = struct.unpack_from("<2H", data, offset + 26)
    compressed = offset + 30 + name_length + extra_length
    data[compressed : compressed + 2] = b"\xff\xff"
    assert chat_files.epub_sections(io.BytesIO(data)) == []


def test_unknown_xml_encoding_returns_empty_sections():
    contents = fixture.entries()
    contents["OPS/nav/toc.xhtml"] = (
        b'<?xml version="1.0" encoding="not-a-real-codec"?><html/>'
    )
    assert read_entries(contents) == []
