import json
from types import SimpleNamespace

from forge import providers


def _cfg(**kw):
    base = dict(provider="claude", oauth_token="ct", openai_api_key="ok",
                codex_auth="auto")
    base.update(kw)
    return SimpleNamespace(**base)


# --- selection ------------------------------------------------------------------

def test_from_config_selects_provider_and_defaults_to_claude():
    assert providers.from_config(SimpleNamespace(provider="claude")).name == "claude"
    assert providers.from_config(SimpleNamespace(provider="codex")).name == "codex"
    assert providers.from_config(SimpleNamespace(provider="gpt-oops")).name == "claude"


# --- claude delegates to the existing CLI contract --------------------------------

def test_claude_provider_builds_cli_argv_and_resolves_models():
    p = providers.ClaudeProvider()
    argv = p.stream_cmd("do it", "sonnet", "sess-1")
    assert argv[:2] == ["claude", "-p"]
    assert "--resume" in argv and "sess-1" in argv
    assert p.resolve_model("opus", "x") == "opus"
    assert p.resolve_model("auto", "fix typo") in ("haiku", "sonnet", "opus")
    ev = p.stream_parser().feed(json.dumps(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hi"}]}}))
    assert ev.kind == "narration" and ev.text == "hi"


# --- codex argv -------------------------------------------------------------------

def test_codex_worker_cmd_new_and_resume():
    p = providers.CodexProvider()
    fresh = p.worker_cmd("do it", "gpt-5-codex")
    assert fresh[:2] == ["codex", "exec"]
    assert fresh[-1] == "do it"
    assert "--json" in fresh and "--model" in fresh
    resumed = p.worker_cmd("go on", "gpt-5-codex", "thread-9")
    assert resumed[:4] == ["codex", "exec", "resume", "thread-9"]
    assert resumed[-1] == "go on"


def test_codex_resolve_model_honors_choice_and_defaults():
    p = providers.CodexProvider()
    assert p.resolve_model("gpt-5", "x") == "gpt-5"
    assert p.resolve_model("auto", "x") == "gpt-5-codex"
    # A claude alias from the UI degrades to the codex default, never errors.
    assert p.resolve_model("opus", "x") == "gpt-5-codex"


# --- codex stream parsing -----------------------------------------------------------

def _codex_lines():
    return [
        json.dumps({"type": "thread.started", "thread_id": "th-1"}),
        json.dumps({"type": "item.started",
                    "item": {"id": "i1", "type": "command_execution",
                             "command": "bun test"}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i1", "type": "command_execution",
                             "command": "bun test", "exit_code": 0}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i2", "type": "file_change",
                             "changes": [{"path": "src/app/page.tsx"}]}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "i3", "type": "agent_message",
                             "text": "Fixed the offer column."}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": 120, "output_tokens": 34}}),
    ]


def test_codex_stream_maps_items_to_events():
    parser = providers.CodexProvider().stream_parser()
    events = [parser.feed(l) for l in _codex_lines()]
    kinds = [e.kind for e in events]
    assert kinds == ["other", "tool", "other", "tool", "narration", "result"]
    tool = events[1]
    assert tool.text == "Bash" and tool.target == "bun test"
    edit = events[3]
    assert edit.text == "Edit" and edit.target == "page.tsx"
    result = events[-1].result
    assert result.ok and result.session_id == "th-1"
    assert result.result_text == "Fixed the offer column."
    assert result.input_tokens == 120 and result.output_tokens == 34


def test_codex_tool_items_emit_once_per_item_id():
    parser = providers.CodexProvider().stream_parser()
    lines = _codex_lines()
    events = [parser.feed(l) for l in lines[:3]]   # started + completed for i1
    assert [e.kind for e in events] == ["other", "tool", "other"]


def test_codex_parse_result_scans_jsonl_and_flags_auth_errors():
    p = providers.CodexProvider()
    ok = p.parse_result("\n".join(_codex_lines()))
    assert ok.ok and ok.result_text == "Fixed the offer column."
    failed = p.parse_result(json.dumps(
        {"type": "turn.failed", "error": {"message": "401 Unauthorized"}}))
    assert failed.is_error and failed.auth_error
    garbage = p.parse_result("total nonsense")
    assert garbage.is_error and not garbage.ok


def test_codex_secrets_expose_openai_key_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))   # no plan login
    assert providers.CodexProvider().secrets(_cfg()) == {"OPENAI_API_KEY": "ok"}
    assert providers.ClaudeProvider().secrets(_cfg()) == \
        {"CLAUDE_CODE_OAUTH_TOKEN": "ct"}


def test_codex_prefers_plan_auth_over_api_billing(tmp_path, monkeypatch):
    # Subscription-first: with a ChatGPT-plan login present, the API key is
    # suppressed so usage bills the plan — FORGE_CODEX_AUTH=api overrides.
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    p = providers.CodexProvider()
    assert p.secrets(_cfg()) == {"OPENAI_API_KEY": ""}
    assert p.secrets(_cfg(codex_auth="api")) == {"OPENAI_API_KEY": "ok"}


def test_credentials_ready_is_provider_aware(tmp_path, monkeypatch):
    # Claude needs its OAuth token.
    assert providers.ClaudeProvider().credentials_ready(_cfg(oauth_token="ct"))
    assert not providers.ClaudeProvider().credentials_ready(_cfg(oauth_token=""))
    # Codex is ready with a ChatGPT-plan login even with no Claude token and no
    # API key — the case the old CLI gate wrongly rejected.
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex = providers.CodexProvider()
    assert codex.credentials_ready(_cfg(oauth_token="", openai_api_key=""))
    # Without a plan login, codex falls back to needing the API key.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    assert codex.credentials_ready(_cfg(oauth_token="", openai_api_key="ok"))
    assert not codex.credentials_ready(_cfg(oauth_token="", openai_api_key=""))


def test_codex_idless_tool_items_emit_once_on_completed():
    # Without item ids there is no way to correlate started/completed pairs —
    # the parser must emit exactly one tool event (the terminal one), never a
    # nondeterministic id()-based dedup.
    parser = providers.CodexProvider().stream_parser()
    started = parser.feed(json.dumps(
        {"type": "item.started",
         "item": {"type": "command_execution", "command": "bun test"}}))
    completed = parser.feed(json.dumps(
        {"type": "item.completed",
         "item": {"type": "command_execution", "command": "bun test"}}))
    assert started.kind == "other"
    assert completed.kind == "tool" and completed.target == "bun test"
