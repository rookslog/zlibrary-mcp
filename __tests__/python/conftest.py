import sys
import os
import pytest

# Add project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# PDF fixtures are stored in Git LFS. A clone made without LFS (or with
# GIT_LFS_SKIP_SMUDGE set) leaves ~130-byte pointer files on disk in their place.
# PyMuPDF then fails with "no objects found", and the real tests fail with
# assertion errors that look like detection regressions. Detect the pointers and
# skip with a message that names the actual cause.
_LFS_POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def is_lfs_pointer(path) -> bool:
    """True if `path` is an unhydrated Git LFS pointer rather than real content."""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_LFS_POINTER_MAGIC)) == _LFS_POINTER_MAGIC
    except OSError:
        return False


def require_real_fixture(path):
    """Skip the calling test when `path` is missing or an unhydrated LFS pointer."""
    if not os.path.isfile(path):
        pytest.skip(f"fixture missing: {path}")
    if is_lfs_pointer(path):
        pytest.skip(
            f"fixture {os.path.basename(path)} is an unhydrated Git LFS pointer. "
            "Run 'git lfs install && git lfs pull' to fetch the real PDF."
        )


@pytest.fixture
def lfs_fixture():
    """Fixture form of require_real_fixture for tests that prefer injection."""
    return require_real_fixture


@pytest.fixture(autouse=True)
def _clear_pymupdf_cache():
    """Clear PyMuPDF textpage cache before each test to prevent stale object ID reuse."""
    try:
        from lib.rag.utils.cache import _clear_textpage_cache

        _clear_textpage_cache()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _no_preflight_probes(request, monkeypatch):
    """Keep the multi-source pre-flight probe off the network in unit tests.

    The source adapters probe DNS and TCP before searching, so a test that
    mocks only the HTTP client would otherwise make a real connection to
    whatever hostname its fixture config names — slow, flaky, and dependent on
    the machine's egress. Tests that mean to exercise the probe itself opt back
    in with `@pytest.mark.real_preflight`.
    """
    if request.node.get_closest_marker("real_preflight"):
        return
    try:
        from lib.sources import net
    except ImportError:
        return

    async def _skip(*_args, **_kwargs):
        return None

    monkeypatch.setattr(net, "probe_host", _skip)
    for module_name in ("lib.sources.annas", "lib.sources.libgen"):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "probe_host"):
            monkeypatch.setattr(module, "probe_host", _skip)
    net.reset_probe_cache()
