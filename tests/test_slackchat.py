import json
import subprocess
from types import SimpleNamespace

from forge.slackchat import generate_reply

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


def test_returns_model_reply_on_success(tmp_path):
    run = _run_returning(_ok("I spin repos up in a sandbox and send a live link."))
    out = generate_reply(_cfg(tmp_path), "", "what can you do?", FALLBACK, run=run)
    assert out == "I spin repos up in a sandbox and send a live link."
    assert out != FALLBACK


def test_preserves_multiline_reply(tmp_path):
    run = _run_returning(_ok("Sure!\n\n- I make changes\n- I open PRs"))
    out = generate_reply(_cfg(tmp_path), "", "what can you do?", FALLBACK, run=run)
    assert "\n" in out                       # chat replies aren't flattened


def test_uses_default_model_not_haiku(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=_ok("hi"), stderr="")
    generate_reply(_cfg(tmp_path), "", "hello", FALLBACK, run=run)
    assert seen["argv"][0] == "claude"
    assert "--model" not in seen["argv"]     # default (Sonnet) tier, per spec


def test_prompt_carries_transcript_and_latest(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=_ok("ok"), stderr="")
    generate_reply(_cfg(tmp_path), "User: hi\nforge: hey", "still there?", FALLBACK, run=run)
    prompt = seen["argv"][2]                  # claude -p <prompt> ...
    assert "still there?" in prompt
    assert "User: hi" in prompt


def test_falls_back_on_exception(tmp_path):
    run = _run_returning(exc=RuntimeError("claude not found"))
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK


def test_falls_back_on_timeout(tmp_path):
    run = _run_returning(exc=subprocess.TimeoutExpired(cmd="claude", timeout=30))
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK


def test_falls_back_on_nonzero_return(tmp_path):
    run = _run_returning(_ok("hi"), rc=1)
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK


def test_falls_back_on_empty_stdout(tmp_path):
    run = _run_returning(stdout="")
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK


def test_falls_back_on_auth_error(tmp_path):
    bad = json.dumps({"subtype": "error", "is_error": False,
                      "result": "Invalid API key / please run /login"})
    run = _run_returning(bad)
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK


def test_falls_back_on_empty_model_result(tmp_path):
    run = _run_returning(_ok("   "))
    assert generate_reply(_cfg(tmp_path), "", "hi", FALLBACK, run=run) == FALLBACK
