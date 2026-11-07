"""Replace sensitive values in uploaded test output using synthetic values only."""

import importlib.util
import base64
import html
from urllib.parse import quote, quote_plus
import json
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def workflow():
    return json.loads(
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


@pytest.mark.parametrize(
    "password",
    [
        "CANARY-OUTPUT-123",
        'CANARY<&"-OUTPUT-456',
        "CANARY-é雪<&\"'-OUTPUT-789",
        "CANARY'ONLY-PW",
    ],
)
def test_real_live_step_keeps_canaries_out_of_uploaded_files(
    workflow, tmp_path, password
):
    email = "canary-output@example.test"
    frame = "CANARY-FRAME-CONTEXT"
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = integration\n")
    shutil.copyfile(
        ROOT / "__tests__/python/integration/ci_credentials.py",
        tmp_path / "ci_credentials.py",
    )
    (tmp_path / "test_canary.py").write_text(
        "import os, sys, json, html, base64, logging, warnings, pytest\n"
        "from urllib.parse import quote, quote_plus\n"
        "from ci_credentials import environment_credentials\n"
        "@pytest.fixture\ndef credentials():\n    return environment_credentials()\n"
        f"@pytest.fixture\ndef context():\n    return {frame!r}\n"
        "def payload():\n"
        "    values = [os.environ['ZLIBRARY_EMAIL'], os.environ['ZLIBRARY_PASSWORD'], 'FAKE-session-雪<&\\\"'+chr(39)]\n"
        "    forms = []\n"
        "    for value in values:\n"
        "        forms.extend([value, html.escape(value), html.escape(value, quote=False), json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1], repr(value)[1:-1], ascii(value)[1:-1], quote(value, safe=''), quote_plus(value), base64.b64encode(value.encode()).decode()])\n"
        "    forms.extend([repr('body \"x\" ' + values[1]), ascii('body \"x\" ' + values[1])])\n"
        "    forms.append(repr({'success': 1, 'user': {'id': 'FAKE-user-1234', 'remix_userkey': values[-1]}}))\n"
        "    return '\\n'.join(forms)\n"
        "@pytest.fixture\ndef broken_setup():\n    pytest.fail(payload())\n"
        "@pytest.fixture\ndef broken_teardown():\n    yield\n    pytest.fail(payload())\n"
        "@pytest.mark.integration\ndef test_assertion(credentials, context):\n    assert False, payload()\n"
        "@pytest.mark.integration\ndef test_exception():\n    raise RuntimeError(payload())\n"
        "@pytest.mark.integration\ndef test_streams():\n"
        "    print(payload()); print(payload(), file=sys.stderr); logging.error(payload()); warnings.warn(payload()); pytest.fail('streams')\n"
        "@pytest.mark.integration\ndef test_setup(broken_setup):\n    pass\n"
        "@pytest.mark.integration\ndef test_teardown(broken_teardown):\n    pass\n"
        "@pytest.mark.integration\n@pytest.mark.parametrize('argument', [os.environ['ZLIBRARY_PASSWORD']])\ndef test_argument(argument):\n    pytest.fail(payload())\n"
    )
    with (tmp_path / "test_canary.py").open("a") as stream:
        stream.write(
            "\n@pytest.mark.integration\n@pytest.mark.asyncio\nasync def test_cookie_reply(monkeypatch, credentials):\n"
            "    import httpx\n    from zlibrary.eapi import EAPIClient\n"
            "    from ci_credentials import login_with_credentials\n"
            "    real_client = httpx.AsyncClient\n"
            "    def reply(request):\n"
            "        assert request.url.host == 'ci.invalid'\n"
            "        if request.url.path == '/eapi/user/login':\n"
            "            return httpx.Response(200, json={'success': 1, 'user': {'id': 987654, 'remix_userkey': 'FAKE-COOKIE-SESSIONKEY'}})\n"
            "        return httpx.Response(200, json={'success': 0, 'message': request.headers.get('cookie', '')})\n"
            "    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(reply), **kwargs))\n"
            "    client = EAPIClient('ci.invalid')\n"
            "    try:\n        assert await login_with_credentials(client, credentials)\n"
            "        response = await client.search('synthetic')\n"
            "        assert response.get('success') == 1, response.get('message')\n"
            "    finally:\n        await client.close()\n"
        )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ["classify_live_tests.py", "filter_live_test_output.py"]:
        source = ROOT / "scripts" / name
        if source.exists():
            shutil.copyfile(source, scripts / name)
    policy = tmp_path / "zlibrary/src/zlibrary/eapi.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("DEFAULT_EAPI_DOMAINS = ['z-library.ec']\n")
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    (bin_path / "python3").symlink_to(sys.executable)
    (bin_path / "tee").symlink_to(shutil.which("tee"))
    uv = bin_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "assert sys.argv[1] == 'run'\n"
        "if sys.argv[2] == 'pytest':\n"
        "    os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[3:]])\n"
        "assert sys.argv[2] == 'python'\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[3:]])\n"
    )
    uv.chmod(0o755)
    runner_temp = tmp_path.parent / (tmp_path.name + "-runner")
    runner_temp.mkdir()
    steps = workflow["jobs"]["live_tests"]["steps"]
    live = next(s for s in steps if s.get("id") == "live")
    capture_env = {
        "ZLIBRARY_TEST_SESSION_VALUES": live["env"][
            "ZLIBRARY_TEST_SESSION_VALUES"
        ].replace("${{ runner.temp }}", str(runner_temp))
    }
    env = {
        **capture_env,
        "PATH": str(bin_path),
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "outputs"),
        "ZLIB_PROBE_DOMAIN": "z-library.ec",
        "ZLIB_BLOCKED": "false",
        "ZLIBRARY_EMAIL": email,
        "ZLIBRARY_PASSWORD": password,
    }
    run = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", live["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, "The real classifier must preserve a failed test result"
    assert "status=failed" in (tmp_path / "outputs").read_text()
    # Unfiltered pytest output must never reach the workflow console.
    console = run.stdout + run.stderr
    for value in (email, password, "FAKE-COOKIE-SESSIONKEY"):
        for encoded in encoded_forms(value).values():
            assert encoded not in console
    assert password.encode("unicode_escape").decode() not in console
    assert "FAKE-COOKIE-SESSIONKEY" in (tmp_path / "live.log").read_text()

    # Establish that every encoder actually reached the real pytest files.
    before = ET.parse(tmp_path / "live.xml")
    before_text = "".join(before.getroot().itertext())
    for value in (email, password, 'FAKE-session-雪<&"' + chr(39)):
        for encoded in encoded_forms(value).values():
            assert encoded in before_text
            assert encoded in (tmp_path / "live.log").read_text()
    prepare = next((s for s in steps if s.get("id") == "filter_output"), None)
    if prepare is not None:
        run = subprocess.run(
            ["/bin/bash", "-e", "-o", "pipefail", "-c", prepare["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0
        assert all(
            value not in run.stdout + run.stderr for value in [email, password, frame]
        )
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    for name in upload["with"]["path"].split():
        data = (tmp_path / name).read_text()
        assert all(value not in data for value in [email, password, frame])
    root = ET.parse(tmp_path / "live.xml")
    assert len(list(root.iter("testcase"))) == 7
    assert len(list(root.iter("failure"))) == 5
    assert len(list(root.iter("error"))) == 2
    decoded = "".join(root.getroot().itertext()) + "".join(
        value for element in root.iter() for value in element.attrib.values()
    )
    assert all(value not in decoded for value in [email, password, frame])
    assert "FAKE-COOKIE-SESSIONKEY" not in decoded
    assert "987654" not in decoded
    for representation in (repr('body "x" ' + password), ascii('body "x" ' + password)):
        escaped = representation[len('body "x" ') + 1 : -1]
        assert escaped not in decoded
    assert password.encode("unicode_escape").decode() not in decoded
    summary = next(step for step in steps if step.get("id") == "filtered_summary")
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", summary["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Prepared live test output:" in result.stdout
    assert "7 test case(s)" in result.stdout
    for value in (email, password, "FAKE-COOKIE-SESSIONKEY"):
        assert value not in result.stdout + result.stderr

    for value in (email, password, 'FAKE-session-雪<&"' + chr(39)):
        for encoded in encoded_forms(value).values():
            assert encoded not in decoded
            assert encoded not in (tmp_path / "live.log").read_text()


def test_upload_requires_successful_output_filtering(workflow):
    steps = workflow["jobs"]["live_tests"]["steps"]
    prepare = next((s for s in steps if s.get("id") == "filter_output"), None)
    assert prepare is not None
    assert "ZLIBRARY_TEST_SESSION_VALUES" not in workflow["jobs"]["live_tests"].get(
        "env", {}
    )
    live = next(step for step in steps if step.get("id") == "live")
    assert live["env"]["ZLIBRARY_TEST_SESSION_VALUES"] == (
        "${{ runner.temp }}/zlibrary-live-sessions.jsonl"
    )
    assert prepare["if"] == "always()"
    assert prepare["env"] == {
        "ZLIBRARY_TEST_SESSION_VALUES": "${{ runner.temp }}/zlibrary-live-sessions.jsonl",
        "ZLIBRARY_EMAIL": "${{ secrets.ZLIBRARY_CI_EMAIL }}",
        "ZLIBRARY_PASSWORD": "${{ secrets.ZLIBRARY_CI_PASSWORD }}",
    }
    assert prepare["run"].strip() == "python3 scripts/filter_live_test_output.py"
    uploads = [
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    ]
    assert len(uploads) == 1
    assert steps.index(prepare) < steps.index(uploads[0])
    assert uploads[0]["if"] == "always() && steps.filter_output.outcome == 'success'"
    assert uploads[0]["with"]["path"].split() == ["live.log", "live.xml"]


@pytest.mark.parametrize("case", ["missing", "unreadable", "replacement"])
def test_filter_skips_absent_files_and_fails_closed(workflow, tmp_path, case):
    if case == "unreadable":
        (tmp_path / "live.log").mkdir()
    elif case == "replacement":
        (tmp_path / "live.log").write_text("[filtered]")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={
            "ZLIBRARY_EMAIL": "fake@example.test" if case == "replacement" else "",
            "ZLIBRARY_PASSWORD": "[filtered]",
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == (0 if case == "missing" else 1)
    assert "[filtered]" not in run.stdout + run.stderr
    if case == "missing":
        assert not (tmp_path / "live.log").exists()
        assert not (tmp_path / "live.xml").exists()


@pytest.mark.parametrize(
    "value", ["CANARY<&\"'-OUTPUT-456", "CANARY-é雪<&\"'-OUTPUT-789"]
)
def test_parsed_attributes_text_cdata_and_tails_are_filtered(tmp_path, value):
    (tmp_path / "live.xml").write_text(
        f"<testsuites label={quoteattr(value)}><testcase>{escape(value)}"
        f"<system-out><![CDATA[{value}]]></system-out>{escape(value)}"
        "</testcase></testsuites>",
        encoding="utf-8",
    )
    (tmp_path / "live.log").write_text(value, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_EMAIL": "fake@example.test", "ZLIBRARY_PASSWORD": value},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert value not in run.stdout + run.stderr
    assert value not in (tmp_path / "live.log").read_text()
    tree = ET.parse(tmp_path / "live.xml")
    assert tree.getroot().attrib["label"] == "[filtered]"
    assert tree.find("testcase").text == "[filtered]"
    assert tree.find("testcase/system-out").text == "[filtered]"
    assert tree.find("testcase/system-out").tail == "[filtered]"
    assert value not in "".join(tree.getroot().itertext())


@pytest.mark.parametrize(
    "data",
    [
        b"<testsuites>broken",
        b"\xff",
        b'<?xml version="1.0" encoding="unknown-codec"?><testsuites/>',
    ],
)
def test_unparseable_xml_is_removed_and_upload_preparation_fails(tmp_path, data):
    (tmp_path / "live.xml").write_bytes(data)
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_PASSWORD": "CANARY-INVALID-XML"},
        capture_output=True,
        text=True,
    )
    assert run.returncode != 0
    assert not (tmp_path / "live.xml").exists()
    assert "CANARY-INVALID-XML" not in run.stdout + run.stderr
    assert "Could not prepare test output for upload." in run.stdout + run.stderr


@pytest.mark.parametrize("name", ["live.log", "live.xml"])
def test_verification_rereads_written_files(monkeypatch, tmp_path, name):
    value = "CANARY-AFTER-WRITE-é雪<&\"'"
    spec = importlib.util.spec_from_file_location(
        "filter_output_under_test", ROOT / "scripts/filter_live_test_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "fake@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", value)
    (tmp_path / "live.log").write_text(value, encoding="utf-8")
    (tmp_path / "live.xml").write_text("<testsuites/>")
    write = Path.write_bytes

    def changed_after_write(path, data):
        result = write(path, data)
        if path.name == name:
            if name == "live.log":
                write(path, data + value.encode("utf-8"))
            else:
                root = ET.fromstring(data)
                root.set("test-output", value)
                write(path, ET.tostring(root, encoding="utf-8"))
        return result

    monkeypatch.setattr(Path, "write_bytes", changed_after_write)
    assert module.filter_output() == 1


@pytest.mark.parametrize("name", ["live.log", "live.xml"])
def test_verification_rechecks_initially_absent_files(monkeypatch, tmp_path, name):
    value = "CANARY-APPEARED-AFTER-FIRST-READ"
    spec = importlib.util.spec_from_file_location(
        "filter_output_absent_under_test", ROOT / "scripts/filter_live_test_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "fake@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", value)
    if name == "live.log":
        read = Path.read_bytes
        first = True

        def appearing_log(path):
            nonlocal first
            if path.name == "live.log" and first:
                first = False
                path.write_text("benign output that was not filtered")
                raise FileNotFoundError
            return read(path)

        monkeypatch.setattr(Path, "read_bytes", appearing_log)
    else:
        read = module.read_xml
        first = True

        def appearing_xml(path):
            nonlocal first
            result = read(path)
            if first:
                first = False
                path.write_text(
                    '<testsuites label="benign output that was not filtered"/>'
                )
            return result

        monkeypatch.setattr(module, "read_xml", appearing_xml)
    assert module.filter_output() == 1


def encoded_forms(value):
    # Independent encoder calls, not the implementation's pattern generator.
    return {
        "literal": value,
        "html-quotes": html.escape(value),
        "html-no-quotes": html.escape(value, quote=False),
        "json-ascii": json.dumps(value)[1:-1],
        "json-unicode": json.dumps(value, ensure_ascii=False)[1:-1],
        "repr": repr(value)[1:-1],
        "ascii-repr": ascii(value)[1:-1],
        "url": quote(value, safe=""),
        "url-plus": quote_plus(value),
        "base64": base64.b64encode(value.encode()).decode(),
    }


@pytest.mark.parametrize("encoder", list(encoded_forms("seed")))
@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_encoded_credentials_are_replaced(tmp_path, encoder, filename):
    value = "FAKE-é雪< &\"'\\-encoded"
    encoded = encoded_forms(value)[encoder]
    content = (
        encoded
        if filename.endswith("log")
        else f"<testsuites><testcase message={quoteattr(encoded)}>{escape(encoded)}</testcase></testsuites>"
    )
    (tmp_path / filename).write_text(content)
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={"ZLIBRARY_EMAIL": "fake@example.test", "ZLIBRARY_PASSWORD": value},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    written = (tmp_path / filename).read_text()
    assert encoded not in written
    if filename.endswith("xml"):
        root = ET.fromstring(written)
        assert encoded not in "".join(root.itertext())
        assert encoded not in root.find("testcase").get("message")


@pytest.mark.parametrize("encoder", list(encoded_forms("seed")))
def test_session_values_in_login_replies_are_replaced(tmp_path, encoder):
    session = "FAKE-session-é雪🔒<&\"'\\-value"
    reply = repr(
        {"success": 1, "user": {"id": "FAKE-user-9191", "remix_userkey": session}}
    )
    encoded = encoded_forms(session)[encoder]
    (tmp_path / "live.log").write_text(reply + "\n" + encoded)
    (tmp_path / "live.xml").write_text(
        f"<testsuites><testcase message={quoteattr(encoded)}>{escape(json.dumps({'remix_userkey': session}))}</testcase></testsuites>"
    )
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env={
            "ZLIBRARY_EMAIL": "fake@example.test",
            "ZLIBRARY_PASSWORD": "FAKE-password",
        },
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert encoded not in (tmp_path / "live.log").read_text()
    tree = ET.parse(tmp_path / "live.xml")
    assert encoded not in tree.find("testcase").get("message")
    assert session not in "".join(tree.getroot().itertext())
    assert "FAKE-user-9191" not in (tmp_path / "live.log").read_text()


@pytest.mark.parametrize("missing", ["ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD"])
@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_existing_output_requires_both_values(tmp_path, missing, filename):
    (tmp_path / filename).write_text(
        "benign" if filename.endswith("log") else "<testsuites/>"
    )
    env = {"ZLIBRARY_EMAIL": "fake@example.test", "ZLIBRARY_PASSWORD": "FAKE-password"}
    env.pop(missing)
    run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/filter_live_test_output.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    assert "Could not prepare test output for upload." in run.stdout + run.stderr


def load_filter(monkeypatch, tmp_path, value):
    spec = importlib.util.spec_from_file_location(
        "filter_adversarial", ROOT / "scripts/filter_live_test_output.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZLIBRARY_EMAIL", "fake@example.test")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", value)
    return module


@pytest.mark.parametrize("location", ["tag", "attribute-name", "joined-text"])
def test_xml_recheck_rejects_unreplaceable_locations(monkeypatch, tmp_path, location):
    value = "FAKE_UNREPLACEABLE"
    content = {
        "tag": f"<testsuites><{value}/></testsuites>",
        "attribute-name": f'<testsuites {value}="benign"/>',
        "joined-text": "<testsuites>FAKE_<part/>UNREPLACEABLE</testsuites>",
    }[location]
    (tmp_path / "live.xml").write_text(content)
    assert load_filter(monkeypatch, tmp_path, value).filter_output() == 1


@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_recheck_rejects_vanished_output(monkeypatch, tmp_path, filename):
    module = load_filter(monkeypatch, tmp_path, "FAKE-password")
    (tmp_path / filename).write_text(
        "benign" if filename.endswith("log") else "<testsuites/>"
    )
    write = Path.write_bytes

    def vanish(path, data):
        result = write(path, data)
        if path.name == filename:
            path.unlink()
        return result

    monkeypatch.setattr(Path, "write_bytes", vanish)
    assert module.filter_output() == 1


@pytest.mark.parametrize("location", ["text", "tail"])
def test_xml_recheck_reads_text_and_tails_after_writing(
    monkeypatch, tmp_path, location
):
    value = "FAKE-AFTER-WRITE-CONTENT"
    module = load_filter(monkeypatch, tmp_path, value)
    (tmp_path / "live.xml").write_text("<testsuites><part/></testsuites>")
    write = Path.write_bytes

    def contaminate(path, data):
        if path.name == "live.xml":
            root = ET.fromstring(data)
            setattr(root if location == "text" else root.find("part"), location, value)
            data = ET.tostring(root)
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", contaminate)
    assert module.filter_output() == 1


SESSION_PRINTS = [
    "Cookie: remix_userid=987654; remix_userkey=FAKESESSIONKEY",
    "<Cookies[<Cookie remix_userkey=FAKESESSIONKEY for 127.0.0.1 />]>",
    "?remix_userid=987654&remix_userkey=FAKESESSIONKEY",
    r"{\"remix_userkey\": \"FAKESESSIONKEY\"}",
    "remix_userkey=b'FAKESESSIONKEY'\nFAKESESSIONKEY",
    "{'remix_userid': 987654, 'remix_userkey': 'FAKESESSIONKEY'}",
    "{'user': {'name': 'n', 'id': '987654', 'remix_userkey': 'FAKESESSIONKEY'}}",
    "{'user': {'details': {'name': 'n'}, 'id': '987654', 'remix_userkey': 'FAKESESSIONKEY'}}",
]


@pytest.mark.parametrize("printed", SESSION_PRINTS)
@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_session_printed_shapes_are_filtered_in_each_file(
    monkeypatch, tmp_path, printed, filename
):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    content = (
        printed
        if filename.endswith("log")
        else f"<testsuites><testcase message={quoteattr(printed)}>{escape(printed)}</testcase></testsuites>"
    )
    (tmp_path / filename).write_text(content)
    assert module.filter_output() == 0
    written = (tmp_path / filename).read_text()
    assert "FAKESESSIONKEY" not in written
    assert "987654" not in written
    if filename.endswith("xml"):
        root = ET.fromstring(written)
        assert "FAKESESSIONKEY" not in "".join(root.itertext())
        assert "987654" not in root.find("testcase").get("message")


@pytest.mark.parametrize(
    "printed",
    [
        "remix_userkey=",
        'remix_userkey={"unknown": "value"}',
        "remix_userkey=[unrecognized]",
        r"remix_userkey='\u12'",
    ],
)
def test_named_session_fields_fail_closed_for_unrecognized_values(
    monkeypatch, tmp_path, printed
):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    (tmp_path / "live.log").write_text(printed)
    assert module.filter_output() == 1


@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_named_session_recheck_rejects_a_new_value_after_writing(
    monkeypatch, tmp_path, filename
):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    (tmp_path / filename).write_text(
        "benign" if filename.endswith("log") else "<testsuites/>"
    )
    write = Path.write_bytes

    def contaminate(path, data):
        if path.name == filename:
            if filename.endswith("log"):
                data += b"\nremix_userkey=NEW-UNSEEN-SESSION"
            else:
                root = ET.fromstring(data)
                root.set("message", "remix_userkey=NEW-UNSEEN-SESSION")
                data = ET.tostring(root)
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", contaminate)
    assert module.filter_output() == 1


@pytest.mark.parametrize("value", ["CANARY'ONLY-PW", "CANARY-é雪'ONLY-PW"])
@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_repr_escaping_comes_from_the_surrounding_string(
    monkeypatch, tmp_path, value, filename
):
    module = load_filter(monkeypatch, tmp_path, value)
    prefix = 'body "x" '
    # Real repr/ascii of surrounding text force an apostrophe escape that
    # repr(value) alone omits. These expectations come from the actual writer.
    representations = [repr(prefix + value), ascii(prefix + value)]
    printed = "\n".join(representations)
    assert "\\'ONLY-PW" in printed
    (tmp_path / filename).write_text(
        printed
        if filename.endswith("log")
        else f"<testsuites message={quoteattr(printed)}>{escape(printed)}</testsuites>"
    )
    assert module.filter_output() == 0
    written = (tmp_path / filename).read_text()
    decoded = (
        written if filename.endswith("log") else ET.fromstring(written).get("message")
    )
    for representation in representations:
        escaped = representation[len(prefix) + 1 : -1]
        assert escaped not in decoded


def test_session_is_discovered_from_xml_only(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    value = 'XML-ONLY-SESSION-雪<&"'
    printed = json.dumps({"remix_userkey": value})
    (tmp_path / "live.xml").write_text(f"<testsuites>{escape(printed)}</testsuites>")
    assert not (tmp_path / "live.log").exists()
    assert module.filter_output() == 0
    assert value not in "".join(ET.parse(tmp_path / "live.xml").getroot().itertext())


def test_html_escaped_session_discovery_is_independent(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    value = 'HTML-ONLY-SESSION-雪<&"'
    printed = html.escape(repr({"remix_userkey": value}))
    assert "&#x27;remix_userkey&#x27;" in printed
    (tmp_path / "live.log").write_text(printed)
    assert module.filter_output() == 0
    assert html.escape(value) not in (tmp_path / "live.log").read_text()
    assert "[filtered]" in html.unescape((tmp_path / "live.log").read_text())


@pytest.mark.parametrize("filename", ["live.log", "live.xml"])
def test_html_wrapped_encodings_are_filtered_independently(
    monkeypatch, tmp_path, filename
):
    value = 'WRAPPED-é雪<&"\\-VALUE'
    module = load_filter(monkeypatch, tmp_path, value)
    encoded = html.escape(json.dumps(value)[1:-1])
    (tmp_path / filename).write_text(
        encoded
        if filename.endswith("log")
        else f"<testsuites message={quoteattr(encoded)}/>"
    )
    assert module.filter_output() == 0
    written = (tmp_path / filename).read_text()
    decoded = (
        written if filename.endswith("log") else ET.fromstring(written).get("message")
    )
    assert decoded == "[filtered]"


def test_longer_values_are_replaced_before_their_prefix(monkeypatch, tmp_path):
    short = "FAKE-OVERLAP"
    long = short + "-PRIVATE-SUFFIX"
    module = load_filter(monkeypatch, tmp_path, long)
    monkeypatch.setenv("ZLIBRARY_EMAIL", short)
    (tmp_path / "live.log").write_text(long + " " + short)
    assert module.filter_output() == 0
    assert (tmp_path / "live.log").read_text() == "[filtered] [filtered]"


def test_real_pytest_parameter_id_forms_are_filtered(monkeypatch, tmp_path):
    value = "PARAM-é雪<&\"'"
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_ids.py").write_text(
        "import os, pytest\n@pytest.mark.parametrize('argument', [os.environ['ZLIBRARY_PASSWORD']])\ndef test_id(argument):\n    assert False\n"
    )
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--no-cov",
            "--tb=short",
            "--junitxml=live.xml",
            "-q",
        ],
        cwd=tmp_path,
        env={**__import__("os").environ, "ZLIBRARY_PASSWORD": value},
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    (tmp_path / "live.log").write_text(run.stdout + run.stderr)
    escaped_id = value.encode("unicode_escape").decode()
    before = ET.parse(tmp_path / "live.xml").find(".//testcase").get("name")
    assert escaped_id in before
    module = load_filter(monkeypatch, tmp_path, value)
    assert module.filter_output() == 0
    assert escaped_id not in ET.parse(tmp_path / "live.xml").find(".//testcase").get(
        "name"
    )
    assert escaped_id not in (tmp_path / "live.log").read_text()


def test_filter_test_description_matches_replacement_contract():
    import ast

    description = ast.get_docstring(ast.parse(Path(__file__).read_text()))
    assert (
        description
        == "Replace sensitive values in uploaded test output using synthetic values only."
    )


@pytest.mark.parametrize(
    "residual",
    [
        "[filtered]-suffix",
        "'[filtered]-suffix'",
        "[filtered]'suffix",
        "[filtered]&suffix",
        "[filtered];suffix",
    ],
)
def test_named_session_recheck_requires_the_exact_replacement(
    monkeypatch, tmp_path, residual
):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    (tmp_path / "live.log").write_text("benign")
    write = Path.write_bytes

    def contaminate(path, data):
        if path.name == "live.log":
            data += ("\nremix_userkey=" + residual).encode()
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", contaminate)
    assert module.filter_output() == 1


def test_excessive_escaped_field_layers_reject_output(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    # Eight backslashes still leave a named escaped field after four passes.
    printed = "{" + "\\" * 8 + '"token' + "\\" * 8 + '": "FAKE-DEEP"}'
    (tmp_path / "live.log").write_text(printed)
    assert module.filter_output() == 1


def test_uppercase_session_field_filters_a_later_bare_value(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    (tmp_path / "live.log").write_text("REMIX_USERKEY=FAKE-UPPERCASE\nFAKE-UPPERCASE")
    assert module.filter_output() == 0
    assert "FAKE-UPPERCASE" not in (tmp_path / "live.log").read_text()


def test_escaped_user_id_is_discovered_without_a_session_key(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    printed = r"{\"user\": {\"id\": \"FAKE-ESCAPED-ID\"}}"
    (tmp_path / "live.log").write_text(printed + "\nFAKE-ESCAPED-ID")
    assert module.filter_output() == 0
    assert "FAKE-ESCAPED-ID" not in (tmp_path / "live.log").read_text()


def test_quoted_closing_brace_does_not_end_user_object(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    printed = '{"user": {"name": "a } b", "id": "FAKE-AFTER-BRACE"}}'
    (tmp_path / "live.log").write_text(printed + "\nFAKE-AFTER-BRACE")
    assert module.filter_output() == 0
    assert "FAKE-AFTER-BRACE" not in (tmp_path / "live.log").read_text()


@pytest.mark.parametrize("field", ["auth_token", "session_token"])
def test_prefixed_token_fields_filter_their_bare_values(monkeypatch, tmp_path, field):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    (tmp_path / "live.log").write_text(
        f"{field}=FAKE-PREFIXED-TOKEN\nFAKE-PREFIXED-TOKEN"
    )
    assert module.filter_output() == 0
    assert "FAKE-PREFIXED-TOKEN" not in (tmp_path / "live.log").read_text()


@pytest.mark.parametrize("printed", ["assert c.remix_userkey == 'x'", "token := x"])
def test_source_operators_do_not_become_session_values(monkeypatch, tmp_path, printed):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    original = printed + "\nordinary = output\n"
    (tmp_path / "live.log").write_text(original)
    assert module.filter_output() == 0
    assert (tmp_path / "live.log").read_text() == original


def test_session_value_cannot_start_with_equals(monkeypatch, tmp_path):
    module = load_filter(monkeypatch, tmp_path, "FAKE-CONFIGURED-PASSWORD")
    with pytest.raises(ValueError):
        module.field_value("=unsafe", 0)
