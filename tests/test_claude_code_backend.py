"""Claude Code CLI fallback backend: selection, the command it runs, output parsing and cost logging.
The subprocess is mocked; no test runs the real CLI."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import config
from analysis.moat import moat_lens
from llm import claude_code
from llm.cache import LLMCache
from llm.claude_code import ClaudeCodeAPI, ClaudeCodeError, flatten_messages, parse_output, strip_text_format
from llm.client import LLMClient, choose_backend
from storage import llm_store
from tests.analysis_helpers import make_inputs
from tests.llm_fakes import MOAT_OK
from tests.screen_helpers import make_info


def cli_output(structured=None, **over):
    out = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(structured),
           "structured_output": structured, "total_cost_usd": 0.0123,
           "usage": {"input_tokens": 5, "cache_creation_input_tokens": 1200, "cache_read_input_tokens": 300,
                     "output_tokens": 250}}
    out.update(over)
    return json.dumps(out)


class Runner:
    """Records subprocess.run calls and replays canned CLI outputs."""

    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if isinstance(out, BaseException):
            raise out
        return SimpleNamespace(stdout=out, stderr="", returncode=0)


def cc_client(tmp_path, runner):
    return LLMClient(api=ClaudeCodeAPI("/opt/claude", runner=runner), cache=LLMCache(tmp_path / "c.db"),
                     db_path=tmp_path / "r.db")


# ---------------------------------------------------------------- selection
def test_api_key_selects_the_api(monkeypatch):
    monkeypatch.setattr(claude_code, "find_cli", lambda: "/opt/claude")
    api, backend = choose_backend(api_key="sk-test", backend="auto")
    assert backend == "api"


def test_no_key_falls_back_to_claude_code(monkeypatch):
    monkeypatch.setattr(claude_code, "find_cli", lambda: "/opt/claude")
    api, backend = choose_backend(api_key="", backend="auto")
    assert backend == "claude_code" and isinstance(api, ClaudeCodeAPI) and api.cli == "/opt/claude"


def test_neither_available_is_not_configured(monkeypatch):
    monkeypatch.setattr(claude_code, "find_cli", lambda: None)
    assert choose_backend(api_key="", backend="auto") == (None, "none")


@pytest.mark.parametrize("backend,key,cli,expected", [
    ("api", "", "/opt/claude", "none"),            # forced API without a key: no silent CLI use
    ("claude_code", "sk-test", "/opt/claude", "claude_code"),  # forced CLI even with a key
    ("none", "sk-test", "/opt/claude", "none"),
])
def test_forced_backends(monkeypatch, backend, key, cli, expected):
    monkeypatch.setattr(claude_code, "find_cli", lambda: cli)
    assert choose_backend(api_key=key, backend=backend)[1] == expected


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError):
        choose_backend(api_key="", backend="gpt")


def test_find_cli_prefers_explicit_path(monkeypatch, tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_CLI", str(exe))
    assert claude_code.find_cli() == str(exe)
    monkeypatch.setenv("CLAUDE_CODE_CLI", str(tmp_path / "missing"))
    assert claude_code.find_cli() is None


def test_find_cli_globs_the_vscode_extension(monkeypatch, tmp_path):
    exe = tmp_path / "ext" / "anthropic.claude-code-9.9.9-linux-x64" / "resources" / "native-binary" / "claude"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.delenv("CLAUDE_CODE_CLI", raising=False)
    monkeypatch.setattr(config, "CLAUDE_CODE_CLI", "")
    monkeypatch.setattr(claude_code.shutil, "which", lambda _: None)
    monkeypatch.setattr(config, "CLAUDE_CODE_CLI_GLOBS",
                        [str(tmp_path / "ext" / "anthropic.claude-code-*/resources/native-binary/claude")])
    assert claude_code.find_cli() == str(exe)


# ---------------------------------------------------------------- the command
def test_command_is_isolated_and_carries_system_and_schema(tmp_path):
    runner = Runner(cli_output(MOAT_OK))
    moat_lens(make_inputs(info=make_info(business_summary="Acme makes widgets.")), cc_client(tmp_path, runner))
    cmd, kw = runner.calls[0]
    assert cmd[:2] == ["/opt/claude", "-p"]
    flag = lambda name: cmd[cmd.index(name) + 1]  # noqa: E731
    assert flag("--output-format") == "json" and flag("--tools") == "" and flag("--setting-sources") == ""
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert flag("--model") == config.ANTHROPIC_MODEL and flag("--effort") == config.LLM_EFFORT
    schema = json.loads(flag("--json-schema"))
    assert "evidence" in schema["properties"] and '"format": "text"' not in json.dumps(schema)
    # The fixed system prompt goes in the flag; third-party text only on stdin, inside its data block.
    assert "Acme makes widgets" not in flag("--system-prompt") and "moat" in flag("--system-prompt")
    assert "<business_summary>\nAcme makes widgets.\n</business_summary>" in kw["input"]
    assert "ANTHROPIC_API_KEY" not in kw["env"] and kw["cwd"] != str(config.ROOT)


def test_retry_is_flattened_into_one_prompt(tmp_path):
    one_fact = {**MOAT_OK, "evidence": MOAT_OK["evidence"][:1]}
    runner = Runner(cli_output(one_fact), cli_output(MOAT_OK))
    r = moat_lens(make_inputs(), cc_client(tmp_path, runner))
    assert r.ok and len(runner.calls) == 2
    second = runner.calls[1][1]["input"]
    assert "<previous_answer>" in second and "rejected" in second


def test_flatten_messages():
    msgs = [{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A"},
            {"role": "user", "content": "Fix it"}]
    assert flatten_messages(msgs) == "Q\n\nYour previous answer was:\n<previous_answer>\nA\n</previous_answer>\n\nFix it"


def test_strip_text_format_keeps_real_formats():
    s = {"properties": {"a": {"type": "string", "format": "text"}, "d": {"type": "string", "format": "date"}}}
    assert strip_text_format(s) == {"properties": {"a": {"type": "string"}, "d": {"type": "string", "format": "date"}}}


# ---------------------------------------------------------------- output and logging
def test_parse_output_success():
    resp = parse_output(cli_output({"x": 1}))
    assert json.loads(resp.content[0].text) == {"x": 1}
    assert resp.usage.input_tokens == 1505 and resp.usage.output_tokens == 250
    assert resp.billed_cost == 0.0 and resp.list_price_cost == pytest.approx(0.0123)


@pytest.mark.parametrize("stdout", [cli_output(None, is_error=True, subtype="error_max_turns"), "not json at all"])
def test_parse_output_errors(stdout):
    with pytest.raises(ClaudeCodeError):
        parse_output(stdout)


def test_subscription_calls_log_tokens_zero_billed_cost_and_list_price(tmp_path):
    llm = cc_client(tmp_path, Runner(cli_output(MOAT_OK)))
    r = moat_lens(make_inputs(), llm)
    assert r.ok and r.cost == 0.0
    moat_lens(make_inputs(), llm)  # cache hit
    calls = llm_store.calls(path=tmp_path / "r.db")
    assert [(c.backend, c.cache_hit) for c in calls] == [("claude_code", False), ("claude_code", True)]
    assert calls[0].cost == 0.0 and calls[0].list_price_cost == pytest.approx(0.0123)
    assert calls[0].input_tokens == 1505 and calls[0].output_tokens == 250
    assert calls[1].list_price_cost == 0.0
    assert llm_store.month_claude_code(calls[0].created_at.date(), path=tmp_path / "r.db") == (1, pytest.approx(0.0123))
    assert llm_store.month_spend(calls[0].created_at.date(), path=tmp_path / "r.db")[0] == 0.0


def test_cli_failure_and_timeout_become_insufficient_data(tmp_path):
    for failure in (cli_output(None, is_error=True, subtype="error_during_execution", result="login required"),
                    subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        r = moat_lens(make_inputs(), LLMClient(api=ClaudeCodeAPI("/opt/claude", runner=Runner(failure)),
                                               cache=LLMCache(tmp_path / f"c{id(failure)}.db"),
                                               db_path=tmp_path / "r.db"))
        assert not r.ok and r.status.startswith("Insufficient data - API error: ClaudeCodeError")


def test_existing_runs_db_is_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE llm_calls (call_id INTEGER PRIMARY KEY, analysis_id INTEGER, ticker TEXT, lens TEXT, "
                 "model TEXT, prompt_version TEXT, input_tokens INTEGER, output_tokens INTEGER, cost REAL, "
                 "cache_hit INTEGER, attempt INTEGER, outcome TEXT, created_at TEXT)")
    conn.commit()
    conn.close()
    llm_store.log_call(llm_store.LLMCallRecord(ticker="T", lens="moat", model="m", prompt_version="v",
                                               backend="claude_code", list_price_cost=0.5), db)
    assert llm_store.calls(path=db)[0].backend == "claude_code"


def test_6k_hits_are_confirmed_through_the_cli_fallback_without_a_key(monkeypatch, tmp_path):
    """Without an API key, the default 6-K confirm now uses Claude Code instead of leaving hits unconfirmed."""
    from datetime import date

    from data import leadership as ld
    from tests.llm_fakes import DEPARTURE_OK

    runner = Runner(cli_output(DEPARTURE_OK))
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")
    monkeypatch.setattr(claude_code, "find_cli", lambda: "/opt/claude")
    monkeypatch.setattr(claude_code.subprocess, "run", runner)
    doc = ld.FilingDoc(form="6-K", filing_date=date(2026, 5, 12), accession="acc-cli",
                       text="The Company announced that its Chief Executive Officer will step down.")
    c = ld.confirm_departure_llm(doc, "NMM.TO")
    assert c.status == "confirmed" and c.role == "CEO" and len(runner.calls) == 1
