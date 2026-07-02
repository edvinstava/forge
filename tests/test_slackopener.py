import json
import subprocess
from types import SimpleNamespace

from forge.slackopener import generate_opener

FALLBACK = "👋 Spinning up `acme/x` — taking a look 👀"


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


def test_returns_cleaned_model_line_on_success(tmp_path):
    run = _run_returning(_ok("Hey! On it — pulling up the repo 👀"))
    out = generate_opener(_cfg(tmp_path), "acme/x", "hi! add a page",
                          "build", FALLBACK, run=run)
    assert out == "Hey! On it — pulling up the repo 👀"
    assert out != FALLBACK


def test_flattens_multiline_output_to_one_line(tmp_path):
    run = _run_returning(_ok("Hey!\nPulling up the repo now 👀"))
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert "\n" not in out
    assert out == "Hey! Pulling up the repo now 👀"


def test_passes_haiku_model_and_runs_claude(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=_ok("hi"), stderr="")
    generate_opener(_cfg(tmp_path), "acme/x", "t", "build", FALLBACK, run=run)
    assert seen["argv"][0] == "claude"
    assert "--model" in seen["argv"] and "haiku" in seen["argv"]


def test_falls_back_on_exception(tmp_path):
    run = _run_returning(exc=RuntimeError("claude not found"))
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert out == FALLBACK


def test_falls_back_on_timeout(tmp_path):
    run = _run_returning(exc=subprocess.TimeoutExpired(cmd="claude", timeout=8))
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert out == FALLBACK


def test_falls_back_on_empty_stdout(tmp_path):
    run = _run_returning(stdout="")
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert out == FALLBACK


def test_falls_back_on_nonzero_return(tmp_path):
    run = _run_returning(_ok("hi"), rc=1)
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert out == FALLBACK


def test_falls_back_on_empty_model_result(tmp_path):
    run = _run_returning(_ok("   "))
    out = generate_opener(_cfg(tmp_path), "acme/x", "t", "build",
                          FALLBACK, run=run)
    assert out == FALLBACK
