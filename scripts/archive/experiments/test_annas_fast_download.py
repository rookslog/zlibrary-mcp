#!/usr/bin/env python3
"""Test Anna's Archive fast download API.

Prerequisites:
  Create .env file with ANNAS_SECRET_KEY (or export it)
  Set TEST_MD5 from search results

Run: uv run python scripts/experiments/test_annas_fast_download.py
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
import httpx
from zlibrary.eapi import RedirectRefusedError, raise_for_refused_redirect

# Load .env from project root
load_dotenv(Path(__file__).parent.parent.parent / ".env")

SECRET_KEY = os.environ.get("ANNAS_SECRET_KEY")
TEST_MD5 = os.environ.get("TEST_MD5")
BASE_URL = os.environ.get("ANNAS_BASE_URL", "https://annas-archive.example.invalid")


def test_fast_download(md5: str):
    """Test the fast download API endpoint."""
    url = f"{BASE_URL}/dyn/api/fast_download.json?md5={md5}&key={SECRET_KEY}"
    print(f"Requesting: {url[:60]}...&key=***")

    try:
        response = httpx.get(url, timeout=30, follow_redirects=False)
        raise_for_refused_redirect(response)
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type', 'unknown')}")

        if response.status_code == 200:
            try:
                data = response.json()
                print("\nResponse JSON:")
                print(json.dumps(data, indent=2))
                return data
            except json.JSONDecodeError:
                print("\nResponse is not JSON:")
                print(response.text[:1000])
                return {"status": "not_json", "body": response.text[:1000]}
        else:
            print(f"\nError response ({response.status_code}):")
            print(response.text[:1000])
            return {
                "status": "http_error",
                "code": response.status_code,
                "body": response.text[:500],
            }

    except RedirectRefusedError as e:
        return {
            "status": "redirect_refused",
            "code": e.status_code,
            "host": e.target_host,
        }
    except httpx.TimeoutException:
        print("ERROR: Request timed out")
        return {"status": "timeout"}
    except Exception as e:
        print(f"Exception: {type(e).__name__}: {e}")
        return {"status": "exception", "error": str(e)}


def test_download_url(url: str):
    """Test that the download URL actually works."""
    print(f"\nTesting download URL: {url[:80]}...")

    try:
        # Just do a HEAD request to check it's valid
        response = httpx.head(url, timeout=30, follow_redirects=True)
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type', 'unknown')}")
        print(f"Content-Length: {response.headers.get('content-length', 'unknown')}")

        if response.status_code == 200:
            return True
        else:
            print(f"Unexpected status: {response.status_code}")
            return False

    except Exception as e:
        print(f"Download URL test failed: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    print("=== Testing Anna's Archive Fast Download API ===\n")

    if not SECRET_KEY:
        print("ERROR: Set ANNAS_SECRET_KEY environment variable")
        print("  export ANNAS_SECRET_KEY='your-key-here'")
        exit(1)

    if not TEST_MD5:
        print("ERROR: Set TEST_MD5 environment variable")
        print("  Run test_annas_search.py first to get an MD5")
        print("  export TEST_MD5='md5-from-search'")
        exit(1)

    print(f"Using MD5: {TEST_MD5}")
    print(f"Using base URL: {BASE_URL}")
    print()

    result = test_fast_download(TEST_MD5)

    print(f"\n{'=' * 50}")

    if result.get("download_url"):
        print("SUCCESS: Got download URL from API")
        print(f"  URL: {result['download_url'][:80]}...")

        # Test the URL actually works
        if test_download_url(result["download_url"]):
            print("\nDOWNLOAD URL VERIFIED: Ready to use")
        else:
            print("\nWARNING: Download URL returned but may not work")

    elif result.get("error"):
        print(f"API ERROR: {result['error']}")
        print("\nPossible causes:")
        print("  - Invalid API key")
        print("  - Daily quota exceeded (you have 25/day)")
        print("  - Invalid MD5 hash")
        print("  - Book not available for fast download")

    elif result.get("status") == "http_error":
        print(f"HTTP ERROR: {result.get('code')}")
        print("Check if the base URL is correct or if Anna's Archive is down")

    else:
        print("UNCLEAR RESPONSE: See output above")
        print("The API may have changed or returned unexpected format")
