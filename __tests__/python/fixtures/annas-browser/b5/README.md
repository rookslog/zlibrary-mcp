# B5 derived fixture variants

Generated offline from the supplied detail/partner captures by the lane's
`b5-make-fixtures.py`; these are test inputs, not additional live captures.
`detail-no-partner.html` removes slow-partner anchors.
`partner-countdown.html` removes file anchors and adds a visible wait message.
`partner-manual.html` replaces partner content with the supplied manual check.
Tests also derive hidden/signed/unsafe links, synthetic payload bytes, and MD5
substitutions in memory. Deadline and budget exhaustion use the real counter.
The five original captures remain byte-identical to their supplied files.
