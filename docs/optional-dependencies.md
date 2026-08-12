# Optional Python Dependencies

The Python environment has three installation tiers. Search, metadata, and downloads
do not install the document-processing stack unless you request it.

| Tier | Command | Capabilities |
|---|---|---|
| Core | `uv sync --no-dev` | Search, metadata, and downloads across configured sources |
| RAG | `uv sync --extra rag` | Core plus PDF and EPUB text extraction with PyMuPDF and EbookLib |
| Scholar | `uv sync --extra scholar` | RAG plus footnote continuation, visual mark detection, and OCR support |

`scholar` is a superset of `rag`; installing both extras is unnecessary. Contributors
and CI can install every optional tier with `uv sync --all-extras` before running the
complete Python test suite.

The core bridge can start without either extra. Calling PDF or EPUB processing without
`rag` returns an error that names `uv sync --extra rag`. Scholar-only features degrade
when their dependencies are absent; install `scholar` when you need NLTK, OpenCV,
OCRmyPDF, Tesseract shims, or Pillow-backed rendering.

The extras install Python libraries only. OCR execution also requires the relevant
system tools, such as Tesseract and Ghostscript, to be available on `PATH`.
