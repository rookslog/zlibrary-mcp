# Optional Python Dependencies

The Python environment has three installation tiers. Search, metadata, and downloads
do not install the document-processing stack unless you request it.

| Tier | Command | Capabilities |
|---|---|---|
| Core | `uv sync --no-dev` | Search, metadata, and downloads across configured sources |
| RAG | `uv sync --no-dev --extra rag` | Core plus PDF/EPUB extraction and NLTK-backed footnote detection |
| Scholar | `uv sync --no-dev --extra scholar` | RAG plus visual mark detection and OCR support |

`scholar` is a superset of `rag`; installing both extras is unnecessary. Contributors
and CI can install every optional tier with `uv sync --all-extras` before running the
complete Python test suite.

The core bridge can start without either extra. Calling PDF or EPUB processing without
`rag` returns an error that names `uv sync --no-dev --extra rag`. Scholar-only features
degrade when their dependencies are absent; install `scholar` when you need OpenCV,
OCRmyPDF, Tesseract shims, or Pillow-backed rendering.

The Docker image deliberately includes `rag`, so its PDF and EPUB processing works out
of the box. It does not install `scholar`: the image is Alpine-based, and the OpenCV
dependency used for X-mark detection has no compatible musl wheel. This does not change
the lightweight core default for source and npm installations.

NLTK-backed sentence boundaries are used when the tokenizer data is already installed.
The server never downloads NLTK data during import or document processing; when that
data is absent, footnote continuation uses a deterministic punctuation fallback.

The extras install Python libraries only. OCR execution also requires the relevant
system tools, such as Tesseract and Ghostscript, to be available on `PATH`.
