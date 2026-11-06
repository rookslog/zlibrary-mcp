# Synthetic no-results fixture

`no-results-synthetic.html` was copied byte for byte from commit `899debf`.
Its marker comes from Anna's published renderer `search.html:450-464` and
English `messages.po:5261-5266` at revision
`d26b35249a5a5241dfe2ce512dbf0d6502999c5e`, as identified in the lead's
CONTINUE3 authority packet. It is a synthetic fixture, not a saved live page.
We have not observed this marker on a live page.

The HTTP adapter recognizes the visible exact English marker inside the search
form, excluding links and record lists. Existing result rows take priority;
challenge pages and unknown rowless pages remain errors.
