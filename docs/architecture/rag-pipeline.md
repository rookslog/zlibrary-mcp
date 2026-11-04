# RAG Document Processing Pipeline Architecture (v2 - File Output)

> Historical processing design: text processing is [parked separately](../text-processing-migration.md).
> Current acquisition uses the configured output root; see [download-location migration](../interface-migration.md#download-location).

**Date:** 2025-04-23 (Updated from 2025-04-14)

## 1. Overview

This document outlines the architecture for the Retrieval-Augmented Generation (RAG) document processing pipeline within the `zlibrary-mcp` server. The goal is to enable AI agents to download documents (initially EPUB, TXT, PDF) from Z-Library, extract their text content, save it to a file, and use the file path for RAG workflows.

**Reason for Update (v2):** Original design returned raw text, causing agent instability with large documents. This version modifies the pipeline to save processed text to a file and return the file path instead.

## 2. Data Flow

Two primary workflows are supported:

**Workflow 1: Combined Download & Process** (Efficient for immediate use)

1.  **Agent:** Uses `search_books` (existing tool) to find a document and obtain the `bookDetails` object.
2.  **Agent:** Calls `download_book_to_file` (updated tool), specifying `book_id`, `outputDir` (optional), and setting `process_for_rag: true`. Can optionally specify `processed_output_format`.
3.  **Server (`zlibrary-mcp`):** Receives the request.
    *   Node.js layer calls the Python bridge.
    *   Python bridge downloads the file using `zlibrary` to the specified `outputDir` (or the configured output root).
    *   Python bridge immediately processes the downloaded file (EPUB/TXT/PDF) to extract text.
    *   Python bridge saves the extracted text to a designated file in `./processed_rag_output/`.
    *   Python bridge returns the original `file_path` and the new `processed_file_path` to Node.js.
4.  **Server (`zlibrary-mcp`):** Returns the `file_path` and `processed_file_path` to the agent.
5.  **Agent:** Uses the `processed_file_path` to access the text for its RAG process.

**Workflow 2: Separate Download & Process** (Flexible for existing files)

1.  **Agent:** Uses `search_books` (existing tool) to obtain the `bookDetails` object.
2.  **Agent:** Calls `download_book_to_file` (updated tool), specifying `book_id` and `outputDir` (optional) (leaving `process_for_rag` as `false` or omitting it).
3.  **Server (`zlibrary-mcp`):** Downloads the file via the Python bridge and returns only the `file_path` to the agent.
4.  **Agent:** (At a later time, or with an existing file) Calls `process_document_for_rag` (updated tool), providing the `file_path`. Can optionally specify `output_format`.
5.  **Server (`zlibrary-mcp`):** Receives the request.
    *   Node.js layer calls the Python bridge.
    *   Python bridge processes the specified file (EPUB/TXT/PDF) to extract text.
    *   Python bridge saves the extracted text to a designated file in `./processed_rag_output/`.
    *   Python bridge returns the `processed_file_path` to Node.js.
6.  **Server (`zlibrary-mcp`):** Returns the `processed_file_path` to the agent.
7.  **Agent:** Uses the `processed_file_path` to access the text for its RAG process.

## 3. Architecture Diagram (Updated)

```mermaid
graph TD
    subgraph Agent Workflow
        A[Agent: Search/Select Book] --> B1(Agent: Call download_book_to_file);
        B1 -- process_for_rag=true --> C{zlibrary-mcp Server};
        C -- Result: Orig Path & Processed Path --> E[Agent: Use Processed Path for RAG];

        A --> B2(Agent: Call download_book_to_file);
        B2 -- process_for_rag=false/omitted --> C;
        C -- Result: Orig File Path --> D[Agent: Call process_document_for_rag];
        D --> C;
        C -- Result: Processed File Path --> E;
    end

    subgraph "zlibrary-mcp Server (Node.js)"
        F[MCP Tool: download_book_to_file] --> G[lib/zlibrary-api.js: downloadBookToFile];
        G --> H(Python Bridge Call);
        I[MCP Tool: process_document_for_rag] --> J[lib/zlibrary-api.js: processDocumentForRag];
        J --> H;
        H -- Orig Path & Processed Path --> G; // Updated path for combined flow
        H -- Processed Path --> J; // Updated path for separate flow
    end

    subgraph "Python Bridge (python-bridge.py in Venv)"
        K[Handle download_book] --> L[zlibrary lib: Download];
        L --> M[Save Orig File to Disk];
        M -- If process_for_rag=true --> N[Call Handle process_document internally];
        N --> T[Save Processed Text to File];
        T --> U[Return Orig Path & Processed Path];

        O[Handle process_document] --> P{Detect File Type};
        P -- EPUB --> Q[Use ebooklib];
        P -- TXT --> R[Read Text File];
        P -- PDF --> R_PDF[Use PyMuPDF];
        Q --> S[Get Processed Text];
        R --> S;
        R_PDF --> S;
        S --> T;
        T --> V[Return Processed Path];
    end

    H -- download args (incl. process_flag) --> K;
    H -- process args --> O; // For separate tool call
    U -- orig_path, processed_path --> H;
    V -- processed_path --> H;

    style Agent Workflow fill:#f9f,stroke:#333,stroke-width:2px
    style "zlibrary-mcp Server (Node.js)" fill:#ccf,stroke:#333,stroke-width:2px
    style "Python Bridge (python-bridge.py in Venv)" fill:#cfc,stroke:#333,stroke-width:2px
```

## 4. Components

*   **Node.js Layer (`index.ts`, `lib/zlibrary-api.ts`):** Handles MCP tool requests, orchestrates calls to the Python bridge, manages communication with the agent.
*   **Python Bridge (`lib/python_bridge.py`):** Executes Python-specific tasks: interacting with the `zlibrary` library for downloads, performing document content extraction/processing, **saving processed text to a file**, and returning file paths. Runs within a managed virtual environment.

## 5. Processed File Handling

*   **Save Location:** Processed text files are saved in a dedicated directory relative to the workspace root: `./processed_rag_output/`. The Python bridge is responsible for creating this directory if it doesn't exist.
*   **Filename Convention:** The filename is derived from the original input file's name, with `.processed.<format_extension>` appended. Example: `my_book.epub` becomes `my_book.epub.processed.txt`.
*   **Output Format:** The default output format is plain text (`.txt`). This can be potentially configured via the `output_format` (for `process_document_for_rag`) or `processed_output_format` (for `download_book_to_file`) parameters in the tool inputs, although initially only `.txt` is supported.
*   **Error Handling:** If the Python bridge encounters an error while saving the processed file (e.g., permissions, disk space), it should raise a specific error (e.g., `FileSaveError`). The Node.js layer will catch this and return a structured error to the agent (e.g., `{ "error": "Failed to save processed RAG output", "details": "..." }`). The raw text content will *not* be returned in case of a save failure.

## 6. MCP Tools (Updated)

### `download_book_to_file` (Updated)

*   **Description:** Downloads a specific book file from Z-Library to a local path. Optionally processes the content for RAG simultaneously, saving the result to a separate file.
*   **Input:** `{ "bookDetails": "object (The full book details object obtained from search_books)", "outputDir": "string (optional, defaults to configured output root)", "process_for_rag": "boolean (optional, default: false)", "processed_output_format": "string (optional, default: 'txt')" }`
*   **Output:**
    *   If `process_for_rag` is `false` or omitted: `{ "file_path": "string" }` (Path to the original downloaded file)
    *   If `process_for_rag` is `true`: `{ "file_path": "string", "processed_file_path": "string" }` (Paths to original download and the processed text file)

### `process_document_for_rag` (Updated)

*   **Description:** Extracts and processes text content from a previously downloaded document file (EPUB, TXT, PDF), saving the result to a file for use in RAG workflows.
*   **Input:** `{ "file_path": "string", "output_format": "string (optional, default: 'txt')" }` (Absolute path to the document file)
*   **Output:** `{ "processed_file_path": "string" }` (Path to the file containing extracted and processed plain text content)

## 7. Technology Choice

*   **Python:** Used for content extraction, processing, and **saving** within the Python bridge.
*   **Rationale:** Leverages the existing bridge infrastructure and Python's strong ecosystem of libraries for handling various document formats (e.g., `ebooklib` for EPUB, `PyMuPDF` for PDF). Consolidates file I/O related to processing within the Python component.

## 8. Extensibility

The architecture remains extensible:
*   **New Formats:** Add support for new formats by installing necessary Python libraries and adding corresponding logic within the `process_document` function in `lib/python-bridge.py`.
*   **Processing Options:** Optional parameters could be added to control output format (e.g., Markdown via `output_format`) or chunking strategies, implemented within the Python bridge.