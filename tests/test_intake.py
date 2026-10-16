"""The f1-stream report intake: ``POST /v1/intake/f1-report``.

The intake reads text a stranger typed, in a pod that holds forge tokens, a
Vault token and a service account. What stands between that text and those
credentials is how the run is started: the agent's file-reading tools, no
permission bypass, no user or project settings that could widen a Read, and an
environment holding nothing but the Claude credential. These tests pin each of
those down, along with the endpoint's own contract (its token, its limits, its
job records).
"""
import asyncio
import base64
import json
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import intake
from app.main import app

TOKEN = "intake-token"

AGENT_MD = """\
---
name: f1-report-intake
description: The Intake.
model: sonnet
tools: Read, Grep, Glob
---

You are the Intake.

Answer with JSON.
"""


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    intake.records.clear()
    intake.semaphore = asyncio.Semaphore(1)
    agent_file = tmp_path / "f1-report-intake.md"
    agent_file.write_text(AGENT_MD)
    monkeypatch.setenv(intake.ENV_AGENT_FILE, str(agent_file))
    monkeypatch.setenv(intake.ENV_TOKEN, TOKEN)
    yield
    intake.records.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _body(**files) -> dict:
    return {"report_id": 42, "files": files or {"report.json": {"text": "{}"}}}


# --------------------------------------------------------------------------- #
# Auth.
# --------------------------------------------------------------------------- #
def test_an_unset_token_refuses_everything_with_503(client, monkeypatch):
    monkeypatch.delenv(intake.ENV_TOKEN)
    r = client.post("/v1/intake/f1-report", json=_body(), headers=_auth(""))
    assert r.status_code == 503
    r = client.get("/v1/intake/f1-report/abc", headers=_auth(""))
    assert r.status_code == 503


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"},
                                     {"Authorization": TOKEN}])
def test_a_wrong_or_missing_token_is_401(client, headers):
    with patch.object(intake, "start") as start:
        r = client.post("/v1/intake/f1-report", json=_body(), headers=headers)
    assert r.status_code == 401
    start.assert_not_called()


def test_the_execute_token_does_not_open_the_intake(client):
    """The backend holds only the intake token; the global one is not a key here."""
    r = client.post("/v1/intake/f1-report", json=_body(),
                    headers=_auth(os.environ["API_BEARER_TOKEN"]))
    assert r.status_code == 401


def test_the_status_endpoint_needs_the_token_too(client):
    intake.records["job1"] = {"status": "running", "result": "", "error": "",
                              "finished_epoch": None}
    assert client.get("/v1/intake/f1-report/job1").status_code == 401
    assert client.get("/v1/intake/f1-report/job1", headers=_auth()).status_code == 200


# --------------------------------------------------------------------------- #
# Validation.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "Report.json", "../x", "report/x", "a b", ".hidden", "..", ".", "",
    "x_y.json", "évidence.md",
])
def test_file_names_outside_the_allowed_set_are_refused(client, name):
    with patch.object(intake, "start") as start:
        r = client.post("/v1/intake/f1-report",
                        json={"report_id": 1, "files": {name: {"text": "x"}}},
                        headers=_auth())
    assert r.status_code == 400
    start.assert_not_called()


def test_more_than_ten_files_is_refused(client):
    files = {f"f{i}.md": {"text": "x"} for i in range(11)}
    r = client.post("/v1/intake/f1-report", json={"report_id": 1, "files": files},
                    headers=_auth())
    assert r.status_code == 400


def test_more_than_8_mb_in_total_is_refused(client):
    big = base64.b64encode(b"\0" * (4 * 1024 * 1024 + 1)).decode()
    files = {"a.jpg": {"base64": big}, "b.jpg": {"base64": big}}
    r = client.post("/v1/intake/f1-report", json={"report_id": 1, "files": files},
                    headers=_auth())
    assert r.status_code == 400


@pytest.mark.parametrize("entry", [
    {"base64": "!!not base64!!"},
    {"text": "a", "base64": "YQ=="},
    {},
])
def test_a_file_must_be_exactly_one_of_text_or_valid_base64(client, entry):
    r = client.post("/v1/intake/f1-report",
                    json={"report_id": 1, "files": {"a.md": entry}}, headers=_auth())
    assert r.status_code == 400


def test_decode_files_returns_the_bytes_to_write():
    out = intake.decode_files({
        "report.json": intake.IntakeFile(text='{"words": "hé"}'),
        "screenshot.jpg": intake.IntakeFile(base64=base64.b64encode(b"\xff\xd8").decode()),
    })
    assert out == {"report.json": '{"words": "hé"}'.encode(), "screenshot.jpg": b"\xff\xd8"}


# --------------------------------------------------------------------------- #
# The job lifecycle over HTTP.
# --------------------------------------------------------------------------- #
def test_a_valid_report_is_accepted_and_runs_as_a_job(client):
    seen = {}

    async def fake_run(job_id, files):
        seen["files"] = files
        intake.finish(job_id, "completed", result='{"verdict": "spam"}')

    with patch.object(intake, "run", new=fake_run):
        r = client.post("/v1/intake/f1-report",
                        json=_body(**{"report.json": {"text": "{}"}}), headers=_auth())
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        status = client.get(f"/v1/intake/f1-report/{job_id}", headers=_auth()).json()
    assert status == {"status": "completed", "result": '{"verdict": "spam"}', "error": ""}
    assert seen["files"] == {"report.json": b"{}"}


def test_an_unknown_job_is_404(client):
    assert client.get("/v1/intake/f1-report/nope", headers=_auth()).status_code == 404


def test_finished_records_are_kept_for_an_hour_and_then_dropped(client):
    intake.records["old"] = {"status": "completed", "result": "x", "error": "",
                             "finished_epoch": time.time() - intake.RECORD_TTL_SECONDS - 1}
    intake.records["new"] = {"status": "completed", "result": "y", "error": "",
                             "finished_epoch": time.time() - 60}
    intake.records["live"] = {"status": "running", "result": "", "error": "",
                              "finished_epoch": None}
    assert client.get("/v1/intake/f1-report/old", headers=_auth()).status_code == 404
    assert client.get("/v1/intake/f1-report/new", headers=_auth()).status_code == 200
    assert client.get("/v1/intake/f1-report/live", headers=_auth()).status_code == 200


# --------------------------------------------------------------------------- #
# The run: argv, environment, files, prompt.
# --------------------------------------------------------------------------- #
def _agent() -> dict:
    return intake.parse_agent(AGENT_MD)


def test_the_agent_file_is_parsed_into_an_inline_definition():
    agent = _agent()
    assert agent == {
        "description": "The Intake.",
        "prompt": "You are the Intake.\n\nAnswer with JSON.",
        "tools": ["Read", "Grep", "Glob"],
        "model": "sonnet",
    }


def test_argv_is_locked_down():
    argv = intake.argv(_agent(), model=None)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--agent") + 1] == "f1-report-intake"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "--strict-mcp-config" in argv
    assert "--disable-slash-commands" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--max-budget-usd") + 1] == "2"
    assert "--model" not in argv


def test_argv_never_bypasses_permissions():
    argv = intake.argv(_agent(), model="haiku")
    assert not any("skip-permissions" in a or "bypass" in a.lower() for a in argv)
    # Pinned, so a settings default (auto, bypass) cannot apply.
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_argv_loads_no_settings_files_and_defines_the_agent_inline():
    """A user or project settings file can grant Read outside the workspace
    (measured on 2.1.300: an allow rule in ~/.claude/settings.json let the agent
    read /etc/hostname). Loading none of them, and defining the agent on the
    command line, keeps the agent and drops every such rule."""
    argv = intake.argv(_agent(), model=None)
    assert argv[argv.index("--setting-sources") + 1] == ""
    defined = json.loads(argv[argv.index("--agents") + 1])
    assert defined["f1-report-intake"]["tools"] == ["Read", "Grep", "Glob"]


def test_argv_takes_the_prompt_on_stdin_not_argv():
    argv = intake.argv(_agent(), model=None)
    assert intake.prompt(["report.json"]) not in argv


def test_a_model_override_is_passed_through():
    argv = intake.argv(_agent(), model="haiku")
    assert argv[argv.index("--model") + 1] == "haiku"


def test_an_agent_with_other_tools_is_refused():
    """The intake's safety rests on the tool list, so a definition that widened
    it must not run, whatever the file on disk says."""
    widened = AGENT_MD.replace("tools: Read, Grep, Glob", "tools: Read, Grep, Glob, Bash")
    with pytest.raises(ValueError):
        intake.argv(intake.parse_agent(widened), model=None)


SECRETS = {
    "FORGEJO_TOKEN": "f", "FIXER_FORGEJO_TOKEN": "ff", "GITHUB_TOKEN": "g",
    "VAULT_TOKEN": "v", "API_BEARER_TOKEN": "a", "INTAKE_API_TOKEN": "i",
    "FIXER_WEBHOOK_SECRET": "w", "MEMORY_API_KEY": "m", "HA_MCP_URL": "h",
    "KUBERNETES_SERVICE_HOST": "10.0.0.1", "WOODPECKER_API_TOKEN": "wp",
    "SLACK_WEBHOOK_URL": "s", "FIXER_NTFY_TOKEN": "n", "VAULT_ADDR": "x",
}


def test_the_environment_holds_only_path_home_lang_and_the_claude_credential():
    environ = {"PATH": "/usr/bin", "HOME": "/home/agent", "LANG": "C.UTF-8",
               "CLAUDE_CODE_OAUTH_TOKEN": "oauth", **SECRETS}
    env = intake.scrubbed_env(environ)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/agent", "LANG": "C.UTF-8",
                   "CLAUDE_CODE_OAUTH_TOKEN": "oauth"}


def test_absent_variables_are_left_out_rather_than_set_empty():
    env = intake.scrubbed_env({"PATH": "/bin"})
    assert env == {"PATH": "/bin"}


def test_files_are_written_under_report(tmp_path):
    intake.write_files(str(tmp_path), {"report.json": b"{}", "screenshot.jpg": b"\xff"})
    assert (tmp_path / "report" / "report.json").read_bytes() == b"{}"
    assert (tmp_path / "report" / "screenshot.jpg").read_bytes() == b"\xff"


def test_the_prompt_lists_the_files_and_asks_for_one_json_object():
    text = intake.prompt(["evidence.md", "report.json", "screenshot.jpg"])
    for name in ("report/evidence.md", "report/report.json", "report/screenshot.jpg"):
        assert name in text
    assert "docs/issue-bar.md" in text
    assert "JSON object" in text
    assert "data" in text


def test_the_result_text_comes_from_the_json_result():
    out = json.dumps({"type": "result", "is_error": False, "result": '{"verdict": "spam"}'})
    assert intake.result_text(out) == ('{"verdict": "spam"}', None)


def test_an_error_result_is_a_failure():
    out = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_budget_usd",
                      "result": ""})
    text, error = intake.result_text(out)
    assert error == "error_max_budget_usd"


def test_unparseable_output_is_a_failure():
    assert intake.result_text("boom")[1]


# --------------------------------------------------------------------------- #
# The run, end to end with the subprocesses mocked.
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = None
        self._rc = returncode
        self.stdin_data = b""

    async def communicate(self, data: bytes | None = None):
        self.stdin_data = data or b""
        self.returncode = self._rc
        return self._stdout, b""

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _spawner(calls: list, claude_out: bytes, clone_rc: int = 0):
    async def spawn(*argv, cwd=None, env=None, stdin=None, stdout=None, stderr=None):
        calls.append({"argv": list(argv), "cwd": cwd, "env": env})
        if argv[0] == "git":
            dest = argv[-1]
            os.makedirs(os.path.join(dest, "docs"), exist_ok=True)
            return _Proc(returncode=clone_rc)
        # The files must be in place when the agent starts.
        calls[-1]["report_files"] = sorted(os.listdir(os.path.join(cwd, "report")))
        proc = _Proc(stdout=claude_out)
        calls[-1]["proc"] = proc
        return proc
    return spawn


@pytest.mark.asyncio
async def test_a_run_clones_writes_runs_and_cleans_up(monkeypatch):
    monkeypatch.setenv("FIXER_FORGEJO_TOKEN", "clone-secret")
    monkeypatch.setenv("FORGEJO_TOKEN", "viktors")
    calls: list = []
    out = json.dumps({"type": "result", "is_error": False,
                      "result": '{"verdict": "feedback"}'}).encode()
    intake.records["j1"] = intake.new_record()
    with patch("app.intake.asyncio.create_subprocess_exec", new=_spawner(calls, out)):
        await intake.run("j1", {"report.json": b"{}", "evidence.md": b"e"})

    assert intake.records["j1"]["status"] == "completed"
    assert intake.records["j1"]["result"] == '{"verdict": "feedback"}'

    clone, claude = calls
    assert clone["argv"][:2] == ["git", "clone"]
    assert "--depth" in clone["argv"] and "master" in clone["argv"]
    # The token rides in a header from the environment: never on argv, so it is
    # neither in the process list nor in the clone's .git/config.
    assert not any("clone-secret" in a for a in clone["argv"])
    assert "Authorization: token clone-secret" in clone["env"].values()
    assert clone["env"]["GIT_CONFIG_GLOBAL"] == os.devnull

    assert claude["cwd"] == clone["argv"][-1]
    assert claude["report_files"] == ["evidence.md", "report.json"]
    assert "--dangerously-skip-permissions" not in claude["argv"]
    assert set(claude["env"]) <= {"PATH", "HOME", "LANG", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert b"report/report.json" in claude["proc"].stdin_data
    # The workspace is gone afterwards.
    assert not os.path.exists(claude["cwd"])


@pytest.mark.asyncio
async def test_a_failed_clone_fails_the_job_and_runs_no_agent(monkeypatch):
    calls: list = []
    intake.records["j2"] = intake.new_record()
    with patch("app.intake.asyncio.create_subprocess_exec",
               new=_spawner(calls, b"", clone_rc=128)):
        await intake.run("j2", {"report.json": b"{}"})
    assert intake.records["j2"]["status"] == "failed"
    assert "clone" in intake.records["j2"]["error"]
    assert [c["argv"][0] for c in calls] == ["git"]


@pytest.mark.asyncio
async def test_an_error_result_fails_the_job(monkeypatch):
    calls: list = []
    out = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_budget_usd",
                      "result": ""}).encode()
    intake.records["j3"] = intake.new_record()
    with patch("app.intake.asyncio.create_subprocess_exec", new=_spawner(calls, out)):
        await intake.run("j3", {"report.json": b"{}"})
    assert intake.records["j3"]["status"] == "failed"
    assert intake.records["j3"]["error"] == "error_max_budget_usd"


@pytest.mark.asyncio
async def test_a_run_past_the_timeout_is_marked_timeout(monkeypatch):
    monkeypatch.setattr(intake, "RUN_TIMEOUT_SECONDS", 0.05)

    async def slow(*a, **k):
        await asyncio.sleep(5)

    intake.records["j4"] = intake.new_record()
    with patch.object(intake, "_run_in_workspace", new=slow):
        await intake.run("j4", {"report.json": b"{}"})
    assert intake.records["j4"]["status"] == "timeout"


@pytest.mark.asyncio
async def test_intake_runs_one_at_a_time_on_its_own_semaphore():
    """Separate from the /execute permit, so a long fixer run never holds up an
    intake, and one intake at a time."""
    from app import main as app_main
    assert intake.semaphore is not app_main.execution_semaphore
    running = 0
    peak = 0

    async def body(workspace, files):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "{}", None

    for jid in ("a", "b", "c"):
        intake.records[jid] = intake.new_record()
    with patch.object(intake, "_run_in_workspace", new=body):
        await asyncio.gather(*(intake.run(j, {}) for j in ("a", "b", "c")))
    assert peak == 1
    assert all(intake.records[j]["status"] == "completed" for j in ("a", "b", "c"))


def test_the_dockerfile_seeds_the_agent():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "Dockerfile")) as fh:
        dockerfile = fh.read()
    assert ("COPY agents/f1-report-intake.md /usr/share/agent-seed/f1-report-intake.md"
            in dockerfile)
    assert intake.DEFAULT_AGENT_FILE == "/usr/share/agent-seed/f1-report-intake.md"


def test_the_shipped_agent_file_has_exactly_the_reading_tools():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "agents", "f1-report-intake.md")) as fh:
        agent = intake.parse_agent(fh.read())
    assert agent["tools"] == ["Read", "Grep", "Glob"]
    intake.argv(agent, model=None)  # does not raise
