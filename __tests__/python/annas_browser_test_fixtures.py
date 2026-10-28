"""Coherent completion metadata for the older, already-trimmed browser fakes.

Original capture files remain untouched. FU-4's partial/complete tests read
unamended real captures. These older session tests exercise other contracts.
"""


def read_browser_fixture(path):
    page = path.read_text()
    if path.name in (
        "kant-settled.html",
        "kant-manual-phrase-variant.html",
        "frankenstein-settled.html",
    ):
        # SYNTHETIC: the old Kant capture retained 2 of its advertised 23 rows.
        page = page.replace("Results 1-23 (23 total)", "Results 1-2 (2 total)")
        if "<footer" not in page:
            # SYNTHETIC trailing stream-completion marker; no new live capture.
            page = page.replace("</body>", "<footer></footer></body>")
    return page
