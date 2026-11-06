# Synthetic no-results fixture

The marker in `no-results-synthetic.html` comes from Anna's published renderer
`search.html:450-464` and English `messages.po:5261-5266` at revision
`d26b35249a5a5241dfe2ce512dbf0d6502999c5e`. It is a synthetic fixture, not a
saved live page.
We have not observed this marker on a live page.

The HTTP adapter recognizes the visible exact English marker inside the search
form, excluding links and record lists. Existing result rows take priority;
challenge pages and unknown rowless pages remain errors.
