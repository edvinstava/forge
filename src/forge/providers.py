"""Which agent CLI runs a worker turn.

Forge execs an agent CLI inside the per-run worker container. The default is
Claude Code (billed to your Claude subscription via CLAUDE_CODE_OAUTH_TOKEN);
the OpenAI Codex CLI is a drop-in alternative (FORGE_PROVIDER=codex) billed to
an OpenAI plan — ChatGPT-plan auth via a mounted ~/.codex, or OPENAI_API_KEY.

A provider owns the full CLI contract: argv for one-shot and streaming turns,
parsing its output into forge's WorkerResult / StreamEvents, model selection,
and which auth env vars its CLI needs inside the container. SessionManager
talks only to this interface, so adding a provider never touches the engine.
"""
import json
import os
from pathlib import Path

from forge import commands as cmd
from forge import models as claude_models
from forge.worker import WorkerResult, parse_stream_line, parse_worker_result
from forge.worker import StreamEvent, workspace_relpath


class ClaudeProvider:
    name = "claude"
    model_choices = claude_models.MODEL_CHOICES
    fast_model = "haiku"    # for one-line conversational calls (Slack opener)
    credential_hint = "CLAUDE_CODE_OAUTH_TOKEN (run `claude setup-token`)"

    def credentials_ready(self, cfg) -> bool:
        return bool(cfg.oauth_token)

    def worker_cmd(self, prompt, model, session_id=None) -> list:
        return cmd.worker_cmd(prompt, model, session_id)

    def stream_cmd(self, prompt, model, session_id=None) -> list:
        return cmd.worker_stream_cmd(prompt, model, session_id)

    def parse_result(self, stdout: str) -> WorkerResult:
        return parse_worker_result(stdout)

    def stream_parser(self):
        return _StatelessParser(parse_stream_line)

    def resolve_model(self, choice, prompt) -> str:
        return claude_models.resolve_model(choice, prompt)

    def secrets(self, cfg) -> dict:
        return {"CLAUDE_CODE_OAUTH_TOKEN": cfg.oauth_token}


class _StatelessParser:
    def __init__(self, fn):
        self._fn = fn

    def feed(self, line):
        return self._fn(line)


# --- OpenAI Codex CLI ---------------------------------------------------------

_CODEX_AUTH_HINTS = ("unauthorized", "401", "usage limit", "rate limit",
                     "invalid api key", "login", "authentication")


def codex_home() -> Path:
    """Where the codex CLI keeps its auth (auth.json). Honors CODEX_HOME like
    the CLI itself; forge mounts this into the worker for plan-based auth."""
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def codex_plan_auth_present() -> bool:
    return (codex_home() / "auth.json").is_file()

# `codex exec --json` emits JSONL "thread items"; map each executable item type
# to the (tool-name, input-key) pair forge renders as a live activity line.
_CODEX_TOOL_ITEMS = {
    "command_execution": ("Bash", "command"),
    "file_change": ("Edit", None),          # target derived from changes[]
    "mcp_tool_call": ("MCP", "tool"),
    "web_search": ("WebSearch", "query"),
}


class CodexProvider:
    name = "codex"
    model_choices = ["auto", "gpt-5-codex", "gpt-5"]
    fast_model = "gpt-5-codex"
    credential_hint = "a ChatGPT-plan `codex login` (~/.codex) or OPENAI_API_KEY"

    def credentials_ready(self, cfg) -> bool:
        mode = getattr(cfg, "codex_auth", "auto")
        if mode != "api" and codex_plan_auth_present():
            return True
        return bool(cfg.openai_api_key)

    def _flags(self, model) -> list:
        # The worker container IS the sandbox (mirrors claude's
        # --dangerously-skip-permissions); the workspace is bind-mounted so the
        # repo-trust check would otherwise refuse to run.
        flags = ["--json", "--dangerously-bypass-approvals-and-sandbox",
                 "--skip-git-repo-check"]
        if model:
            flags += ["--model", model]
        return flags

    def worker_cmd(self, prompt, model, session_id=None) -> list:
        if session_id:
            return (["codex", "exec", "resume", session_id]
                    + self._flags(model) + [prompt])
        return ["codex", "exec"] + self._flags(model) + [prompt]

    def stream_cmd(self, prompt, model, session_id=None) -> list:
        return self.worker_cmd(prompt, model, session_id)   # --json is already JSONL

    def parse_result(self, stdout: str) -> WorkerResult:
        parser = self.stream_parser()
        result = None
        for line in (stdout or "").splitlines():
            ev = parser.feed(line)
            if ev is not None and ev.kind == "result":
                result = ev.result
        return result or WorkerResult(False, True, 0, 0, None, 0, 0, "",
                                      stdout or "", False)

    def stream_parser(self):
        return _CodexStreamParser()

    def resolve_model(self, choice, prompt) -> str:
        if choice in self.model_choices and choice != "auto":
            return choice
        return "gpt-5-codex"    # auto / unknown (e.g. a claude alias from the UI)

    def secrets(self, cfg) -> dict:
        # Subscription-first: when a ChatGPT-plan login exists (~/.codex is
        # mounted into the worker), suppress the API key so the CLI bills the
        # plan — never per-token API usage. FORGE_CODEX_AUTH=api forces the key.
        mode = getattr(cfg, "codex_auth", "auto")
        if mode != "api" and codex_plan_auth_present():
            return {"OPENAI_API_KEY": ""}
        return {"OPENAI_API_KEY": cfg.openai_api_key}


class _CodexStreamParser:
    """Stateful: codex's terminal `turn.completed` event carries usage but not
    the reply text, so the parser remembers the last agent_message to hand back
    as the turn's result_text."""

    def __init__(self):
        self._thread_id = ""
        self._last_message = ""
        self._seen_items: set = set()

    def feed(self, line: str) -> "StreamEvent | None":
        line = (line or "").strip()
        if not line:
            return None
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
        t = d.get("type", "")
        if t in ("thread.started", "session.created"):
            self._thread_id = str(d.get("thread_id") or d.get("session_id") or "")
            return StreamEvent("other")
        if t in ("item.started", "item.completed"):
            return self._item(d.get("item") or {}, completed=t == "item.completed")
        if t == "turn.completed":
            usage = d.get("usage") or {}
            return StreamEvent("result", self._last_message,
                               result=self._result(ok=True, usage=usage))
        if t in ("turn.failed", "error"):
            msg = str((d.get("error") or {}).get("message")
                      or d.get("message") or "codex turn failed")
            return StreamEvent("result", msg, result=self._result(ok=False, text=msg))
        return StreamEvent("other")

    def _item(self, item, completed):
        itype = item.get("type", "")
        if itype == "agent_message" and completed:
            text = str(item.get("text", ""))
            self._last_message = text or self._last_message
            return StreamEvent("narration", text)
        if itype in _CODEX_TOOL_ITEMS:
            # Emit each tool once. With an item id we take the first sighting
            # (started, falling back to completed); without one there is no way
            # to correlate started/completed pairs, so emit only the terminal
            # event — deterministic, never double- or under-counts.
            item_id = item.get("id")
            if item_id is not None:
                if item_id in self._seen_items:
                    return StreamEvent("other")
                self._seen_items.add(item_id)
            elif not completed:
                return StreamEvent("other")
            name, field = _CODEX_TOOL_ITEMS[itype]
            if itype == "file_change":
                changes = item.get("changes") or []
                path = (changes[0] or {}).get("path", "") if changes else ""
                target = path.rsplit("/", 1)[-1]
                return StreamEvent("tool", name, target=target,
                                   path=workspace_relpath(path))
            target = str(item.get(field) or "")[:72]
            return StreamEvent("tool", name, target=target)
        return StreamEvent("other")

    def _result(self, ok, usage=None, text=None) -> WorkerResult:
        usage = usage or {}
        rtext = text if text is not None else self._last_message
        return WorkerResult(
            ok=ok, is_error=not ok, num_turns=1, duration_ms=0,
            total_cost_usd=None,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            session_id=self._thread_id, result_text=rtext,
            auth_error=(not ok) and any(h in rtext.lower()
                                        for h in _CODEX_AUTH_HINTS),
        )


_PROVIDERS = {"claude": ClaudeProvider, "codex": CodexProvider}


def from_config(cfg) -> "ClaudeProvider | CodexProvider":
    """The configured provider; unknown names fall back to claude so a typo'd
    FORGE_PROVIDER never bricks the daemon."""
    return _PROVIDERS.get(getattr(cfg, "provider", "claude"), ClaudeProvider)()


def host_env(provider, cfg) -> dict:
    """Extra env for running the provider CLI on the HOST (Slack chat/opener
    one-shots — conversational turns with no repo checkout; repo Q&A runs
    containerized, see slackqa): its auth secret(s), with empty values dropped
    so the CLI falls back to its own stored login (~/.codex, claude keychain)
    — subscription-first there too."""
    return {k: v for k, v in provider.secrets(cfg).items() if v}
