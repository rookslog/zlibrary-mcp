# FU-4 fixtures, checked 2026-10-28

`blue-partial.html` and `blue-complete.html` are offline-trimmed real captures:
- `/home/rookslog/workspace/lanes/zlib-w1023-S2/capture-fresh/trace-browser-1.html`
  SHA256 e36eabfef79a15a0cc4784b2fdb6589b1700ef02aeeeec4504fdabf07cc1c5cc.
- `/home/rookslog/workspace/lanes/zlib-w1023-S2/capture/capture-2s.html`
  SHA256 8340d551eb8ef01e672fa7b7279d3db2e28622148ffd683f7bdfad3c76772f7f.

Retained: real title/form markup, range heading, list wrappers, title anchors,
partial-match heading, footer presence/order. Removed: CSS/scripts, navigation,
images, descriptions, metadata and footer contents. BeautifulSoup reserializes
attributes. No result/count/loading state was invented. Exact list: 1/24 rows
in partial, 24/24 plus 7/7 partial matches in complete. All five named snapshots
were inspected; none contains a real no-files notice.

`no-results-synthetic.html` is SYNTHETIC, not a provider capture. Its no-files
block is rendered from `search.html:450-464` and the English messages at
`messages.po:5261-5266` in public source mirror revision
`d26b35249a5a5241dfe2ce512dbf0d6502999c5e`:
https://github.com/LilyLoops/annas-archive/blob/d26b35249a5a5241dfe2ce512dbf0d6502999c5e/allthethings/page/templates/page/search.html#L450
https://github.com/LilyLoops/annas-archive/blob/d26b35249a5a5241dfe2ce512dbf0d6502999c5e/allthethings/translations/en/LC_MESSAGES/messages.po#L5261
The outer form/footer are minimal synthetic wrappers matching the saved layout.
This older mirror supports the fixture's provenance, not current live markup.
Original source copies and SHA receipts are in the FU-4 lane's `evidence/`.

The existing test modules use `annas_browser_test_fixtures.read_browser_fixture`
to add explicitly SYNTHETIC completion metadata to older session fakes: the
trimmed two-row Kant pages announce 2 instead of 23; a missing footer is added.
The original capture files are unchanged. The new real partial fixture keeps
its contradictory 1-of-24 state to catch premature settlement.
