import json
import subprocess
from types import SimpleNamespace

from forge.slackroute import route_chat, parse_route, Route

FALLBACK = "👋 I'm forge."


def _cfg(tmp_path):
    return SimpleNamespace(runs_dir=tmp_path, oauth_token="o")


def _ok(text):
    return json.dumps({"subtype": "success", "is_error": False,
                       "result": text, "session_id": "s"})


def _run_returning(stdout="", rc=0, exc=None):
    def run(argv, **kw):
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=rc, stdout=stdout, stderr="")
    return run


# --- parse_route (pure) ---

def test_parse_build_action_drops_reply():
    r = parse_route('{"action": "build", "reply": ""}', FALLBACK)
    assert r == Route("build", None)


def test_parse_question_maps_to_qa():
    assert parse_route('{"action": "question", "reply": ""}', FALLBACK).action == "qa"


def test_parse_chat_keeps_reply():
    r = parse_route('{"action": "chat", "reply": "hei på deg! 👋"}', FALLBACK)
    assert r.action == "chat"
    assert r.reply == "hei på deg! 👋"


def test_parse_chat_without_reply_uses_fallback():
    assert parse_route('{"action": "chat"}', FALLBACK).reply == FALLBACK


def test_parse_tolerates_prose_and_fences():
    raw = "Sure, here you go:\n```json\n{\"action\":\"build\",\"reply\":\"\"}\n```"
    assert parse_route(raw, FALLBACK).action == "build"


def test_parse_garbage_falls_back_to_chat():
    assert parse_route("not json at all", FALLBACK) == Route("chat", FALLBACK)


def test_parse_unknown_action_falls_back_to_chat():
    assert parse_route('{"action": "launch_missiles"}', FALLBACK) == Route("chat", FALLBACK)


# --- route_chat (subprocess wrapper) ---

def test_route_chat_returns_build(tmp_path):
    run = _run_returning(_ok('{"action": "build", "reply": ""}'))
    assert route_chat(_cfg(tmp_path), "", "lag en about-side", FALLBACK, run=run) \
        == Route("build", None)


def test_route_chat_returns_chat_reply(tmp_path):
    run = _run_returning(_ok('{"action": "chat", "reply": "klart, hva vil du bygge?"}'))
    r = route_chat(_cfg(tmp_path), "", "hei", FALLBACK, run=run)
    assert r.action == "chat" and r.reply == "klart, hva vil du bygge?"


def test_route_chat_falls_back_on_exception(tmp_path):
    run = _run_returning(exc=RuntimeError("claude not found"))
    assert route_chat(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == Route("chat", FALLBACK)


def test_route_chat_falls_back_on_timeout(tmp_path):
    run = _run_returning(exc=subprocess.TimeoutExpired(cmd="claude", timeout=30))
    assert route_chat(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == Route("chat", FALLBACK)


def test_route_chat_falls_back_on_nonzero(tmp_path):
    run = _run_returning(_ok('{"action":"build"}'), rc=1)
    assert route_chat(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == Route("chat", FALLBACK)


def test_route_chat_falls_back_on_auth_error(tmp_path):
    bad = json.dumps({"subtype": "error", "is_error": False,
                      "result": "Invalid API key / please run /login"})
    run = _run_returning(bad)
    assert route_chat(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == Route("chat", FALLBACK)


def test_route_chat_prompt_carries_message_and_transcript(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=_ok('{"action":"chat","reply":"ok"}'),
                               stderr="")
    route_chat(_cfg(tmp_path), "User: hi\nforge: hey", "lag en side", FALLBACK, run=run)
    prompt = seen["argv"][2]
    assert "lag en side" in prompt
    assert "User: hi" in prompt
