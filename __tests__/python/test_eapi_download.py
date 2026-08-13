"""Tests for EAPIClient.download_file method."""

import asyncio
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root and zlibrary src to path
project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, project_root)
sys.path.insert(0, os.path.join(project_root, "zlibrary", "src"))

from zlibrary.eapi import EAPIClient  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def eapi_client():
    """Create an EAPIClient with test credentials."""
    return EAPIClient(
        domain="test.z-library.sk",
        remix_userid="12345",
        remix_userkey="testkey",
    )


@pytest.fixture
def tmp_download_dir(tmp_path):
    """Provide a temporary download directory."""
    return str(tmp_path / "downloads")


class TestDownloadFile:
    """Tests for EAPIClient.download_file."""

    @pytest.mark.asyncio
    async def test_download_file_success(self, eapi_client, tmp_download_dir):
        """download_file gets link then downloads the file."""
        mock_dl_response = {
            "file": {"downloadLink": "https://dl.z-library.sk/file/book123.epub"}
        }

        # Mock get_download_link
        eapi_client.get_download_link = AsyncMock(return_value=mock_dl_response)

        # Mock the httpx streaming download
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {
            "content-disposition": 'attachment; filename="mybook.epub"'
        }
        mock_response.url = "https://dl.z-library.sk/file/book123.epub"

        async def mock_aiter_bytes():
            yield b"fake epub content here"

        mock_response.aiter_bytes = mock_aiter_bytes

        # Create a mock async context manager for stream
        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client_instance = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_cm)

        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("zlibrary.eapi.httpx.AsyncClient", return_value=mock_client_cm):
            result = await eapi_client.download_file(
                book_id=123,
                book_hash="abc123",
                output_dir=tmp_download_dir,
            )

        # Verify get_download_link was called
        eapi_client.get_download_link.assert_awaited_once_with(123, "abc123")

        # Verify file was saved
        assert os.path.exists(result)
        assert "mybook.epub" in result
        with open(result, "rb") as f:
            assert f.read() == b"fake epub content here"

    @pytest.mark.asyncio
    async def test_download_file_cancellation_removes_partial_output(
        self, eapi_client, tmp_download_dir
    ):
        """Cancellation after a written chunk removes the incomplete final path."""
        eapi_client.get_download_link = AsyncMock(
            return_value={
                "file": {"downloadLink": "https://dl.z-library.sk/file/book123.epub"}
            }
        )
        output_path = os.path.join(tmp_download_dir, "partial.epub")
        first_chunk_written = asyncio.Event()
        release_stream = asyncio.Event()
        lifecycle = {
            "client_entered": False,
            "client_exited": False,
            "stream_entered": False,
            "stream_exited": False,
        }

        class Response:
            headers = {"content-disposition": 'attachment; filename="partial.epub"'}
            url = "https://dl.z-library.sk/file/book123.epub"

            def raise_for_status(self):
                return None

            async def aiter_bytes(self):
                yield b"partial content"
                # The generator is resumed only after download_file has awaited
                # the real file write for the yielded chunk.
                first_chunk_written.set()
                await release_stream.wait()

        class StreamContext:
            async def __aenter__(self):
                lifecycle["stream_entered"] = True
                return Response()

            async def __aexit__(self, exc_type, exc, traceback):
                lifecycle["stream_exited"] = True
                return False

        class Client:
            def stream(self, method, url):
                return StreamContext()

        class ClientContext:
            async def __aenter__(self):
                lifecycle["client_entered"] = True
                return Client()

            async def __aexit__(self, exc_type, exc, traceback):
                lifecycle["client_exited"] = True
                return False

        with patch("zlibrary.eapi.httpx.AsyncClient", return_value=ClientContext()):
            task = asyncio.create_task(
                eapi_client.download_file(
                    book_id=123,
                    book_hash="abc123",
                    output_dir=tmp_download_dir,
                )
            )
            await asyncio.wait_for(first_chunk_written.wait(), timeout=1)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert not os.path.exists(output_path)
        assert lifecycle == {
            "client_entered": True,
            "client_exited": True,
            "stream_entered": True,
            "stream_exited": True,
        }

    @pytest.mark.asyncio
    async def test_download_file_with_explicit_filename(
        self, eapi_client, tmp_download_dir
    ):
        """download_file uses explicit filename when provided."""
        mock_dl_response = {
            "file": {"downloadLink": "https://dl.z-library.sk/file/123"}
        }
        eapi_client.get_download_link = AsyncMock(return_value=mock_dl_response)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}
        mock_response.url = "https://dl.z-library.sk/file/123"

        async def mock_aiter_bytes():
            yield b"content"

        mock_response.aiter_bytes = mock_aiter_bytes

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client_instance = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_cm)

        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("zlibrary.eapi.httpx.AsyncClient", return_value=mock_client_cm):
            result = await eapi_client.download_file(
                book_id=123,
                book_hash="abc123",
                output_dir=tmp_download_dir,
                filename="custom_name.pdf",
            )

        assert "custom_name.pdf" in result

    @pytest.mark.asyncio
    async def test_download_file_no_link_raises(self, eapi_client, tmp_download_dir):
        """download_file raises RuntimeError when no download link returned."""
        eapi_client.get_download_link = AsyncMock(return_value={"success": 0})

        with pytest.raises(RuntimeError, match="no download link"):
            await eapi_client.download_file(
                book_id=999,
                book_hash="nope",
                output_dir=tmp_download_dir,
            )

    @pytest.mark.asyncio
    async def test_download_file_relative_url(self, eapi_client, tmp_download_dir):
        """download_file makes relative URLs absolute using base_url."""
        mock_dl_response = {"file": {"downloadLink": "/dl/file/book456.epub"}}
        eapi_client.get_download_link = AsyncMock(return_value=mock_dl_response)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.headers = {}
        mock_response.url = "https://test.z-library.sk/dl/file/book456.epub"

        async def mock_aiter_bytes():
            yield b"data"

        mock_response.aiter_bytes = mock_aiter_bytes

        mock_stream_cm = AsyncMock()
        mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream_cm.__aexit__ = AsyncMock(return_value=False)

        mock_client_instance = AsyncMock()
        mock_client_instance.stream = MagicMock(return_value=mock_stream_cm)

        mock_client_cm = AsyncMock()
        mock_client_cm.__aenter__ = AsyncMock(return_value=mock_client_instance)
        mock_client_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("zlibrary.eapi.httpx.AsyncClient", return_value=mock_client_cm):
            await eapi_client.download_file(
                book_id=456,
                book_hash="def456",
                output_dir=tmp_download_dir,
            )

        # Verify the stream was called with absolute URL
        mock_client_instance.stream.assert_called_once()
        call_args = mock_client_instance.stream.call_args
        assert call_args[0][1].startswith("https://")
