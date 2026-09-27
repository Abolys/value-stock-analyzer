"""Claude Code CLI backend: the fallback when no ANTHROPIC_API_KEY is set (CLAUDE.md Rule 4).

`ClaudeCodeAPI` exposes the same `.messages.create(**params)` call the Anthropic
SDK does, so LLMClient's cache, validation, retry, evidence rule and logging are
unchanged. Each call runs one non-interactive `claude -p` with:

- the fixed system prompt (`--system-prompt`) and the JSON schema (`--json-schema`),
  the model from ANTHROPIC_MODEL and the configured effort;
- no tools, no settings, no MCP servers and no saved session, run from an empty
  temporary directory so no project CLAUDE.md or memory reaches the model;
- the user message (with its delimited data blocks) on stdin.

Calls run on the user's Claude subscription, so the billed API cost logged is $0;
the CLI's own list-price estimate is kept alongside as `list_price_cost`.
Temperature can't be set through the CLI; repeatability rests on the response cache.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any

import config

BACKEND = "claude_code"


class ClaudeCodeError(Exception):
    """The CLI failed, timed out, or returned no structured output."""


def find_cli() -> str | None:
    """CLAUDE_CODE_CLI, else `claude` on PATH, else the newest match of CLAUDE_CODE_CLI_GLOBS."""
    explicit = os.getenv("CLAUDE_CODE_CLI", config.CLAUDE_CODE_CLI)
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    found = [p for pattern in config.CLAUDE_CODE_CLI_GLOBS for p in glob.glob(os.path.expanduser(pattern))
             if os.access(p, os.X_OK)]
    return max(found, key=os.path.getmtime) if found else None


def strip_text_format(schema: Any) -> Any:
    """Drop the SDK's `"format": "text"` string annotations, which are not standard JSON Schema."""
    if isinstance(schema, dict):
        return {k: strip_text_format(v) for k, v in schema.items() if not (k == "format" and v == "text")}
    if isinstance(schema, list):
        return [strip_text_format(v) for v in schema]
    return schema


def flatten_messages(messages: list[dict[str, Any]]) -> str:
    """The CLI takes one prompt: the first user turn, then any retry (previous answer + feedback)."""
    parts = [str(messages[0]["content"])]
    for m in messages[1:]:
        if m["role"] == "assistant":
            parts.append(f"Your previous answer was:\n<previous_answer>\n{m['content']}\n</previous_answer>")
        else:
            parts.append(str(m["content"]))
    return "\n\n".join(parts)


class ClaudeCodeAPI:
    backend_name = BACKEND

    def __init__(self, cli: str, timeout: float | None = None, runner=None):
        self.cli = cli
        self.timeout = timeout or config.CLAUDE_CODE_TIMEOUT_SECONDS
        self.messages = self
        self._runner = runner

    def _run(self, *args, **kwargs):
        return (self._runner or subprocess.run)(*args, **kwargs)

    def command(self, params: dict[str, Any]) -> list[str]:
        schema = strip_text_format(params["output_config"]["format"]["schema"])
        cmd = [self.cli, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--system-prompt", params["system"], "--model", params["model"], "--tools", "",
               "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
        effort = params["output_config"].get("effort")
        if effort:
            cmd += ["--effort", effort]
        return cmd

    def create(self, **params: Any) -> SimpleNamespace:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDECODE")}
        with tempfile.TemporaryDirectory(prefix="vsa-llm-") as cwd:
            try:
                proc = self._run(self.command(params), input=flatten_messages(params["messages"]),
                                 capture_output=True, text=True, timeout=self.timeout, cwd=cwd, env=env)
            except subprocess.TimeoutExpired as exc:
                raise ClaudeCodeError(f"Claude Code CLI timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise ClaudeCodeError(f"Claude Code CLI could not start: {exc}") from exc
        return parse_output(proc.stdout, proc.stderr, proc.returncode)


def parse_output(stdout: str, stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    """The CLI's --output-format json result → an Anthropic-Message-shaped object."""
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = (stderr or stdout or "").strip()[:300]
        raise ClaudeCodeError(f"Claude Code CLI returned no JSON (exit {returncode}): {detail}") from exc
    if out.get("is_error") or out.get("subtype") != "success":
        raise ClaudeCodeError(f"Claude Code CLI error ({out.get('subtype')}, "
                              f"api status {out.get('api_error_status')}): {str(out.get('result'))[:300]}")
    structured = out.get("structured_output")
    usage = out.get("usage") or {}
    in_tok = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                    "cache_read_input_tokens"))
    text = json.dumps(structured) if structured is not None else (out.get("result") or "")
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=int(usage.get("output_tokens") or 0)),
        stop_reason="end_turn" if structured is not None else "no_structured_output",
        stop_details=None, backend=BACKEND, billed_cost=0.0,
        list_price_cost=float(out.get("total_cost_usd") or 0.0))


def available() -> bool:
    return find_cli() is not None


def cli_version(cli: str) -> str:
    try:
        return subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
