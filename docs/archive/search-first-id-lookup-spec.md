**Note:** This specification describes an internal "Search-First" strategy that was ultimately deemed unworkable due to external website limitations (ID search is non-functional). The `get_book_by_id` tool has been deprecated. See **[ADR-003: Handle Failure of `get_book_by_id` Tool](./adr/ADR-003-Handle-ID-Lookup-Failure.md)** for the final decision.

---
# Specification: Search-First Internal ID Lookup

## 1. Overview

**Context:** Existing ID-based lookups (`get_download_info`) are failing due to issues with the external `zlibrary` library and direct fetching (`/book/ID` results in 404).

**Strategy:** Implement a "Search-First" internal lookup mechanism within `lib/python_bridge.py`. This involves:
1.  Performing an internal general search using the book ID as the query string via `httpx`.
2.  Parsing the search results page HTML using `BeautifulSoup` to find the correct book detail page URL (which includes a slug).
3.  Fetching the book detail page using the obtained URL via `httpx`.
4.  Parsing the book detail page HTML using `BeautifulSoup` to extract metadata and download links.

**Known Risk:** This strategy relies on the website's general search functionality returning accurate results when queried with a book ID. Previous investigations ([2025-04-16 07:27:22]) indicated that search-by-ID might be unreliable or non-functional on the target website. If the search consistently fails to find the book by its ID, this strategy will not work, and `InternalBookNotFoundError` will be raised.

## 2. File Implementation

- **File:** `lib/python_bridge.py`

## 3. Dependencies

- `httpx`: For asynchronous HTTP requests. (Confirmed in `requirements.txt`)
- `beautifulsoup4`: For parsing HTML. (Confirmed in `requirements.txt`)
- `lxml`: Recommended parser for `beautifulsoup4`. (Confirmed in `requirements.txt`)
- `urllib.parse`: (Standard library) For joining URLs (`urljoin`).
- `logging`: (Standard library) For logging.
- `asyncio`: (Standard library) For running async functions.

## 4. Custom Exceptions

```python
# To be added in lib/python_bridge.py

class InternalBookNotFoundError(Exception):
    """Book not found via internal search or subsequent fetch."""
    pass

class InternalParsingError(Exception):
    """Error parsing search results or book detail page HTML."""
    pass

class InternalFetchError(Exception):
    """Error during HTTP request (network, non-200 status, timeout)."""
    pass
```

## 5. Function: `_internal_search`

**Signature:**

```python
async def _internal_search(query: str, domain: str, count: int = 1) -> list[dict]:
```

**Pseudocode:**

```python
# File: lib/python_bridge.py (Addition)
# Dependencies: httpx, bs4, logging, asyncio, urllib.parse

IMPORT httpx
IMPORT logging
IMPORT asyncio
FROM bs4 IMPORT BeautifulSoup
FROM urllib.parse IMPORT urljoin

# Assume logging is configured
# Assume InternalFetchError, InternalParsingError are defined

ASYNC FUNCTION _internal_search(query: str, domain: str, count: int = 1) -> list[dict]:
    """Performs an internal search and extracts book page URLs."""
    search_url = f"https://{domain}/s/{query}" # Example search URL pattern
    headers = { 'User-Agent': 'Mozilla/5.0 ...' } # Standard User-Agent
    timeout_seconds = 20

    LOG info f"Performing internal search for '{query}' at {search_url}"

    TRY
        ASYNC WITH httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            response = AWAIT client.get(search_url, headers=headers)

            IF response.status_code != 200 THEN
                LOG error f"Internal search failed for '{query}'. Status: {response.status_code}"
                RAISE InternalFetchError(f"Search request failed with status {response.status_code}")
            ENDIF

            # Parse HTML
            TRY
                soup = BeautifulSoup(response.text, 'lxml')
                results = []
                # Selector based on prompt - needs verification
                book_items = soup.select('#searchResultBox .book-item')

                IF NOT book_items THEN
                    LOG warning f"No book items found in search results for '{query}' using selector '#searchResultBox .book-item'."
                    RETURN [] # No results found is not necessarily an error here
                ENDIF

                FOR item IN book_items[:count]:
                    link_element = item.select_one('a[href]') # Find the main link
                    IF link_element AND link_element.has_attr('href'):
                        relative_url = link_element['href']
                        # Ensure URL is absolute
                        absolute_url = urljoin(str(response.url), relative_url)
                        results.append({'book_page_url': absolute_url})
                        LOG info f"Found potential book URL: {absolute_url}"
                    ELSE
                        LOG warning f"Could not find valid link element in search result item for '{query}'."
                    ENDIF
                ENDFOR

                RETURN results

            EXCEPT Exception as parse_exc:
                LOG exception f"Error parsing search results page for '{query}': {parse_exc}"
                RAISE InternalParsingError(f"Failed to parse search results page: {parse_exc}")

    EXCEPT httpx.RequestError as req_err:
        LOG error f"Network error during internal search for '{query}': {req_err}"
        RAISE InternalFetchError(f"Network error during search: {req_err}")
    EXCEPT Exception as e:
         LOG exception f"Unexpected error during internal search for '{query}': {e}"
         RAISE InternalFetchError(f"An unexpected error occurred during search: {e}") # Or re-raise

END FUNCTION
```

## 6. Function: `_internal_get_book_details_by_id`

**Signature:**

```python
async def _internal_get_book_details_by_id(book_id: str, domain: str) -> dict:
```

**Pseudocode:**

```python
# File: lib/python_bridge.py (Addition/Modification)
# Dependencies: httpx, bs4, logging, asyncio, urllib.parse
# Assumes _internal_search and custom exceptions are defined

ASYNC FUNCTION _internal_get_book_details_by_id(book_id: str, domain: str) -> dict:
    """Fetches book details using the Search-First strategy."""
    LOG info f"Attempting Search-First internal lookup for book ID {book_id}"

    # 1. Search for the book ID to find the correct URL
    TRY
        search_results = AWAIT _internal_search(query=str(book_id), domain=domain, count=1)
    EXCEPT (InternalFetchError, InternalParsingError) as search_err:
        LOG error f"Internal search step failed for book ID {book_id}: {search_err}"
        # Propagate or wrap the error; using InternalFetchError as a general category
        RAISE InternalFetchError(f"Search step failed for ID {book_id}: {search_err}")
    EXCEPT Exception as e:
        LOG exception f"Unexpected error during search step for ID {book_id}: {e}"
        RAISE InternalFetchError(f"Unexpected error during search step for ID {book_id}: {e}")


    IF NOT search_results:
        LOG warning f"Internal search for book ID {book_id} returned no results."
        RAISE InternalBookNotFoundError(f"Book ID {book_id} not found via internal search.")
    ENDIF

    book_page_url = search_results[0].get('book_page_url')
    IF NOT book_page_url:
        LOG error f"Internal search result for book ID {book_id} missing 'book_page_url'."
        RAISE InternalParsingError("Search result parsing failed: book_page_url missing.")
    ENDIF

    LOG info f"Found book page URL via search: {book_page_url}"

    # 2. Fetch the book detail page
    headers = { 'User-Agent': 'Mozilla/5.0 ...' }
    timeout_seconds = 15

    TRY
        ASYNC WITH httpx.AsyncClient(follow_redirects=True, timeout=timeout_seconds) as client:
            response = AWAIT client.get(book_page_url, headers=headers)

            IF response.status_code == 404 THEN
                 LOG warning f"Book page fetch for ID {book_id} resulted in 404 at {book_page_url}."
                 # This might indicate the search result was stale or incorrect
                 RAISE InternalBookNotFoundError(f"Book page not found (404) at {book_page_url} for ID {book_id}.")
            ENDIF

            response.raise_for_status() # Raise for other non-200 errors

            # 3. Parse the book detail page
            TRY
                soup = BeautifulSoup(response.text, 'lxml')
                details = {}

                # --- Placeholder Selectors (MUST BE VERIFIED/REFINED) ---
                title_element = soup.select_one('h1[itemprop="name"]') # Example
                details['title'] = title_element.text.strip() IF title_element ELSE None

                author_element = soup.select_one('a[itemprop="author"]') # Example
                details['author'] = author_element.text.strip() IF author_element ELSE None

                year_element = soup.select_one('.property-year .property_value') # Example
                details['year'] = year_element.text.strip() IF year_element ELSE None

                publisher_element = soup.select_one('.property-publisher .property_value') # Example
                details['publisher'] = publisher_element.text.strip() IF publisher_element ELSE None

                description_element = soup.select_one('.book-description') # Example
                details['description'] = description_element.text.strip() IF description_element ELSE None

                # Download URL(s) - This often requires specific logic
                download_link_element = soup.select_one('a.btn-primary[href*="/download"]') # Example
                download_url = None
                IF download_link_element AND download_link_element.has_attr('href'):
                    download_url = urljoin(str(response.url), download_link_element['href'])
                details['download_url'] = download_url
                # --- End Placeholder Selectors ---

                # Check for essential missing data
                IF details['title'] IS None OR details['download_url'] IS None THEN
                    LOG error f"Parsing failed for book ID {book_id}: Essential details missing (title or download_url)."
                    RAISE InternalParsingError(f"Failed to parse essential details (title/download_url) for book ID {book_id}.")
                ENDIF

                LOG info f"Successfully parsed details for book ID {book_id} from {book_page_url}"
                RETURN details

            EXCEPT Exception as parse_exc:
                LOG exception f"Error parsing book detail page for ID {book_id} at {book_page_url}: {parse_exc}"
                RAISE InternalParsingError(f"Failed to parse book detail page: {parse_exc}")

    EXCEPT httpx.RequestError as req_err:
        LOG error f"Network error fetching book page for ID {book_id} at {book_page_url}: {req_err}"
        RAISE InternalFetchError(f"Network error fetching book page: {req_err}")
    EXCEPT httpx.HTTPStatusError as status_err:
        LOG error f"HTTP status error {status_err.response.status_code} fetching book page for ID {book_id} at {book_page_url}: {status_err}"
        RAISE InternalFetchError(f"HTTP error {status_err.response.status_code} fetching book page.")
    # InternalBookNotFoundError, InternalParsingError raised directly from parsing block
    EXCEPT Exception as e:
         LOG exception f"Unexpected error fetching/parsing book page for ID {book_id}: {e}"
         RAISE InternalFetchError(f"An unexpected error occurred fetching/parsing book page: {e}") # Or re-raise

END FUNCTION
```

## 7. Caller Modifications (`if __name__ == "__main__":` block)

The main execution block in `lib/python_bridge.py` needs to be updated. Functions handling actions like `get_book_details` or `get_download_info` (or a unified function) must now call `asyncio.run(_internal_get_book_details_by_id(book_id, domain))` instead of relying on the external `zlibrary` client or the previous internal 404-handling function.

Crucially, the error handling must translate the new internal exceptions:
- `InternalBookNotFoundError` should be caught and re-raised as `ValueError("Book ID ... not found.")` for the Node.js side.
- `InternalFetchError` and `InternalParsingError` (and potentially `httpx` errors if not caught internally) should be caught and re-raised as `RuntimeError("Failed to fetch/parse book details...")` for the Node.js side.

**Pseudocode Snippet:**

```python
# File: lib/python-bridge.py (Modification in __main__ block)

# Example for 'get_book_details' action handler
elif cli_args.function_name == 'get_book_details':
    book_id = args_dict.get('book_id')
    domain = args_dict.get('domain', 'z-library.sk') # Or get from config/args
    IF NOT book_id: RAISE ValueError("Missing 'book_id'")

    TRY
        # Call the new async function using asyncio.run()
        details = asyncio.run(_internal_get_book_details_by_id(book_id, domain))
        response = details # Format if needed

    EXCEPT InternalBookNotFoundError as e:
        # Translate to ValueError for Node.js
        LOG warning f"BookNotFound for ID {book_id}: {e}"
        RAISE ValueError(f"Book ID {book_id} not found.")
    EXCEPT (InternalFetchError, InternalParsingError) as e:
        # Translate fetch/parse errors to RuntimeError
        LOG error f"Fetch/Parse error for ID {book_id}: {e}"
        RAISE RuntimeError(f"Failed to fetch or parse details for book ID {book_id}.")
    EXCEPT Exception as e:
         # Catch any other unexpected errors from the async function or asyncio.run
         LOG exception f"Unexpected error processing book ID {book_id}"
         RAISE RuntimeError(f"An unexpected error occurred processing book ID {book_id}.")

# Similar logic for 'get_download_info', potentially extracting just 'download_url' from details dict
elif cli_args.function_name == 'get_download_info':
     # ... get book_id, domain ...
     TRY
         details = asyncio.run(_internal_get_book_details_by_id(book_id, domain))
         IF details.get('download_url') IS None:
             RAISE InternalParsingError("Download URL missing from parsed details.") # Caught below
         response = {"download_url": details['download_url']}
     EXCEPT InternalBookNotFoundError as e:
         RAISE ValueError(f"Book ID {book_id} not found.")
     EXCEPT (InternalFetchError, InternalParsingError) as e:
         RAISE RuntimeError(f"Failed to get download info for book ID {book_id}.")
     EXCEPT Exception as e:
         RAISE RuntimeError(f"An unexpected error occurred getting download info for book ID {book_id}.")

# ... rest of main block ...
# print(json.dumps(response))
```

## 8. TDD Anchors

**`_internal_search`:**
- Test successful search returns list with `{'book_page_url': '...'}`.
- Test search with no results returns empty list `[]`.
- Test search page parsing error raises `InternalParsingError`.
- Test network error (timeout, connection refused) raises `InternalFetchError`.
- Test non-200 HTTP status code raises `InternalFetchError`.
- Test `urljoin` correctly makes relative URLs absolute.

**`_internal_get_book_details_by_id`:**
- Test successful flow: search -> fetch -> parse -> returns details dict.
- Test `_internal_search` fails (raises `InternalFetchError`) -> raises `InternalFetchError`.
- Test `_internal_search` returns empty list -> raises `InternalBookNotFoundError`.
- Test `_internal_search` result missing `book_page_url` -> raises `InternalParsingError`.
- Test book page fetch fails (network error) -> raises `InternalFetchError`.
- Test book page fetch returns 404 -> raises `InternalBookNotFoundError`.
- Test book page fetch returns other non-200 status -> raises `InternalFetchError`.
- Test book page parsing fails (bad HTML) -> raises `InternalParsingError`.
- Test book page parsing succeeds but essential data (title, download_url) is missing -> raises `InternalParsingError`.

**Caller Modifications (`if __name__ == "__main__":`)**
- Test `get_book_details` handler calls `asyncio.run(_internal_get_book_details_by_id)`.
- Test `get_book_details` handler translates `InternalBookNotFoundError` to `ValueError`.
- Test `get_book_details` handler translates `InternalFetchError` to `RuntimeError`.
- Test `get_book_details` handler translates `InternalParsingError` to `RuntimeError`.
- Test `get_download_info` handler calls `asyncio.run(_internal_get_book_details_by_id)`.
- Test `get_download_info` handler translates `InternalBookNotFoundError` to `ValueError`.
- Test `get_download_info` handler translates `InternalFetchError`/`InternalParsingError` to `RuntimeError`.
- Test `get_download_info` handler raises `RuntimeError` if `download_url` is missing after successful parse.