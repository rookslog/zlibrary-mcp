# Authored EPUB navigation fixtures

These two tiny archives contain original test labels and an empty chapter, not copied
book content. Regenerate deterministically with:
`python3 __tests__/fixtures/generate-chat-epubs.py test_files/epub-nav`.
EPUB 3 exercises nested XHTML navigation; EPUB 2 exercises a nested NCX (Navigation
Control file for XML), selected by the package spine's `toc` identifier. Both resolve
through `META-INF/container.xml` and the package manifest, including subdirectories.

On 2026-10-23 the repository sample.epub and the four non-cache inbox EPUB paths were
inspected read-only for size, package version and rights metadata. No EPUB 3 public-domain
edition was identified; the EPUB 2 editions did not supply affirmative public-domain
rights metadata. The brief's minimal-fixture fallback was used, with no inbox copies.
