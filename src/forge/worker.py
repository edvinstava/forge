import json
from dataclasses import dataclass

# Specific auth/usage phrases — NOT bare "login"/"credit", which match ordinary
# task text ("add a login page", "credit calculation") and falsely flag a normal
# failed turn as a Claude credentials problem.
_AUTH_HINTS = ("oauth", "/login", "credit balance", "rate limit", "usage limit",
               "unauthorized", "invalid api key")


@dataclass(frozen=True)
class WorkerResult:
    ok: bool
    is_error: bool
    num_turns: int
    duration_ms: int
    total_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    session_id: str
    result_text: str
    auth_error: bool


def parse_worker_result(stdout: str) -> WorkerResult:
    try:
        d = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return WorkerResult(False, True, 0, 0, None, 0, 0, "", stdout or "", True)
    usage = d.get("usage", {}) or {}
    subtype = d.get("subtype", "")
    is_error = bool(d.get("is_error", False))
    result_text = str(d.get("result", ""))
    auth = subtype != "success" and any(h in result_text.lower() for h in _AUTH_HINTS)
    return WorkerResult(
        ok=(not is_error and subtype == "success"),
        is_error=is_error,
        num_turns=int(d.get("num_turns", 0)),
        duration_ms=int(d.get("duration_ms", 0)),
        total_cost_usd=d.get("total_cost_usd"),
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        session_id=str(d.get("session_id", "")),
        result_text=result_text,
        auth_error=auth,
    )


@dataclass(frozen=True)
class StreamEvent:
    kind: str                 # 'narration' | 'tool' | 'result' | 'other'
    text: str = ""
    target: str = ""          # tool's primary argument (file, command, pattern)
    result: "WorkerResult | None" = None


def _tool_target(name: str, inp: dict) -> str:
    """A short, human-readable label for what a tool call acts on, so the UI can
    render "Edit  global-error.tsx" instead of a bare, repeated tool name. Kept
    intentionally terse — the stream is a glanceable activity log, not a console."""
    inp = inp or {}
    if name in ("Read", "Edit", "Write", "NotebookEdit", "NotebookRead"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        return path.rsplit("/", 1)[-1] if path else ""
    if name == "Bash":
        first = (inp.get("command") or "").strip().splitlines()
        return first[0][:72] if first else ""
    if name in ("Grep", "Glob"):
        return (inp.get("pattern") or inp.get("query") or "")[:72]
    if name in ("Task", "Agent"):
        return inp.get("description") or ""
    if name in ("WebFetch", "WebSearch"):
        return (inp.get("url") or inp.get("query") or "")[:72]
    return ""


def parse_stream_line(line: str) -> "StreamEvent | None":
    line = (line or "").strip()
    if not line:
        return None
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    t = d.get("type")
    if t == "result":
        return StreamEvent("result", str(d.get("result", "")),
                           result=parse_worker_result(line))
    if t == "assistant":
        for block in d.get("message", {}).get("content", []) or []:
            if block.get("type") == "text":
                return StreamEvent("narration", block.get("text", ""))
            if block.get("type") == "tool_use":
                name = block.get("name", "tool")
                return StreamEvent("tool", name, target=_tool_target(name, block.get("input")))
    return StreamEvent("other")
