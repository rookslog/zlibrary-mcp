"""Exercise session-value capture and output preparation with synthetic logins."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import pytest

ROOT = Path(__file__).resolve().parents[2]
KEY = "FAKE-STAMPED-SESSION-AX917"
USER_ID = "FAKE-STAMPED-USER-8291"
STAMP_ENV = "ZLIBRARY_TEST_SESSION_VALUES"


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def environment(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stamp = tmp_path / "sessions.jsonl"
    stamp.write_text("")
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("GITHUB_WORKSPACE", str(workspace))
    monkeypatch.setenv(STAMP_ENV, str(stamp))
    monkeypatch.setenv("ZLIBRARY_EMAIL", "fake-stamp@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "FAKE-STAMPED-PASSWORD")
    return workspace, stamp


def login(*, success=1, raises=False):
    helper = load(ROOT / "__tests__/python/integration/ci_credentials.py")

    class Client:
        remix_userid = USER_ID
        remix_userkey = KEY

        async def login(self, *_):
            if raises:
                raise ValueError("FAKE-LOGIN-ERROR")
            return {"success": success}

    return asyncio.run(
        helper.login_with_credentials(Client(), helper.environment_credentials())
    )


def prepare():
    return load(ROOT / "scripts/filter_live_test_output.py").filter_output()


@pytest.mark.parametrize(
    "printed",
    [
        f"[('remix_userid', '{USER_ID}'), ('remix_userkey', '{KEY}')]",
        f"Cookie(version=0, name='remix_userkey', value='{KEY}', ...)",
        f"('{USER_ID}', '{KEY}', <EAPIClient ...>)",
        f"remix_userkey%3D{KEY}",
        f"remix_userkey={KEY})\nremix_userkey={KEY}.\n{KEY}",
    ],
    ids=[
        "cookie-items",
        "cookiejar",
        "login-tuple",
        "url-field",
        "trailing-punctuation",
    ],
)
def test_stamped_values_cover_anonymous_session_shapes(environment, printed):
    workspace, stamp = environment
    assert login() is True
    (workspace / "live.log").write_text(printed + f"\n{USER_ID}\n{KEY}")
    (workspace / "live.xml").write_text(
        f"<testsuites><testcase message={quoteattr(printed)}>{escape(printed + USER_ID + KEY)}</testcase></testsuites>"
    )
    assert prepare() == 0
    log = (workspace / "live.log").read_text()
    xml = (workspace / "live.xml").read_text()
    assert KEY not in log + xml
    assert USER_ID not in log + xml
    assert "[filtered]" in log
    assert not stamp.exists()


@pytest.mark.parametrize(
    "expression",
    ["client.remix_userkey == 'expected'", "len(client.remix_userkey) == 1"],
)
def test_real_pytest_attribute_failures_use_captured_session_values(
    environment, expression
):
    workspace, stamp = environment
    (workspace / "pytest.ini").write_text("[pytest]\nasyncio_mode=auto\n")
    helper_dir = str(ROOT / "__tests__/python/integration")
    (workspace / "test_session.py").write_text(
        "import sys, pytest, httpx\n"
        f"sys.path.insert(0, {helper_dir!r})\n"
        "from ci_credentials import environment_credentials, login_with_credentials\n"
        "from zlibrary.eapi import EAPIClient\n"
        "async def test_session(monkeypatch):\n"
        "    real = httpx.AsyncClient\n"
        "    def reply(request):\n"
        "        assert request.url.host == 'session.invalid'\n"
        f"        return httpx.Response(200, json={{'success': 1, 'user': {{'id': {USER_ID!r}, 'remix_userkey': {KEY!r}}}}})\n"
        "    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(reply), **kw))\n"
        "    client = EAPIClient('session.invalid')\n"
        "    try:\n"
        "        assert await login_with_credentials(client, environment_credentials())\n"
        f"        assert {expression}\n"
        "    finally:\n"
        "        await client.close()\n"
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "--benchmark-disable",
            "--no-header",
            "--tb=short",
            "--junitxml=live.xml",
            "-q",
        ],
        cwd=workspace,
        env={**os.environ, "PYTHONPATH": str(ROOT / "zlibrary/src")},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    (workspace / "live.log").write_text(run.stdout + run.stderr)
    assert KEY in (workspace / "live.log").read_text()
    assert KEY in "".join(ET.parse(workspace / "live.xml").getroot().itertext())
    assert prepare() == 0
    assert KEY not in (workspace / "live.log").read_text()
    assert KEY not in (workspace / "live.xml").read_text()
    assert not stamp.exists()


@pytest.mark.parametrize("case", ["missing", "malformed", "unfinished"])
def test_missing_or_invalid_session_capture_prevents_preparation(environment, case):
    workspace, stamp = environment
    assert login() is True
    if case == "missing":
        stamp.unlink()
    elif case == "malformed":
        stamp.write_text("not json\n")
    else:
        stamp.write_text(
            json.dumps({"attempt": "interrupted", "status": "pending"}) + "\n"
        )
    (workspace / "live.log").write_text(KEY)
    assert prepare() == 1
    assert not stamp.exists()


def test_capture_is_removed_when_output_is_invalid(environment):
    workspace, stamp = environment
    assert login() is True
    (workspace / "live.xml").write_text("<broken>")
    assert prepare() == 1
    assert not stamp.exists()


@pytest.mark.parametrize("success", [0, 1])
def test_helper_records_actual_client_values_and_preserves_result(environment, success):
    _, stamp = environment
    assert login(success=success) is bool(success)
    assert KEY in stamp.read_text()
    assert USER_ID in stamp.read_text()
    assert stamp.stat().st_mode & 0o077 == 0


def test_exceptional_login_still_records_client_values(environment):
    _, stamp = environment
    with pytest.raises(RuntimeError, match="^Integration login failed$"):
        login(raises=True)
    assert KEY in stamp.read_text()
    assert USER_ID in stamp.read_text()


def test_filter_keeps_values_from_multiple_logins(environment):
    workspace, stamp = environment
    assert login() is True
    helper = load(ROOT / "__tests__/python/integration/ci_credentials.py")

    class AnotherClient:
        remix_userid = "FAKE-SECOND-USER"
        remix_userkey = "FAKE-SECOND-SESSION"

        async def login(self, *_):
            return {"success": 1}

    assert asyncio.run(
        helper.login_with_credentials(AnotherClient(), helper.environment_credentials())
    )
    (workspace / "live.log").write_text(KEY + " FAKE-SECOND-SESSION")
    assert prepare() == 0
    assert (workspace / "live.log").read_text() == "[filtered] [filtered]"
    assert not stamp.exists()


def test_capture_inside_workspace_is_rejected(environment, monkeypatch):
    workspace, _ = environment
    stamp = workspace / "sessions.jsonl"
    stamp.write_text("")
    monkeypatch.setenv(STAMP_ENV, str(stamp))
    with pytest.raises(RuntimeError, match="^Integration login failed$"):
        login()
    (workspace / "live.log").write_text(KEY)
    assert prepare() == 1


def test_interrupted_pytest_with_clean_partial_xml_is_failed(tmp_path):
    workflow = json.loads(
        subprocess.check_output(
            [
                "node",
                "--input-type=module",
                "-e",
                "import fs from 'node:fs';import {parse} from 'yaml';process.stdout.write(JSON.stringify(parse(fs.readFileSync('.github/workflows/upstream-check.yml','utf8'))));",
            ],
            cwd=ROOT,
            text=True,
        )
    )
    live = next(
        step
        for step in workflow["jobs"]["live_tests"]["steps"]
        if step.get("id") == "live"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(
        ROOT / "scripts/classify_live_tests.py", scripts / "classify_live_tests.py"
    )
    policy = tmp_path / "zlibrary/src/zlibrary/eapi.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("DEFAULT_EAPI_DOMAINS = ['z-library.ec']\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"#!{sys.executable}\nimport sys, os\nfrom pathlib import Path\n"
        "if sys.argv[2] == 'pytest':\n"
        "    if '--collect-only' in sys.argv: sys.exit(0)\n"
        "    Path('live.xml').write_text('<testsuites><testsuite><testcase name=\"partial\"/></testsuite></testsuites>')\n"
        "    sys.exit(2)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[3:]])\n"
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "ZLIB_PROBE_DOMAIN": "z-library.ec",
        "ZLIB_BLOCKED": "false",
        "ZLIBRARY_EMAIL": "fake-interrupted@example.test",
        "ZLIBRARY_PASSWORD": "FAKE-INTERRUPTED",
        STAMP_ENV: str(tmp_path.parent / (tmp_path.name + "-sessions.jsonl")),
    }
    run = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", live["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "status=failed" in (tmp_path / "outputs").read_text()
    assert "pytest exit 2" in run.stdout


LONG_KEY = "0686f57587b17a4e3999cd5cd2d654bc"


def run_login_case(
    workspace, statement, *, direct=False, integration_setup=False, collection=False
):
    helper_dir = str(ROOT / "__tests__/python/integration")
    test_dir = workspace
    if direct or integration_setup:
        test_dir = workspace / "__tests__/python/integration"
        test_dir.mkdir(parents=True)
        (test_dir / "__init__.py").write_text("")
        for name in ["conftest.py", "ci_credentials.py"]:
            shutil.copyfile(
                ROOT / "__tests__/python/integration" / name, test_dir / name
            )
        (workspace / "lib").symlink_to(ROOT / "lib", target_is_directory=True)
    (workspace / "pytest.ini").write_text("[pytest]\nasyncio_mode=auto\n")
    prefix = (
        "import sys, pytest, pytest_asyncio, httpx\n"
        f"sys.path.insert(0, {helper_dir!r})\n"
        "from ci_credentials import environment_credentials, login_with_credentials\n"
        "from zlibrary.eapi import EAPIClient\n"
    )
    fixture = (
        "@pytest_asyncio.fixture(scope='module', loop_scope='module')\n"
        "async def logged_in():\n"
        "    with pytest.MonkeyPatch.context() as monkeypatch:\n"
        "        real = httpx.AsyncClient\n"
        "        def reply(request):\n"
        "            assert request.url.host == 'session.invalid'\n"
        f"            return httpx.Response(200, json={{'success': 1, 'user': {{'id': {USER_ID!r}, 'remix_userkey': {LONG_KEY!r}}}}})\n"
        "        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(reply), **kw))\n"
        "        client = EAPIClient('session.invalid')\n"
        "        try:\n"
        + (
            "            result = await client.login('fake-direct@example.test', 'FAKE-DIRECT-PASSWORD')\n"
            "            assert result['success'] == 1\n"
            if direct
            else "            assert await login_with_credentials(client, environment_credentials())\n"
        )
        + "            yield client\n"
        "        finally:\n"
        "            await client.close()\n"
    )
    if collection:
        fixture = fixture.replace(
            "@pytest_asyncio.fixture(scope='module', loop_scope='module')\n", ""
        ).replace("yield client", "return client.remix_userkey")
        source = (
            prefix
            + fixture
            + "import asyncio\nasyncio.run(logged_in())\ndef test_collection():\n    pass\n"
        )
    else:
        source = (
            prefix
            + fixture
            + "def test_fragment(logged_in):\n    client = logged_in\n    "
            + statement
            + "\n"
        )
    (test_dir / "test_fragment.py").write_text(source)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_dir / "test_fragment.py"),
            *(["--collect-only"] if collection else []),
            "--no-cov",
            "--benchmark-disable",
            "--no-header",
            "--tb=short",
            "--junitxml=live.xml",
            "-q",
        ],
        cwd=workspace,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(ROOT / "zlibrary/src"), str(ROOT / "lib")]
            ),
        },
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "assert client.remix_userkey == 'expected'",
        "pytest.fail(client.remix_userkey)",
        "pytest.fail('remix_userkey%3D' + client.remix_userkey)",
    ],
    ids=["assertion", "failure-message", "encoded-field"],
)
def test_shortened_pytest_session_runs_are_removed(environment, statement):
    workspace, stamp = environment
    assert len(LONG_KEY) == 32
    run = run_login_case(workspace, statement)
    assert run.returncode == 1, run.stdout + run.stderr
    (workspace / "live.log").write_text(run.stdout + run.stderr)
    windows = {LONG_KEY[start : start + 8] for start in range(25)}
    raw = (workspace / "live.log").read_text() + (workspace / "live.xml").read_text()
    assert any(window in raw for window in windows)
    assert LONG_KEY in stamp.read_text()
    assert prepare() == 0
    written = (workspace / "live.log").read_text() + (
        workspace / "live.xml"
    ).read_text()
    assert not any(window in written for window in windows)
    assert not stamp.exists()


@pytest.mark.parametrize("direct", [True, False], ids=["direct", "opaque-helper"])
def test_module_fixture_login_capture_covers_direct_and_helper_calls(
    environment, direct
):
    workspace, stamp = environment
    run = run_login_case(
        workspace,
        "pytest.fail(client.remix_userkey)",
        direct=direct,
        integration_setup=True,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    report = ET.parse(workspace / "live.xml")
    assert not list(report.iter("error"))
    assert len(list(report.iter("failure"))) == 1
    assert LONG_KEY in stamp.read_text()
    records = [json.loads(line) for line in stamp.read_text().splitlines()]
    assert [r["status"] for r in records] == ["pending", "complete"]
    (workspace / "live.log").write_text(run.stdout + run.stderr)
    assert prepare() == 0
    written = (workspace / "live.log").read_text() + (
        workspace / "live.xml"
    ).read_text()
    assert not any(LONG_KEY[i : i + 8] in written for i in range(25))
    assert not stamp.exists()


def test_session_writer_refuses_symlink_without_changing_target(environment):
    _, stamp = environment
    target = stamp.with_name("outside-target.jsonl")
    target.write_text("synthetic target\n")
    stamp.unlink()
    stamp.symlink_to(target)
    with pytest.raises(RuntimeError, match="^Integration login failed$"):
        login()
    assert target.read_text() == "synthetic target\n"


def test_completion_without_pending_has_fixed_failure_output(environment):
    workspace, stamp = environment
    stamp.write_text(
        json.dumps(
            {
                "attempt": "unknown-attempt",
                "status": "complete",
                "values": [USER_ID, KEY],
            }
        )
        + "\n"
    )
    (workspace / "live.log").write_text(KEY)
    module = load(ROOT / "scripts/filter_live_test_output.py")
    try:
        module.captured_values(stamp)
    except Exception as error:
        assert isinstance(error, ValueError)
        assert str(error) == "invalid session capture"
    else:
        pytest.fail("An orphan completion must be rejected by the parser")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=workspace,
        env=os.environ,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    assert run.stdout == "Could not prepare test output for upload.\n"
    assert run.stderr == ""
    assert not stamp.exists()


@pytest.mark.parametrize("duplicate", ["pending", "completed"])
def test_duplicate_capture_attempts_reject_preparation(environment, duplicate):
    workspace, stamp = environment
    pending = {"attempt": "same-attempt", "status": "pending"}
    complete = {
        "attempt": "same-attempt",
        "status": "complete",
        "values": [USER_ID, KEY],
    }
    records = (
        [pending, pending, complete]
        if duplicate == "pending"
        else [pending, complete, pending, complete]
    )
    stamp.write_text("".join(json.dumps(record) + "\n" for record in records))
    (workspace / "live.log").write_text(KEY)
    assert prepare() == 1
    assert not stamp.exists()


@pytest.mark.parametrize(
    "captured,printed,expected",
    [
        ("0123456789abcdef", "3456789a", "[filtered]"),
        ("0123456789abcde", "3456789a", "3456789a"),
        ("0123456789abcdef", "3456789", "3456789"),
        (LONG_KEY, "prefix " + LONG_KEY[1:14] + " suffix", "prefix [filtered] suffix"),
        (LONG_KEY, LONG_KEY[:12] + "..." + LONG_KEY[-12:], "[filtered]...[filtered]"),
    ],
    ids=["sixteen-eight", "fifteen-eight", "sixteen-seven", "entire-run", "two-runs"],
)
def test_shortened_pytest_capture_boundaries(environment, captured, printed, expected):
    workspace, stamp = environment
    records = [
        {"attempt": "boundary", "status": "pending"},
        {"attempt": "boundary", "status": "complete", "values": ["", captured]},
    ]
    stamp.write_text("".join(json.dumps(record) + "\n" for record in records))
    (workspace / "live.log").write_text(printed)
    assert prepare() == 0
    assert (workspace / "live.log").read_text() == expected


def test_complete_configured_value_is_replaced_before_session_fragments(
    environment, monkeypatch
):
    workspace, stamp = environment
    configured = "configured-prefix-" + KEY[2:10] + "-configured-suffix"
    monkeypatch.setenv("ZLIBRARY_PASSWORD", configured)
    assert login() is True
    (workspace / "live.log").write_text(configured)
    assert prepare() == 0
    assert (workspace / "live.log").read_text() == "[filtered]"


@pytest.mark.parametrize("name", ["live.log", "live.xml"])
def test_session_fragment_recheck_detects_contamination(environment, monkeypatch, name):
    workspace, _ = environment
    assert login() is True
    fragment = KEY[2:10]
    (workspace / name).write_text(
        "ordinary output" if name.endswith("log") else "<testsuites/>"
    )
    write = Path.write_bytes

    def contaminate(path, data):
        if path.name == name:
            if name.endswith("log"):
                data += fragment.encode()
            else:
                tree = ET.fromstring(data)
                tree.set("message", fragment)
                data = ET.tostring(tree)
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", contaminate)
    assert prepare() == 1


def test_direct_login_during_collection_is_captured(environment):
    workspace, stamp = environment
    run = run_login_case(workspace, "pass", direct=True, collection=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert LONG_KEY in stamp.read_text()
    records = [json.loads(line) for line in stamp.read_text().splitlines()]
    assert [record["status"] for record in records] == ["pending", "complete"]
