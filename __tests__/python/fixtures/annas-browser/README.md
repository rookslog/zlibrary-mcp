# Anna browser HTML fixtures

Copied byte for byte from the B4 lane context/fixtures on 2026-10-23.
The controlling BRIEF.md supplies the provenance; context/L5-RESULT.md documents
the real visible-Chrome search/detail/partner captures on annas-archive.gl.
The cold-human capture is the supplied manual DDoS-Guard page from 16:20:32 UTC.
The five original captures were copied without modification; this lane did not fetch them.

frankenstein-settled.html contains 50 actual result rows and 8 unique header links;
kant-settled.html contains 2 actual result rows and 8 unique header links.
Header links repeat and must not become search results. Both saved search pages
lack the older middle-dot metadata strips, so unavailable row fields stay empty.
The detail/partner captures test negative challenge classification and rejection
as search pages. Their public links are historical, not runtime test targets.

FU-1 adds `kant-manual-phrase-variant.html`: a deliberately derived test case,
not an additional live capture. It retains the saved Kant page structure and
changes the Critique of Pure Reason title to include "complete the manual check".
BeautifulSoup reserializes the HTML; the five original captures remain unchanged.

B5 download variants and their transformations are documented in `b5/README.md`.
They are derived offline, not additional live captures.

FU-4 adds [trimmed partial/complete search captures and a synthetic no-results
control](fu4/README.md). Older session fakes receive explicitly synthetic count
and footer metadata through `annas_browser_test_fixtures.py`; the original HTML
files above remain unchanged.
