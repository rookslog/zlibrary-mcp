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
