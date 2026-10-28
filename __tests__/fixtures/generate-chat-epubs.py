"""Authored, text-free EPUB fixtures; no third-party book content."""

from pathlib import Path
import sys
import zipfile

CONTAINER = """<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""
NAV = """<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<nav epub:type="landmarks"><ol><li><a href="c.xhtml">Ignore landmarks</a></li></ol></nav>
<nav epub:type="toc"><h1>Contents</h1><ol><li><span>Part <em>One</em></span><ol><li><a href="../c.xhtml#one"> Chapter 1 &amp; start </a></li><li><a href="../c.xhtml#two">Chapter 2</a></li></ol></li><li><a href="../c.xhtml#end">End</a></li></ol></nav></body></html>"""
NCX = """<?xml version="1.0"?><!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd"><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap><navPoint id="part" playOrder="1"><navLabel><text>Part One</text></navLabel><content src="../c.xhtml"/><navPoint id="one" playOrder="2"><navLabel><text>Chapter 1 &amp; start</text></navLabel><content src="../c.xhtml#one"/></navPoint><navPoint id="two" playOrder="3"><navLabel><text>Chapter 2</text></navLabel><content src="../c.xhtml#two"/></navPoint></navPoint><navPoint id="end" playOrder="4"><navLabel><text>End</text></navLabel><content src="../c.xhtml#end"/></navPoint></navMap></ncx>"""


def entries(version=3, both=False):
    items = '<item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>'
    if version == 3:
        items += '<item id="nav" href="nav/toc.xhtml" media-type="application/xhtml+xml" properties="scripted nav"/>'
    if version == 2 or both:
        items += '<item id="outline" href="outline/book.ncx" media-type="application/x-dtbncx+xml"/>'
    result = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": CONTAINER,
        "OPS/package.opf": f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}.0"><manifest>{items}</manifest><spine toc="outline"><itemref idref="c"/></spine></package>',
        "OPS/c.xhtml": '<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>',
    }
    if version == 3:
        result["OPS/nav/toc.xhtml"] = NAV
    if version == 2 or both:
        result["OPS/outline/book.ncx"] = NCX
    return result


def write(target, contents):
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    for version in (2, 3):
        write(root / f"epub{version}.epub", entries(version))
