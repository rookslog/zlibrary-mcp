"""
Unified filename generation utilities for Z-Library MCP.

This module provides consistent filename generation across download and RAG processing.
Format: {author-lastname}-{title-slug}-{book-id}.{ext}

Design Decisions:
- Use dashes instead of underscores (web-friendly, filesystem-safe)
- Lowercase slugs (consistent, predictable)
- Author lastname first (easier sorting/grouping)
- Book ID preserved for uniqueness
- Maximum filename length: 255 characters (filesystem limit)
"""

import re
import unicodedata
from pathlib import Path

from zlibrary.const import Extension


# Formats reachable through the multi-source adapters (LibGen, Anna's Archive)
# that Z-Library's own extension vocabulary does not carry.
_MULTI_SOURCE_EXTENSIONS = frozenset({"cbr", "cbz", "doc", "docx", "odt"})

# Single source of truth for the Z-Library half: a format the search layer can
# return must survive normalization, or the published artifact loses its suffix.
SAFE_DOCUMENT_EXTENSIONS = frozenset(
    {member.value.lower() for member in Extension} | _MULTI_SOURCE_EXTENSIONS
)


def normalize_document_extension(extension) -> str:
    """Return a supported, path-safe document extension or an empty string."""
    normalized = str(extension or "").strip().lstrip(".").lower()
    return normalized if normalized in SAFE_DOCUMENT_EXTENSIONS else ""


def to_camel_case(value: str, max_length: int = None) -> str:
    """
    Convert string to CamelCase (remove all hyphens, spaces, special chars).

    Examples:
        "Byung-Chul Han" → "ByungChulHan"
        "The Burnout Society" → "TheBurnoutSociety"
        "L'Être et le Néant" → "LEtreEtLeNeant"

    Args:
        value: String to convert
        max_length: Maximum length (None = no limit)

    Returns:
        CamelCase string
    """
    if not value or not str(value).strip():
        return "Unknown"

    value = str(value).strip()

    # Convert to ASCII (remove accents)
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")

    # Split on any non-alphanumeric character
    words = re.findall(r"[A-Za-z0-9]+", value)

    # Capitalize each word
    camel = "".join(word.capitalize() for word in words if word)

    # Apply length limit
    if max_length and len(camel) > max_length:
        camel = camel[:max_length]

    return camel if camel else "Unknown"


def slugify(value: str, allow_unicode: bool = False, max_length: int = None) -> str:
    """
    Convert string to filesystem-safe slug (lowercase-dashes).

    NOTE: This function is kept for backward compatibility but main
    filename generation now uses to_camel_case().

    Examples:
        "Byung-Chul Han" → "byung-chul-han"
        "The Burnout Society" → "the-burnout-society"

    Args:
        value: String to slugify
        allow_unicode: If True, preserve Unicode characters
        max_length: Maximum length for slug (None = no limit)

    Returns:
        Slugified string
    """
    value = str(value).lower().strip()

    if allow_unicode:
        value = unicodedata.normalize("NFKC", value)
        # Replace non-word chars with space
        value = re.sub(r"[^\w\s-]", " ", value)
    else:
        # Convert to ASCII
        value = unicodedata.normalize("NFKD", value)
        value = value.encode("ascii", "ignore").decode("ascii")
        # Replace non-alphanumeric with space
        value = re.sub(r"[^a-z0-9\s-]", " ", value)

    # Collapse whitespace to single dash
    value = re.sub(r"[\s_]+", "-", value)
    # Remove consecutive dashes
    value = re.sub(r"-+", "-", value)
    # Strip leading/trailing dashes
    value = value.strip("-")

    # Apply length limit
    if max_length and len(value) > max_length:
        value = value[:max_length].rstrip("-")

    return value if value else "unknown"


def format_author_camelcase(author_str: str) -> str:
    """
    Format author name to CamelCase LastNameFirstName format.

    Handles formats:
        "Byung-Chul Han" → "HanByungChul" (LastName first)
        "Han, Byung-Chul" → "HanByungChul" (already Last, First)
        "Agamben, Giorgio" → "AgambenGiorgio"
        "Plato" → "Plato"
        "Jean-Luc Nancy" → "NancyJeanLuc"

    Args:
        author_str: Author name in various formats

    Returns:
        CamelCase author name (LastNameFirstName)
    """
    if not author_str or not author_str.strip():
        return "UnknownAuthor"

    # Handle comma-separated format (Lastname, Firstname) → already in right order
    if "," in author_str:
        parts = [p.strip() for p in author_str.split(",")]
        # Keep as "Lastname Firstname"
        author_str = " ".join(parts)
    else:
        # Space-separated format (Firstname ... Lastname) → reorder to Lastname Firstname
        parts = author_str.strip().split()
        if len(parts) > 1:
            # Move last name to front
            lastname = parts[-1]
            firstnames = " ".join(parts[:-1])
            author_str = f"{lastname} {firstnames}"
        # If single word (like "Plato"), keep as is

    return to_camel_case(author_str, max_length=50)


def create_unified_filename(
    book_details: dict,
    extension: str = None,
    suffix: str = None,
    max_total_length: int = 200,
    year: str = "",
    language: str = "",
    publisher: str = "",
) -> str:
    """
    Create unified filename for download or RAG processing.

    Format: {AuthorCamelCase}_{TitleCamelCase}[_{Year}][_{Lang}]_{BookID}[.suffix].{ext}

    Disambiguation fields (year, language) are included only when non-empty.

    Examples:
        With disambiguation: "OrwellGeorge_1984_1949_en_12345.epub"
        Without: "OrwellGeorge_1984_12345.epub"
        RAG: "HanByungChul_TheBurnoutSociety_3505318.pdf.processed.markdown"

    Args:
        book_details: Dictionary with 'author', 'title', 'id', optionally 'extension'
        extension: File extension (overrides book_details['extension'])
        suffix: Additional suffix before extension (e.g., '.processed.markdown')
        max_total_length: Maximum filename length before extension
        year: Publication year for disambiguation (optional)
        language: Language code for disambiguation (optional)
        publisher: Publisher name for disambiguation (optional, reserved for future use)

    Returns:
        Filesystem-safe CamelCase filename with underscores
    """
    # Extract and process author(s)
    author_str = book_details.get("author", "")
    if not author_str:
        # Try 'authors' list as fallback
        authors_list = book_details.get("authors", [])
        if isinstance(authors_list, list) and authors_list:
            # Handle multiple authors: "Author1_Author2"
            if len(authors_list) > 1:
                author_parts = [
                    format_author_camelcase(a) for a in authors_list[:2]
                ]  # Max 2 authors
                author_camel = "_".join(author_parts)
            else:
                author_camel = format_author_camelcase(authors_list[0])
        else:
            author_camel = "UnknownAuthor"
    else:
        author_camel = format_author_camelcase(author_str)

    # Extract and process title
    title_str = book_details.get("title") or book_details.get("name", "")
    if not title_str:
        title_camel = "UntitledBook"
    else:
        title_camel = to_camel_case(title_str, max_length=100)

    # A source result may not carry a provider-local numeric ID. Its content
    # hash is the next-stablest identity and prevents equal metadata records
    # from overwriting one another.
    book_id = next(
        (
            str(identity).strip()
            for identity in (book_details.get("id"), book_details.get("md5"))
            if identity is not None and str(identity).strip()
        ),
        "NoID",
    )

    # Build disambiguation segments (only non-empty values)
    disambiguation_parts = []
    if year and str(year).strip():
        disambiguation_parts.append(str(year).strip())
    if language and str(language).strip():
        disambiguation_parts.append(str(language).strip().lower()[:5])

    # Construct base filename with underscores
    parts = [author_camel, title_camel] + disambiguation_parts + [book_id]
    base_name = "_".join(parts)

    # Truncate if necessary (preserve book ID)
    if len(base_name) > max_total_length:
        id_len = len(book_id)
        available_len = max_total_length - id_len - 1  # -1 for underscore before ID

        # Split available length between author and title (40/60 ratio)
        author_max = int(available_len * 0.4)
        title_max = available_len - author_max

        author_camel = author_camel[:author_max]
        title_camel = title_camel[:title_max]

        base_name = f"{author_camel}_{title_camel}_{book_id}"

    # Add extension first (if no suffix provided)
    if not suffix:
        normalized_extension = normalize_document_extension(extension)
        metadata_extension = normalize_document_extension(book_details.get("extension"))
        if normalized_extension:
            ext = normalized_extension.lower()
            filename = f"{base_name}.{ext}"
        elif metadata_extension:
            ext = metadata_extension.lower()
            filename = f"{base_name}.{ext}"
        else:
            filename = base_name
    else:
        # If suffix provided, assume it includes extension handling
        filename = f"{base_name}{suffix}"

    return filename


def create_metadata_filename(original_filename: str) -> str:
    """
    Create metadata sidecar filename from original filename.

    Examples:
        "han-burnout-society-3505318.pdf" → "han-burnout-society-3505318.pdf.metadata.json"
        "agamben-community-6035827.epub" → "agamben-community-6035827.epub.metadata.json"

    Args:
        original_filename: Original file or processed filename

    Returns:
        Metadata filename
    """
    # Remove any existing .processed.* suffix for metadata
    base = re.sub(r"\.processed\.\w+$", "", original_filename)

    # Get just the filename without directory
    base = Path(base).name

    return f"{base}.metadata.json"


def parse_filename(filename: str) -> dict:
    """
    Parse unified filename back into components.

    Examples:
        "han-burnout-society-3505318.pdf" → {
            'author_slug': 'han',
            'title_slug': 'burnout-society',
            'book_id': '3505318',
            'extension': 'pdf'
        }

    Args:
        filename: Unified format filename

    Returns:
        Dictionary with parsed components
    """
    # Remove directory if present
    filename = Path(filename).name

    # Handle multi-part extensions (e.g., .pdf.processed.markdown)
    # Look for .processed. pattern first
    if ".processed." in filename:
        # Split at .processed. to separate base from processed extension
        parts = filename.split(".processed.")
        base_with_first_ext = parts[0]  # e.g., "han-burnout-society-3505318.pdf"
        processed_ext = parts[1]  # e.g., "markdown"

        # Remove the original extension to get the true base
        base_parts = base_with_first_ext.rsplit(".", 1)
        base_name = base_parts[0]  # e.g., "han-burnout-society-3505318"
        # The extension is just the processed part
        extension = f"processed.{processed_ext}"
    else:
        # Simple case - single extension
        parts = filename.rsplit(".", 1)
        base_name = parts[0]
        extension = parts[1] if len(parts) > 1 else None

    # Split into components (Author_Title_ID format with underscores)
    # Find last underscore followed by digits (book ID)
    match = re.match(r"^(.+)_(\d+)$", base_name)

    if match:
        author_title = match.group(1)
        book_id = match.group(2)

        # Split author from title (first component is author)
        components = author_title.split("_", 1)
        author_camel = components[0] if components else "Unknown"
        title_camel = components[1] if len(components) > 1 else "Untitled"
    else:
        # Fallback if pattern doesn't match
        author_camel = "Unknown"
        title_camel = base_name
        book_id = "NoID"

    return {
        "author_camel": author_camel,
        "title_camel": title_camel,
        "book_id": book_id,
        "extension": extension,
    }
