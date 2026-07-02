# Forge Chat Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web chat workspace where each chat session owns one warm Docker environment and one live inspection URL; you iterate with multiple prompts, watch Claude work live, and open a PR on demand.

**Architecture:** A new `SessionManager` decomposes today's monolithic `ComposeOrchestrator.run()` into `start / turn / open_pr / end` steps that reuse the existing leaf functions (`host.clone`, `recipe.resolve`, `ComposeEnv`, `commands.*`, `verify`, `health`, `lifecycle`, `proxy`). A FastAPI app (`forge web`) serves a React+Vite SPA plus a JSON/SSE API, persists sessions in the existing SQLite `Store`, and runs the Caddy proxy + idle reaper in-process. The CLI (`run/status/down/serve/bake`) is untouched.

**Tech Stack:** Python 3.11+, FastAPI + uvicorn (new deps), existing Docker Compose engine, React 18 + Vite + TypeScript (built to static, served by FastAPI), pytest, Vitest (frontend unit).

## Global Constraints

- Python `>=3.11` (matches `pyproject.toml`). Stdlib + minimal deps only.
- New runtime deps: `fastapi`, `uvicorn[standard]`. New dev/test deps: `httpx` (for `TestClient`). Declared under a `web` extra + `dev` extra in `pyproject.toml`.
- Server binds **`127.0.0.1` only**. `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN` stay server-side; never serialized to any API response.
- Reuse the existing `Store` (SQLite at `runs_dir/forge.db`); schema changes are **additive only** (guarded `ALTER TABLE`, `CREATE TABLE IF NOT EXISTS`). Never break existing `runs`/`events`/`envs` rows or tests.
- `session_id == run_id` (a hex uuid). One `runs` row + one `envs` row per session.
- Turns **never commit**; the diff viewer shows uncommitted working-tree changes vs `HEAD`. Commit/push/PR happen only in `open_pr()`.
- The worker prompt instructs the agent to favor **minimal, simple, well-crafted** changes and to never commit/push/PR.
- Follow existing code style: pure argv builders in `commands.py`/`compose.py`; dataclasses (`frozen=True` where immutable); injected `host`/`env_factory`/`clock` for testability; capture-don't-log secrets.
- Tests mirror existing patterns: fakes for `host` and `env_factory`; real-Docker smokes gated behind an image-presence check (see `tests/test_node_web_smoke.py`).
- All new CLI/HTTP must keep `pytest` green: full suite is the gate (`python -m pytest -q`).

---

## File Structure

```
src/forge/
  store.py             EDIT: + runs columns (claude_session_id, repo_source, title), messages table + CRUD, reconcile helper
  worker.py            EDIT: + parse_stream_line() for stream-json events (keep parse_worker_result)
  commands.py          EDIT: + worker_stream_cmd()
  composeenv.py        EDIT: + exec_stream() (yields stdout lines; returns process handle for cancel)
  hostops.py           EDIT: + clone_local(), origin_url(); list_repos lives in repos.py
  config.py            EDIT: + workspace_dir, max_live_sessions
  probing.py           CREATE: build_probe(host, ws) extracted from ComposeOrchestrator._probe (shared)
  compose_orchestrator.py  EDIT: _probe delegates to probing.build_probe (no behavior change)
  repos.py             CREATE: list_repos(workspace_dir, q) → local repo listing/search
  session.py           CREATE: SessionManager + TurnEvent (start/turn/open_pr/end/diff/reconcile)
  webapp.py            CREATE: FastAPI app — routes, SSE, static serving, startup reconcile + reaper task
  cli.py               EDIT: + `forge web` subcommand
web/                   CREATE: React + Vite SPA
  package.json, vite.config.ts, tsconfig.json, index.html
  src/api.ts           typed API client + SSE parser
  src/types.ts         shared TS types
  src/App.tsx          three-pane layout + session state
  src/Sidebar.tsx      sessions list + repo picker
  src/Chat.tsx         message bubbles + streaming + action buttons
  src/Inspector.tsx    tabs: Preview / Diff / Verify
  src/*.test.ts        Vitest unit tests for api.ts SSE parsing
  dist/                built static output (gitignored or committed build)
tests/
  test_store_messages.py        runs migration + messages CRUD + reconcile
  test_worker_stream.py         parse_stream_line
  test_commands_stream.py       worker_stream_cmd
  test_composeenv_stream.py     exec_stream (real local subprocess, no docker)
  test_hostops_local.py         clone_local + origin_url (real git, temp repos)
  test_repos.py                 list_repos
  test_probing.py               build_probe parity
  test_session.py               SessionManager lifecycle with fakes
  test_webapp.py                FastAPI routes via TestClient (mocked manager)
  test_session_smoke.py         real-Docker node-web session (gated)
```

---

## PHASE A — Backend foundations

### Task 1: Store — runs migration, messages table, CRUD, reconcile

**Files:**
- Modify: `src/forge/store.py`
- Test: `tests/test_store_messages.py`

**Interfaces:**
- Consumes: existing `Store(db_path)`, `runs`/`envs` tables.
- Produces:
  - `Store.add_message(run_id, role, content, meta=None) -> int` (returns row id)
  - `Store.list_messages(run_id) -> list[dict]` (each: `id, run_id, role, content, created_at, meta` with `meta` JSON-decoded or `{}`)
  - `Store.set_session_fields(run_id, *, claude_session_id=None, repo_source=None, title=None) -> None` (COALESCE — only non-None overwrite)
  - `Store.get_run(run_id)` now includes `claude_session_id, repo_source, title`.
  - `Store.list_sessions() -> list[dict]` (join runs + envs: `run_id, repo, title, state, repo_source, web_url, last_active`; `last_active = max(runs.updated_at, envs.last_seen_at)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store_messages.py
from forge.store import Store


def test_messages_roundtrip(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "task", "forge/x")
    mid = s.add_message("r1", "user", "fix the date picker")
    s.add_message("r1", "assistant", "done", meta={"cost": 0.12, "diff_files": 3})
    msgs = s.list_messages("r1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["meta"]["diff_files"] == 3
    assert isinstance(mid, int)


def test_session_fields_and_listing(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_run("r1", "o/r", "task", "forge/x")
    s.set_session_fields("r1", claude_session_id="sess-9", repo_source="github:o/r",
                         title="Date picker fix")
    run = s.get_run("r1")
    assert run["claude_session_id"] == "sess-9"
    assert run["repo_source"] == "github:o/r"
    assert run["title"] == "Date picker fix"
    s.create_env("r1", "forge-r1", "http://localhost:5051", 3000, "live", web_service="web")
    rows = s.list_sessions()
    assert rows[0]["run_id"] == "r1"
    assert rows[0]["web_url"] == "http://localhost:5051"
    assert rows[0]["title"] == "Date picker fix"


def test_migration_is_idempotent_and_preserves_rows(tmp_path):
    db = tmp_path / "f.db"
    s = Store(db)
    s.create_run("r1", "o/r", "task", "forge/x")
    s2 = Store(db)            # re-open → migration runs again, no error
    assert s2.get_run("r1")["repo"] == "o/r"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store_messages.py -q`
Expected: FAIL (`add_message`/`set_session_fields`/`list_sessions` not defined).

- [ ] **Step 3: Implement migration + methods**

In `store.py`, append a `messages` table to `_SCHEMA`:

```python
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')), meta TEXT
);
```

Add a guarded column migration run from `__init__` after `executescript`:

```python
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _migrate(self, c) -> None:
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
        for col in ("claude_session_id", "repo_source", "title"):
            if col not in cols:
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")

    def add_message(self, run_id, role, content, meta=None) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages(run_id, role, content, meta) VALUES (?,?,?,?)",
                (run_id, role, content, json.dumps(meta) if meta is not None else None))
            return cur.lastrowid

    def list_messages(self, run_id) -> list:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM messages WHERE run_id=? ORDER BY id",
                             (run_id,)).fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"]) if r["meta"] else {}}
                for r in rows]

    def set_session_fields(self, run_id, *, claude_session_id=None,
                           repo_source=None, title=None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET claude_session_id=COALESCE(?, claude_session_id), "
                "repo_source=COALESCE(?, repo_source), title=COALESCE(?, title), "
                "updated_at=datetime('now') WHERE run_id=?",
                (claude_session_id, repo_source, title, run_id))

    def list_sessions(self) -> list:
        q = ("SELECT r.run_id, r.repo, r.title, r.state, r.repo_source, r.pr_url, "
             "e.web_url, e.web_service, e.state AS env_state, "
             "MAX(r.updated_at, COALESCE(e.last_seen_at, r.updated_at)) AS last_active "
             "FROM runs r LEFT JOIN envs e ON e.run_id=r.run_id "
             "ORDER BY last_active DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(q).fetchall()]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_store_messages.py tests/test_store.py tests/test_store_envs.py -q`
Expected: PASS (new + existing store tests).

- [ ] **Step 5: Commit**

```bash
git add src/forge/store.py tests/test_store_messages.py
git commit -m "feat(store): session fields, messages table, session listing"
```

---

### Task 2: Stream-json worker parsing

**Files:**
- Modify: `src/forge/worker.py`
- Test: `tests/test_worker_stream.py`

**Interfaces:**
- Consumes: existing `WorkerResult`, `parse_worker_result`.
- Produces:
  - `StreamEvent` dataclass: `kind: str` (`'narration'|'tool'|'result'|'other'`), `text: str`, `result: WorkerResult | None`.
  - `parse_stream_line(line: str) -> StreamEvent | None` — parse one line of `claude -p --output-format stream-json` output. Returns `None` for blank/unparseable lines.
    - `{"type":"assistant","message":{"content":[{"type":"text","text":...}]}}` → `narration` with the text.
    - `{"type":"assistant",...,"content":[{"type":"tool_use","name":"Read",...}]}` → `tool` with text like `"Read"`.
    - `{"type":"result", ...}` → `result` with `result=parse_worker_result(line)`.
    - anything else → `other` with empty text.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker_stream.py
import json
from forge.worker import parse_stream_line


def test_assistant_text_is_narration():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": "Reading files"}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "narration"
    assert ev.text == "Reading files"


def test_tool_use_is_tool_event():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                "input": {"file_path": "a.ts"}}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "tool"
    assert "Edit" in ev.text


def test_result_line_carries_worker_result():
    line = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "session_id": "sess-1", "result": "all done",
                       "total_cost_usd": 0.2, "num_turns": 3,
                       "usage": {"input_tokens": 10, "output_tokens": 5}})
    ev = parse_stream_line(line)
    assert ev.kind == "result"
    assert ev.result.session_id == "sess-1"
    assert ev.result.ok is True


def test_blank_line_returns_none():
    assert parse_stream_line("") is None
    assert parse_stream_line("not json") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker_stream.py -q`
Expected: FAIL (`parse_stream_line` not defined).

- [ ] **Step 3: Implement**

```python
# add to worker.py
@dataclass(frozen=True)
class StreamEvent:
    kind: str                 # 'narration' | 'tool' | 'result' | 'other'
    text: str = ""
    result: "WorkerResult | None" = None


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
        return StreamEvent("result", str(d.get("result", "")), parse_worker_result(line))
    if t == "assistant":
        for block in d.get("message", {}).get("content", []) or []:
            if block.get("type") == "text":
                return StreamEvent("narration", block.get("text", ""))
            if block.get("type") == "tool_use":
                return StreamEvent("tool", block.get("name", "tool"))
    return StreamEvent("other")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worker_stream.py tests/test_worker.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/worker.py tests/test_worker_stream.py
git commit -m "feat(worker): parse_stream_line for stream-json events"
```

---

### Task 3: Streaming worker command

**Files:**
- Modify: `src/forge/commands.py`
- Test: `tests/test_commands_stream.py`

**Interfaces:**
- Produces: `worker_stream_cmd(prompt: str, model: str | None, session_id: str | None = None) -> list` — like `worker_cmd` but `--output-format stream-json --verbose`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commands_stream.py
from forge.commands import worker_stream_cmd


def test_stream_cmd_uses_stream_json_and_verbose():
    cmd = worker_stream_cmd("do it", None)
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert "stream-json" in cmd and "--verbose" in cmd
    assert "--dangerously-skip-permissions" in cmd


def test_stream_cmd_resumes_session():
    cmd = worker_stream_cmd("more", None, "sess-7")
    assert "--resume" in cmd and "sess-7" in cmd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_commands_stream.py -q`
Expected: FAIL (`worker_stream_cmd` not defined).

- [ ] **Step 3: Implement**

```python
# add to commands.py
def worker_stream_cmd(prompt: str, model: str | None,
                      session_id: str | None = None) -> list:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_commands_stream.py tests/test_commands.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/commands.py tests/test_commands_stream.py
git commit -m "feat(commands): worker_stream_cmd (stream-json)"
```

---

### Task 4: ComposeEnv.exec_stream

**Files:**
- Modify: `src/forge/composeenv.py`
- Test: `tests/test_composeenv_stream.py`

**Interfaces:**
- Produces: `ComposeEnv.exec_stream(argv, workdir="/work", service=None) -> Iterator[str]` — yields stdout lines (newline-stripped) as the subprocess produces them. Stores the live `subprocess.Popen` on `self._proc` so `cancel()` can terminate it. After the generator is exhausted, `self._proc` is cleared.
- Produces: `ComposeEnv.cancel() -> None` — kills the current streaming process if any (`self._proc.kill()`).
- Note: implemented against `compose.exec_cmd` (already `-T`, no TTY). Tests substitute the command builder to run a local process so no Docker is required.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_composeenv_stream.py
import sys
from forge.composeenv import ComposeEnv


def test_exec_stream_yields_lines(monkeypatch):
    env = ComposeEnv("rid", [])
    # bypass docker: make exec_cmd a local python one-liner printing 3 lines
    from forge import composeenv
    script = "import sys,time\n[print(f'line{i}') or sys.stdout.flush() for i in range(3)]"
    monkeypatch.setattr(composeenv.compose, "exec_cmd",
                        lambda *a, **k: [sys.executable, "-c", script])
    lines = list(env.exec_stream(["ignored"]))
    assert lines == ["line0", "line1", "line2"]
    assert env._proc is None   # cleared after exhaustion
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_composeenv_stream.py -q`
Expected: FAIL (`exec_stream` not defined).

- [ ] **Step 3: Implement**

```python
# add to composeenv.py (imports already include subprocess, compose)
    def exec_stream(self, argv: list, workdir: str = "/work",
                    service: str | None = None):
        svc = service or self.worker_service
        cmd = compose.exec_cmd(self.project, self.files, svc, argv, workdir)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._proc = proc
        try:
            for line in proc.stdout:
                yield line.rstrip("\n")
            proc.wait()
        finally:
            self._proc = None

    def cancel(self) -> None:
        p = getattr(self, "_proc", None)
        if p and p.poll() is None:
            p.kill()
```

Also initialise `self._proc = None` in `__init__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_composeenv_stream.py tests/test_compose.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/composeenv.py tests/test_composeenv_stream.py
git commit -m "feat(composeenv): exec_stream + cancel for live worker output"
```

---

### Task 5: LocalHost.clone_local + origin_url

**Files:**
- Modify: `src/forge/hostops.py`
- Test: `tests/test_hostops_local.py`

**Interfaces:**
- Produces:
  - `LocalHost.origin_url(path: str) -> str | None` — `git -C <path> remote get-url origin` (stripped) or `None`.
  - `LocalHost.clone_local(src: str, branch: str, dest: str) -> ExecResult` — `git clone <src> <dest>`, then `git -C dest checkout -b branch`; if the source has a GitHub origin, rewire `dest`'s origin to it (so push/PR target GitHub). Returns the checkout `ExecResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hostops_local.py
import subprocess
from pathlib import Path
from forge.hostops import LocalHost


def _init_repo(path: Path, origin: str | None = None):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("hi")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)


def test_clone_local_creates_branch_and_rewires_origin(tmp_path):
    src = tmp_path / "src"
    _init_repo(src, origin="https://github.com/o/r.git")
    dest = tmp_path / "dest"
    h = LocalHost()
    res = h.clone_local(str(src), "forge/x", str(dest))
    assert res.exit_code == 0
    assert (dest / "README.md").is_file()
    branch = subprocess.run(["git", "-C", str(dest), "branch", "--show-current"],
                            capture_output=True, text=True).stdout.strip()
    assert branch == "forge/x"
    assert h.origin_url(str(dest)) == "https://github.com/o/r.git"


def test_origin_url_none_when_absent(tmp_path):
    src = tmp_path / "src"
    _init_repo(src, origin=None)
    assert LocalHost().origin_url(str(src)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hostops_local.py -q`
Expected: FAIL (`clone_local`/`origin_url` not defined).

- [ ] **Step 3: Implement**

```python
# add to hostops.py
    def origin_url(self, path: str) -> str | None:
        r = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                           capture_output=True, text=True)
        url = r.stdout.strip()
        return url if (r.returncode == 0 and url) else None

    def clone_local(self, src: str, branch: str, dest: str) -> ExecResult:
        d = Path(dest)
        d.parent.mkdir(parents=True, exist_ok=True)
        r1 = subprocess.run(["git", "clone", src, str(d)],
                            capture_output=True, text=True)
        if r1.returncode != 0:
            return ExecResult(r1.returncode, r1.stdout, r1.stderr)
        gh_origin = self.origin_url(src)
        if gh_origin:
            subprocess.run(["git", "-C", str(d), "remote", "set-url", "origin", gh_origin],
                           capture_output=True)
        r2 = subprocess.run(["git", "-C", str(d), "checkout", "-b", branch],
                            capture_output=True, text=True)
        return ExecResult(r2.returncode, r2.stdout, r2.stderr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_hostops_local.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/hostops.py tests/test_hostops_local.py
git commit -m "feat(hostops): clone_local from a local checkout + origin rewire"
```

---

### Task 6: Config + repo listing

**Files:**
- Modify: `src/forge/config.py`
- Create: `src/forge/repos.py`
- Test: `tests/test_repos.py`

**Interfaces:**
- `Config` gains: `workspace_dir: Path = Path.home() / "forge-repos"` (overridable via `FORGE_WORKSPACE_DIR`), `max_live_sessions: int = 4` (via `FORGE_MAX_SESSIONS`). `from_env` reads both.
- `repos.list_repos(workspace_dir, q: str = "") -> list[dict]` — directories directly under `workspace_dir` that contain `.git`; each `{name, path, remote}` where `remote` = origin URL or `""`. Filter by case-insensitive substring `q` against `name`. Returns `[]` if the dir is missing.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_repos.py
import subprocess
from forge.repos import list_repos


def test_list_repos_finds_git_dirs_and_filters(tmp_path):
    (tmp_path / "not-a-repo").mkdir()
    for name in ("dhis2-app", "chap-frontend"):
        p = tmp_path / name
        p.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/o/dhis2-app.git"],
                   cwd=tmp_path / "dhis2-app", check=True)
    names = {r["name"] for r in list_repos(str(tmp_path))}
    assert names == {"dhis2-app", "chap-frontend"}
    only = list_repos(str(tmp_path), q="dhis")
    assert [r["name"] for r in only] == ["dhis2-app"]
    assert only[0]["remote"] == "https://github.com/o/dhis2-app.git"


def test_missing_dir_returns_empty(tmp_path):
    assert list_repos(str(tmp_path / "nope")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_repos.py -q`
Expected: FAIL (`forge.repos` missing).

- [ ] **Step 3: Implement**

`config.py` — add fields + env reads:

```python
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "forge-repos")
    max_live_sessions: int = 4
```
In `from_env`, add:
```python
            workspace_dir=Path(os.environ.get("FORGE_WORKSPACE_DIR",
                                               str(Path.home() / "forge-repos"))),
            max_live_sessions=int(os.environ.get("FORGE_MAX_SESSIONS", "4")),
```

`repos.py`:

```python
import subprocess
from pathlib import Path


def _origin(path: Path) -> str:
    r = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def list_repos(workspace_dir, q: str = "") -> list:
    base = Path(workspace_dir)
    if not base.is_dir():
        return []
    out = []
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / ".git").exists():
            if q.lower() in child.name.lower():
                out.append({"name": child.name, "path": str(child),
                            "remote": _origin(child)})
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_repos.py tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/config.py src/forge/repos.py tests/test_repos.py
git commit -m "feat: workspace_dir/max_live_sessions config + local repo listing"
```

---

### Task 7: Extract shared probe helper

**Files:**
- Create: `src/forge/probing.py`
- Modify: `src/forge/compose_orchestrator.py` (delegate `_probe`)
- Test: `tests/test_probing.py`

**Interfaces:**
- `probing.build_probe(host, ws: str) -> recipe.Probe` — exactly the logic currently in `ComposeOrchestrator._probe` (incl. `_CHAP_APP_ID`, `_REPO_COMPOSE`). `ComposeOrchestrator._probe` becomes `return build_probe(self.host, ws)`. No behavior change.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_probing.py
from forge.probing import build_probe


class FakeHost:
    def __init__(self, files): self.files = files
    def read(self, ws, rel): return self.files.get(rel)
    def exists(self, ws, rel): return rel in self.files


def test_build_probe_detects_node_and_compose():
    host = FakeHost({"package.json": '{"scripts":{"dev":"vite"}}',
                     "docker-compose.yml": "services: {}"})
    p = build_probe(host, "/ws")
    assert p.package_json and p.repo_compose_path == "docker-compose.yml"
    assert not p.is_chap_frontend


def test_build_probe_detects_chap_frontend():
    host = FakeHost({"d2.config.js": "id: 'a29851f9-...'"})
    p = build_probe(host, "/ws")
    assert p.is_chap_frontend is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probing.py -q`
Expected: FAIL (`forge.probing` missing).

- [ ] **Step 3: Implement**

Move the constants + `_probe` body into `probing.py` as `build_probe(host, ws)`:

```python
from forge.recipe import Probe

_CHAP_APP_ID = "a29851f9"
_REPO_COMPOSE = ("docker-compose.yml", "docker-compose.yaml",
                 "compose.yml", "compose.yaml")


def build_probe(host, ws: str) -> Probe:
    d2 = host.read(ws, "d2.config.js") or ""
    pyproject = host.read(ws, "pyproject.toml") or ""
    repo_compose = next((f for f in _REPO_COMPOSE if host.exists(ws, f)), None)
    return Probe(
        package_json=host.read(ws, "package.json"),
        repo_yml=host.read(ws, ".forge/repo.yml"),
        env_yml=host.read(ws, ".forge/env.yml"),
        has_supabase_config=host.exists(ws, "supabase/config.toml"),
        has_d2_config=bool(d2),
        is_chap_frontend=_CHAP_APP_ID in d2,
        is_chap_core=("chap-core" in pyproject or "chap_core" in pyproject
                      or host.exists(ws, "chap_core")),
        repo_compose_path=repo_compose,
    )
```

In `compose_orchestrator.py`, replace the `_probe` method body with `from forge.probing import build_probe` (top) and `return build_probe(self.host, ws)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_probing.py tests/test_compose_orchestrator.py tests/test_recipe.py -q`
Expected: PASS (orchestrator behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/forge/probing.py src/forge/compose_orchestrator.py tests/test_probing.py
git commit -m "refactor: extract build_probe into probing.py (shared)"
```

---

## PHASE B — SessionManager

### Task 8: SessionManager.start (provision)

**Files:**
- Create: `src/forge/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- `TurnEvent` dataclass: `kind: str` (`'phase'|'narration'|'tool'|'verify'|'url'|'done'|'error'`), `data: dict`.
- `SessionManager(config, store, host, env_factory=default_env_factory, clock=time.monotonic)` — same injection shape as `ComposeOrchestrator`.
- `SessionManager.start(run_id, repo, source) -> Iterator[TurnEvent]`:
  - `source` is `"github"` (clone via `host.clone(repo, branch, ws, gh_token)`) or `"local"` (clone via `host.clone_local(repo, branch, ws)` — here `repo` is the local path).
  - Sequence: create_run → set_session_fields(repo_source) → reap-superseded **skipped** (sessions independent) → clone → `build_probe` → `resolve` recipe → write `forge-compose.yml` → parse verify plan → `create_env(starting)` → `env.up(secrets)` → `setup_git` → seed steps → health-gate via `_register` → yield `phase` events throughout, ending with `url` (or `error`).
  - Stores `web_service` in env row (as today). On any failure yields `error` and sets run/env state `failed`.
- Produces helper `_env_for(run_id) -> ComposeEnv` — rebuild from the run's compose file path (`runs/<id>/forge-compose.yml`) so envs are reconstructable across turns/restarts.
- Produces `_secrets() -> dict` (same three secrets as orchestrator).
- Reuses `RunOutcome`-free; start returns nothing but events.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session.py
import time
from pathlib import Path
from forge.session import SessionManager, TurnEvent
from forge.config import Config, Budget
from forge.store import Store


class FakeEnv:
    def __init__(self, run_id, files): self.run_id, self.files = run_id, files
        # provisioning + turn behavior is scripted by the test via attributes
    up_calls = 0
    def up(self, secrets): type(self).up_calls += 1
    def exec(self, argv, service=None, workdir="/work"):
        from forge.container import ExecResult
        joined = " ".join(argv)
        if "status" in joined and "--porcelain" in joined:
            return ExecResult(0, " M src/x.ts\n", "")
        if "rev-parse" in joined or "diff" in joined:
            return ExecResult(0, "diff --git a/x b/x\n", "")
        return ExecResult(0, "", "")
    def exec_stream(self, argv, service=None, workdir="/work"):
        import json
        yield json.dumps({"type": "assistant",
                          "message": {"content": [{"type": "text", "text": "editing"}]}})
        yield json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "session_id": "sess-1", "result": "fixed",
                          "total_cost_usd": 0.1, "num_turns": 1, "usage": {}})
    def port(self, service, port): return 5599
    def cancel(self): pass


class FakeHost:
    def clone(self, repo, branch, ws, token):
        from forge.container import ExecResult
        Path(ws).mkdir(parents=True, exist_ok=True)
        (Path(ws) / "package.json").write_text('{"scripts":{"dev":"vite","test":"jest"}}')
        return ExecResult(0, "", "")
    def read(self, ws, rel): return (Path(ws) / rel).read_text() if (Path(ws)/rel).is_file() else None
    def exists(self, ws, rel): return (Path(ws) / rel).exists()
    def write_file(self, path, content): Path(path).parent.mkdir(parents=True, exist_ok=True); Path(path).write_text(content)
    def run(self, argv, env=None):
        from forge.container import ExecResult; return ExecResult(0, "", "")


def _mgr(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", oauth_token="t", gh_token="g",
                 budget=Budget(max_iterations=2, max_wall_secs=60))
    store = Store(cfg.runs_dir / "forge.db")
    # health_poll succeeds because FakeEnv.exec returns 0 for the health argv
    return SessionManager(cfg, store, FakeHost(),
                          env_factory=lambda rid, files: FakeEnv(rid, files)), store


def test_start_provisions_and_registers_url(tmp_path):
    mgr, store = _mgr(tmp_path)
    events = list(mgr.start("r1", "o/r", "github"))
    kinds = [e.kind for e in events]
    assert "phase" in kinds
    assert events[-1].kind == "url"
    assert store.get_env("r1")["state"] == "live"
    assert store.get_run("r1")["repo_source"] == "github:o/r"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session.py::test_start_provisions_and_registers_url -q`
Expected: FAIL (`forge.session` missing).

- [ ] **Step 3: Implement `session.py` (start + helpers)**

```python
import json
import time
from pathlib import Path

from forge import commands as cmd
from forge import lifecycle
from forge.budget import BudgetTracker
from forge.health import health_poll_argv
from forge.prompts import build_task_prompt
from forge.probing import build_probe
from forge.recipe import SUPABASE_LOCAL_ANON_KEY, resolve
from forge.runspec import make_runspec
from forge.verify import parse_verify
from forge.worker import parse_stream_line
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnEvent:
    kind: str
    data: dict


def default_env_factory(run_id, files):
    from forge.composeenv import ComposeEnv
    return ComposeEnv(run_id, files)


class SessionManager:
    def __init__(self, config, store, host, env_factory=default_env_factory,
                 clock=time.monotonic):
        self.cfg, self.store, self.host = config, store, host
        self.env_factory, self.clock = env_factory, clock

    # --- helpers ---
    def _secrets(self) -> dict:
        return {"CLAUDE_CODE_OAUTH_TOKEN": self.cfg.oauth_token,
                "GH_TOKEN": self.cfg.gh_token,
                "FORGE_SUPABASE_ANON_KEY": SUPABASE_LOCAL_ANON_KEY}

    def _compose_path(self, run_id) -> Path:
        return Path(self.cfg.runs_dir) / run_id / "forge-compose.yml"

    def _env_for(self, run_id):
        cf = self._compose_path(run_id)
        files = [str(cf)] if cf.is_file() else []
        return self.env_factory(run_id, files)

    def _recipe_for(self, run_id, ws):
        probe = build_probe(self.host, ws)
        seed_dir = str(Path(self.cfg.runs_dir) / "cache" / "dhis2-seed")
        return resolve(probe, ws, self.cfg.image_tag, seed_dir=seed_dir), probe

    def start(self, run_id, repo, source):
        rs = make_runspec(repo if source == "github" else "local/repo", "session", run_id)
        self.store.create_run(run_id, repo, "", rs.branch)
        self.store.set_session_fields(run_id, repo_source=f"{source}:{repo}")
        ws = str(Path(self.cfg.runs_dir) / run_id / "workspace")
        self.store.set_state(run_id, "provisioning")
        yield TurnEvent("phase", {"name": "clone", "label": "Cloning"})
        cl = (self.host.clone_local(repo, rs.branch, ws) if source == "local"
              else self.host.clone(repo, rs.branch, ws, self.cfg.gh_token))
        if cl.exit_code != 0:
            self.store.set_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "clone",
                                      "detail": (cl.stderr or cl.stdout)[:300]})
            return
        recipe, probe = self._recipe_for(run_id, ws)
        yield TurnEvent("phase", {"name": "recipe", "label": f"Recipe: {recipe.name}"})
        if recipe.compose is not None:
            self.host.write_file(str(self._compose_path(run_id)),
                                 json.dumps(recipe.compose, indent=2))
        self._verify_plan = parse_verify(probe.package_json, probe.repo_yml,
                                         self.host.exists(ws, ".forge/verify.sh"))
        env = self._env_for(run_id)
        self.store.create_env(run_id, f"forge-{run_id}", None, recipe.web_port,
                              "starting", web_service=recipe.web_service)
        for hc in recipe.host_pre:
            self.host.run(hc)
        yield TurnEvent("phase", {"name": "up", "label": "Starting stack"})
        try:
            env.up(self._secrets())
        except Exception as e:                      # compose up failed
            self.store.set_state(run_id, "failed")
            self.store.set_env_state(run_id, "failed")
            yield TurnEvent("error", {"kind": "up", "detail": str(e)[:300]})
            return
        self.store.set_state(run_id, "running")
        env.exec(cmd.setup_git_cmd(), service="forge")
        for svc, argv in recipe.seed:
            env.exec(argv, service=svc)
        web_url = self._register(env, run_id, recipe)
        if web_url:
            yield TurnEvent("url", {"web_url": web_url})
        else:
            yield TurnEvent("phase", {"name": "noweb", "label": "No web service"})

    def _register(self, env, run_id, recipe):
        if not recipe.web_service:
            self.store.set_env_state(run_id, "live")    # worker-only: env is usable
            return None
        h = env.exec(health_poll_argv(recipe.web_port, recipe.health_path,
                                      self.cfg.health_timeout_secs,
                                      host=recipe.web_service), service="forge")
        if h.exit_code != 0:
            self.store.set_env_state(run_id, "failed")
            return None
        hp = env.port(recipe.web_service, recipe.web_port)
        from forge.envreg import web_url
        url = web_url(hp) if hp else None
        self.store.set_env_state(run_id, "live", url)
        return url
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session.py::test_start_provisions_and_registers_url -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/session.py tests/test_session.py
git commit -m "feat(session): SessionManager.start provisions a session env"
```

---

### Task 9: SessionManager.turn (stream + verify)

**Files:**
- Modify: `src/forge/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- `SessionManager.turn(run_id, prompt) -> Iterator[TurnEvent]`:
  - Reject (yield `error` kind=`busy`) if a turn is already in flight for `run_id` (tracked in `self._active: set`).
  - Persist the user message (`store.add_message(run_id,"user",prompt)`).
  - Build the prompt: first turn uses `build_task_prompt(prompt, app_url)`; later turns pass `prompt` plus the same role preamble — use `build_task_prompt(prompt, app_url)` each time (the worker `--resume` carries history). `app_url` = `http://{web_service}:{web_port}` if any.
  - Resume with the stored `claude_session_id` (from `get_run`).
  - Stream `env.exec_stream(worker_stream_cmd(prompt2, None, sid), service="forge")`; for each line, `parse_stream_line`; emit `narration`/`tool` events; capture the `result` event's `WorkerResult`.
  - Persist `claude_session_id` from the result.
  - If `result.auth_error`: yield `error` kind=`auth`; persist assistant message; touch env; return.
  - Run verify (if `has_real_verification`): yield `verify` event `{ok, failed:[names], output}`.
  - Re-poll health to refresh URL (yield `url`).
  - Persist assistant summary message (`result.result_text`) with meta `{cost, diff_files}`; `diff_files` from `git diff --name-only HEAD` count.
  - yield `done` `{message, diff_files, verify_ok}`. `store.touch_env(run_id)`; set run state back to `running`.
  - Always remove `run_id` from `self._active` in a `finally`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_session.py
def test_turn_streams_verifies_and_persists(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    events = list(mgr.turn("r1", "make the header bold"))
    kinds = [e.kind for e in events]
    assert "narration" in kinds
    assert "verify" in kinds
    assert events[-1].kind == "done"
    msgs = store.list_messages("r1")
    assert msgs[0]["role"] == "user" and "header" in msgs[0]["content"]
    assert msgs[-1]["role"] == "assistant"
    assert store.get_run("r1")["claude_session_id"] == "sess-1"


def test_turn_rejects_concurrent_turn(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    mgr._active.add("r1")
    out = list(mgr.turn("r1", "x"))
    assert out[0].kind == "error" and out[0].data["kind"] == "busy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session.py -q`
Expected: FAIL (`turn`/`_active` not defined).

- [ ] **Step 3: Implement**

```python
# add to SessionManager.__init__:  self._active = set()
# add import: from forge.worker import parse_stream_line  (already) and:
from forge.commands import worker_stream_cmd

    def _app_url(self, run_id):
        env_row = self.store.get_env(run_id)
        svc, port = env_row.get("web_service"), env_row.get("web_port")
        return f"http://{svc}:{port}" if svc and port else None

    def turn(self, run_id, prompt):
        if run_id in self._active:
            yield TurnEvent("error", {"kind": "busy", "detail": "a turn is in flight"})
            return
        self._active.add(run_id)
        try:
            self.store.add_message(run_id, "user", prompt)
            self.store.set_state(run_id, "running")
            env = self._env_for(run_id)
            sid = self.store.get_run(run_id).get("claude_session_id")
            full = build_task_prompt(prompt, self._app_url(run_id))
            yield TurnEvent("phase", {"name": "agent", "label": "Agent working"})
            result = None
            for line in env.exec_stream(worker_stream_cmd(full, None, sid),
                                        service="forge"):
                ev = parse_stream_line(line)
                if ev is None:
                    continue
                if ev.kind in ("narration", "tool"):
                    yield TurnEvent(ev.kind, {"text": ev.text})
                elif ev.kind == "result":
                    result = ev.result
            if result is None:
                yield TurnEvent("error", {"kind": "worker", "detail": "no result event"})
                return
            if result.session_id:
                self.store.set_session_fields(run_id, claude_session_id=result.session_id)
            if result.auth_error:
                self.store.add_message(run_id, "system", "Claude auth/usage problem.")
                yield TurnEvent("error", {"kind": "auth", "detail": result.result_text[:300]})
                return
            verify_ok = True
            if getattr(self, "_verify_plan", None) and self._verify_plan.has_real_verification:
                self.store.set_state(run_id, "verifying")
                failures = self._run_verify(env)
                verify_ok = not failures
                yield TurnEvent("verify", {"ok": verify_ok,
                                           "failed": [n for n, _ in failures],
                                           "output": "\n\n".join(o for _, o in failures)[:4000]})
            recipe_url = self._refresh_url(env, run_id)
            if recipe_url:
                yield TurnEvent("url", {"web_url": recipe_url})
            diff_files = self._diff_file_count(env)
            self.store.add_message(run_id, "assistant", result.result_text or "(done)",
                                   meta={"cost": result.total_cost_usd,
                                         "diff_files": diff_files, "verify_ok": verify_ok})
            self.store.set_state(run_id, "running")
            self.store.touch_env(run_id)
            yield TurnEvent("done", {"message": result.result_text,
                                     "diff_files": diff_files, "verify_ok": verify_ok})
        finally:
            self._active.discard(run_id)

    def _run_verify(self, env):
        failures = []
        for c in self._verify_plan.commands:
            res = env.exec(c.argv, service="forge")
            if res.exit_code != 0:
                failures.append((c.name, (res.stdout + res.stderr)[-2000:]))
        return failures

    def _refresh_url(self, env, run_id):
        row = self.store.get_env(run_id)
        if not row.get("web_service"):
            return None
        hp = env.port(row["web_service"], row["web_port"])
        from forge.envreg import web_url
        url = web_url(hp) if hp else row.get("web_url")
        if url:
            self.store.set_env_state(run_id, "live", url)
        return url

    def _diff_file_count(self, env) -> int:
        env.exec(["bash", "-lc", "git add -A -N"], service="forge")
        r = env.exec(["bash", "-lc", "git diff --name-only HEAD"], service="forge")
        return len([x for x in r.stdout.splitlines() if x.strip()])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/session.py tests/test_session.py
git commit -m "feat(session): turn() streams worker, verifies, persists transcript"
```

---

### Task 10: SessionManager.diff / open_pr / end / reconcile

**Files:**
- Modify: `src/forge/session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- `diff(run_id) -> str` — `git add -A -N && git diff HEAD` via `env.exec`, returns the patch text.
- `open_pr(run_id) -> dict` — guard `has_changes_cmd`; if empty return `{"ok": False, "reason": "no_changes"}`. Else commit (as configured author) → push → `gh pr create` (draft if no real verification). Returns `{"ok": True, "pr_url": ..., "draft": bool}` or `{"ok": False, "reason": "push_failed"|"pr_failed"}`. Persist `pr_url` via `set_state(run_id, state, pr_url=...)` and add a `system` message.
- `end(run_id) -> None` — `lifecycle.reap_project(self.store, run_id)`; run `recipe.host_post` best-effort (re-resolve recipe to get host_post, or store it — simplest: best-effort `supabase stop` only if `next-supabase`; for v1 just reap_project).
- `can_start() -> tuple[bool, str]` — counts `store.list_envs(states=("live","starting"))`; returns `(False, "max_live_sessions reached (N); end an idle session first")` if `>= cfg.max_live_sessions`, else `(True, "")`.
- `reconcile() -> None` — for each env in `list_envs(states=("live","starting","running"))`, check `docker compose -p forge-<id> ps -q` (via a injected `ps_checker` defaulting to a real subprocess); if no containers, `store.set_env_state(run_id,"failed")` + `store.set_state(run_id,"failed")`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_session.py
def test_open_pr_only_on_demand_and_commits(tmp_path):
    mgr, store = _mgr(tmp_path)
    list(mgr.start("r1", "o/r", "github"))
    list(mgr.turn("r1", "change"))
    # FakeEnv.exec reports porcelain has changes + pr url on gh pr create
    res = mgr.open_pr("r1")
    assert res["ok"] is True


def test_can_start_enforces_cap(tmp_path):
    mgr, store = _mgr(tmp_path)
    mgr.cfg.max_live_sessions = 1
    store.create_env("a", "forge-a", None, 3000, "live")
    ok, msg = mgr.can_start()
    assert ok is False and "max" in msg.lower()
```

Extend `FakeEnv.exec` to return a PR url for `gh pr create`:
```python
        if "pr" in joined and "create" in joined:
            return ExecResult(0, "https://github.com/o/r/pull/1\n", "")
        if "push" in joined:
            return ExecResult(0, "", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session.py -q`
Expected: FAIL (`open_pr`/`can_start` not defined).

- [ ] **Step 3: Implement**

```python
# add to session.py imports: import shlex
    def diff(self, run_id) -> str:
        env = self._env_for(run_id)
        r = env.exec(["bash", "-lc", "git add -A -N && git diff HEAD"], service="forge")
        return r.stdout

    def can_start(self):
        n = len(self.store.list_envs(states=("live", "starting")))
        if n >= self.cfg.max_live_sessions:
            return (False, f"max_live_sessions reached ({n}); end an idle session first")
        return (True, "")

    def open_pr(self, run_id) -> dict:
        env = self._env_for(run_id)
        if env.exec(cmd.has_changes_cmd(), service="forge").stdout.strip() == "":
            return {"ok": False, "reason": "no_changes"}
        run = self.store.get_run(run_id)
        name = self.cfg.git_author_name or "Forge User"
        email = self.cfg.git_author_email or "forge@local"
        title = f"forge: {run.get('title') or run.get('repo')}"
        for cc in cmd.commit_cmds(title, name, email):
            env.exec(cc, service="forge")
        if env.exec(cmd.push_cmd(run["branch"]), service="forge").exit_code != 0:
            return {"ok": False, "reason": "push_failed"}
        draft = not (getattr(self, "_verify_plan", None)
                     and self._verify_plan.has_real_verification)
        body = f"# {title}\n\nrun: {run_id}\nbranch: {run['branch']}\n"
        env.exec(["bash", "-lc", f"printf '%s' {shlex.quote(body)} > /work/report.md"],
                 service="forge")
        pr = env.exec(cmd.pr_create_cmd(title, "/work/report.md", draft), service="forge")
        lines = pr.stdout.strip().splitlines()
        pr_url = lines[-1] if (pr.exit_code == 0 and lines) else None
        if not pr_url:
            return {"ok": False, "reason": "pr_failed"}
        self.store.set_state(run_id, self.store.get_run(run_id)["state"], pr_url=pr_url)
        self.store.add_message(run_id, "system", f"PR opened{' (draft)' if draft else ''}: {pr_url}")
        return {"ok": True, "pr_url": pr_url, "draft": draft}

    def end(self, run_id) -> None:
        lifecycle.reap_project(self.store, run_id)

    def reconcile(self, ps_checker=None) -> None:
        import subprocess
        def _default(project):
            r = subprocess.run(["docker", "compose", "-p", project, "ps", "-q"],
                               capture_output=True, text=True)
            return bool(r.stdout.strip())
        check = ps_checker or _default
        for e in self.store.list_envs(states=("live", "starting", "running")):
            if not check(f"forge-{e['run_id']}"):
                self.store.set_env_state(e["run_id"], "failed")
                self.store.set_state(e["run_id"], "failed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/session.py tests/test_session.py
git commit -m "feat(session): diff, on-demand open_pr, end, cap + restart reconcile"
```

---

## PHASE C — Web API

### Task 11: FastAPI skeleton + read endpoints + deps

**Files:**
- Modify: `pyproject.toml` (deps)
- Create: `src/forge/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- `webapp.create_app(config, store, manager) -> FastAPI` — dependency-injected so tests pass a mocked `manager` and a temp `store`.
- Read endpoints: `GET /api/repos?q=`, `GET /api/sessions`, `GET /api/sessions/{id}` (state, pr_url, web_url, messages), `GET /api/sessions/{id}/diff`, `GET /api/sessions/{id}/verify` (latest `verify` from the last assistant message meta or events).
- Static: mount `web/dist` at `/` if it exists (SPA). API under `/api` takes precedence.
- Never include tokens in any response.

- [ ] **Step 1: Update deps + write the failing test**

`pyproject.toml`:
```toml
[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]
web = ["fastapi>=0.110", "uvicorn[standard]>=0.29"]
```
Install: `pip install -e ".[dev,web]"`

```python
# tests/test_webapp.py
from fastapi.testclient import TestClient
from forge.webapp import create_app
from forge.config import Config
from forge.store import Store


class FakeManager:
    def __init__(self, store): self.store = store
    def can_start(self): return (True, "")


def _client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    return TestClient(create_app(cfg, store, FakeManager(store))), store


def test_list_sessions_empty(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/sessions").json() == []


def test_session_detail_includes_messages_not_tokens(tmp_path):
    client, store = _client(tmp_path)
    store.create_run("r1", "o/r", "", "forge/x")
    store.add_message("r1", "user", "hello")
    body = client.get("/api/sessions/r1").json()
    assert body["run_id"] == "r1"
    assert body["messages"][0]["content"] == "hello"
    assert "oauth_token" not in str(body) and "gh_token" not in str(body)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webapp.py -q`
Expected: FAIL (`forge.webapp` missing).

- [ ] **Step 3: Implement read endpoints**

```python
# src/forge/webapp.py
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from forge import repos


def create_app(config, store, manager) -> FastAPI:
    app = FastAPI(title="forge")

    @app.get("/api/repos")
    def get_repos(q: str = ""):
        return repos.list_repos(config.workspace_dir, q)

    @app.get("/api/sessions")
    def list_sessions():
        return store.list_sessions()

    @app.get("/api/sessions/{run_id}")
    def session_detail(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "no such session")
        env = store.get_env(run_id)
        return {"run_id": run_id, "repo": run.get("repo"), "title": run.get("title"),
                "state": run.get("state"), "pr_url": run.get("pr_url"),
                "repo_source": run.get("repo_source"),
                "web_url": env.get("web_url"), "env_state": env.get("state"),
                "messages": store.list_messages(run_id)}

    @app.get("/api/sessions/{run_id}/diff")
    def session_diff(run_id: str):
        return {"diff": manager.diff(run_id)}

    @app.get("/api/sessions/{run_id}/verify")
    def session_verify(run_id: str):
        msgs = [m for m in store.list_messages(run_id) if m["role"] == "assistant"]
        meta = msgs[-1]["meta"] if msgs else {}
        return {"verify_ok": meta.get("verify_ok"), "diff_files": meta.get("diff_files")}

    dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")
    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_webapp.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/forge/webapp.py tests/test_webapp.py
git commit -m "feat(web): FastAPI app skeleton + read endpoints + deps"
```

---

### Task 12: Streaming endpoints (start + message → SSE)

**Files:**
- Modify: `src/forge/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- `POST /api/sessions` body `{repo, source}` → **SSE StreamingResponse** of `manager.start(run_id, repo, source)` events. Generates `run_id` server-side, includes it as the first `session` SSE event. Enforces `manager.can_start()` (returns 409 JSON `{error}` if at cap).
- `POST /api/sessions/{id}/messages` body `{prompt}` → **SSE StreamingResponse** of `manager.turn(id, prompt)` events.
- Helper `_sse(event_iter) -> StreamingResponse` formatting each `TurnEvent` as `event: <kind>\ndata: <json>\n\n`. Media type `text/event-stream`. Runs the sync generator in a threadpool (Starlette default for sync iterators).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_webapp.py
from forge.session import TurnEvent
import uuid, json as _json


class StreamManager(FakeManager):
    def can_start(self): return (True, "")
    def start(self, run_id, repo, source):
        yield TurnEvent("phase", {"name": "clone"})
        yield TurnEvent("url", {"web_url": "http://localhost:5599"})
    def turn(self, run_id, prompt):
        yield TurnEvent("narration", {"text": "editing"})
        yield TurnEvent("done", {"message": "ok", "diff_files": 1})


def _stream_client(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    return TestClient(create_app(cfg, store, StreamManager(store)))


def test_start_streams_sse(tmp_path):
    client = _stream_client(tmp_path)
    with client.stream("POST", "/api/sessions", json={"repo": "o/r", "source": "github"}) as r:
        body = "".join(chunk for chunk in r.iter_text())
    assert "event: url" in body
    assert "http://localhost:5599" in body


def test_message_streams_sse(tmp_path):
    client = _stream_client(tmp_path)
    with client.stream("POST", "/api/sessions/r1/messages", json={"prompt": "x"}) as r:
        body = "".join(chunk for chunk in r.iter_text())
    assert "event: narration" in body and "event: done" in body


def test_start_at_cap_returns_409(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    class Full(StreamManager):
        def can_start(self): return (False, "max reached")
    client = TestClient(create_app(cfg, store, Full(store)))
    r = client.post("/api/sessions", json={"repo": "o/r", "source": "github"})
    assert r.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webapp.py -q`
Expected: FAIL (POST routes not defined).

- [ ] **Step 3: Implement**

```python
# add to webapp.py
import json, uuid
from fastapi import Request
from fastapi.responses import StreamingResponse, JSONResponse


def _sse(event_iter):
    def gen():
        for ev in event_iter:
            yield f"event: {ev.kind}\ndata: {json.dumps(ev.data)}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
```
Inside `create_app`:
```python
    @app.post("/api/sessions")
    async def start_session(req: Request):
        body = await req.json()
        ok, msg = manager.can_start()
        if not ok:
            return JSONResponse({"error": msg}, status_code=409)
        run_id = uuid.uuid4().hex
        def events():
            yield type("E", (), {"kind": "session", "data": {"run_id": run_id}})()
            yield from manager.start(run_id, body["repo"], body.get("source", "github"))
        return _sse(events())

    @app.post("/api/sessions/{run_id}/messages")
    async def post_message(run_id: str, req: Request):
        body = await req.json()
        return _sse(manager.turn(run_id, body["prompt"]))
```
(`_sse`'s `ev.kind`/`ev.data` works for both `TurnEvent` and the inline session event.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_webapp.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/webapp.py tests/test_webapp.py
git commit -m "feat(web): SSE streaming for session start + turn"
```

---

### Task 13: Action endpoints (pr / stop / delete)

**Files:**
- Modify: `src/forge/webapp.py`
- Test: `tests/test_webapp.py`

**Interfaces:**
- `POST /api/sessions/{id}/pr` → `manager.open_pr(id)` JSON; 400 if `{"ok": False}`.
- `POST /api/sessions/{id}/stop` → `manager.stop(id)` (new manager method: `self._env_for(id).cancel()` + discard from `_active`); returns `{"stopped": True}`.
- `DELETE /api/sessions/{id}` → `manager.end(id)`; returns `{"ended": True}`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_webapp.py
class ActionManager(StreamManager):
    def open_pr(self, run_id): return {"ok": True, "pr_url": "u", "draft": True}
    def stop(self, run_id): return None
    def end(self, run_id): return None


def test_pr_stop_delete(tmp_path):
    cfg = Config(runs_dir=tmp_path / "runs", workspace_dir=tmp_path / "ws")
    store = Store(cfg.runs_dir / "forge.db")
    client = TestClient(create_app(cfg, store, ActionManager(store)))
    assert client.post("/api/sessions/r1/pr").json()["pr_url"] == "u"
    assert client.post("/api/sessions/r1/stop").json()["stopped"] is True
    assert client.delete("/api/sessions/r1").json()["ended"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_webapp.py -q`
Expected: FAIL (routes / `manager.stop` not defined).

- [ ] **Step 3: Implement**

Add `stop` to `SessionManager`:
```python
    def stop(self, run_id) -> None:
        self._env_for(run_id).cancel()
        self._active.discard(run_id)
```
Add routes:
```python
    @app.post("/api/sessions/{run_id}/pr")
    def open_pr(run_id: str):
        res = manager.open_pr(run_id)
        if not res.get("ok"):
            return JSONResponse(res, status_code=400)
        return res

    @app.post("/api/sessions/{run_id}/stop")
    def stop(run_id: str):
        manager.stop(run_id)
        return {"stopped": True}

    @app.delete("/api/sessions/{run_id}")
    def end(run_id: str):
        manager.end(run_id)
        return {"ended": True}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_webapp.py tests/test_session.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/webapp.py src/forge/session.py tests/test_webapp.py
git commit -m "feat(web): pr/stop/delete action endpoints + manager.stop"
```

---

### Task 14: `forge web` CLI command (server + proxy + reaper)

**Files:**
- Modify: `src/forge/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- `forge web [--runs-dir runs] [--host 127.0.0.1] [--port 8099] [--no-open]`.
- `_cmd_web(args)`: validate tokens (same check as `_cmd_run`); build `Config.from_env`, `Store`, `SessionManager`, `create_app`; on FastAPI startup event call `manager.reconcile()` and ensure the Caddy proxy + start a background idle-reaper thread (reuse `lifecycle.reap_idle` on an interval, same as `_cmd_serve`); then `uvicorn.run(app, host, port)`.
- Keep it import-light: import `uvicorn`/`webapp` **inside** `_cmd_web` so the CLI works without the `web` extra installed.
- The reaper/proxy wiring is factored into `webapp.attach_background(app, config, store, manager)` so it's unit-testable without launching uvicorn.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_cli.py
def test_web_subcommand_parses(monkeypatch):
    import forge.cli as cli
    called = {}
    def fake_web(args):
        called["host"], called["port"] = args.host, args.port
        return 0
    monkeypatch.setattr(cli, "_cmd_web", fake_web)
    # re-register parser path: main dispatches to func; ensure 'web' is wired
    rc = cli.main(["web", "--port", "9090", "--host", "127.0.0.1", "--runs-dir", "runs"])
    assert rc == 0 and called["port"] == 9090
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py::test_web_subcommand_parses -q`
Expected: FAIL (no `web` subparser / `_cmd_web`).

- [ ] **Step 3: Implement**

In `cli.py`, add the parser in `main`:
```python
    webp = sub.add_parser("web")
    webp.add_argument("--runs-dir", default="runs")
    webp.add_argument("--host", default="127.0.0.1")
    webp.add_argument("--port", type=int, default=8099)
    webp.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)
    webp.set_defaults(func=_cmd_web)
```
And:
```python
def _cmd_web(args) -> int:
    cfg = Config.from_env(Path(args.runs_dir))
    if not cfg.oauth_token or not cfg.gh_token:
        print("error: CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN must be set", file=sys.stderr)
        return 1
    _populate_identity(cfg)
    from forge.session import SessionManager
    from forge.webapp import create_app, attach_background
    import uvicorn
    store = Store(cfg.runs_dir / "forge.db")
    manager = SessionManager(cfg, store, LocalHost())
    app = create_app(cfg, store, manager)
    attach_background(app, cfg, store, manager)
    url = f"http://{args.host}:{args.port}"
    print(f"forge web → {url}")
    if args.open and sys.platform == "darwin":
        subprocess.run(["open", url], capture_output=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0
```
In `webapp.py` add (uses existing `proxy`/`lifecycle`):
```python
def attach_background(app, config, store, manager):
    import threading, time
    from forge import lifecycle, proxy
    from datetime import datetime

    @app.on_event("startup")
    def _startup():
        manager.reconcile()
        caddyfile = config.runs_dir / "Caddyfile"
        caddyfile.write_text(proxy.caddy_config(
            proxy.routes_for(store.list_envs(states=("live",)), domain=config.proxy_domain),
            config.proxy_port))
        proxy.ensure_proxy(str(caddyfile), config.proxy_port)
        def reap_loop():
            while True:
                lifecycle.reap_idle(store, datetime.utcnow(), config.env_ttl_secs)
                live = store.list_envs(states=("live",))
                proxy.connect_networks([e["run_id"] for e in live])
                caddyfile.write_text(proxy.caddy_config(
                    proxy.routes_for(live, domain=config.proxy_domain), config.proxy_port))
                proxy.reload_proxy()
                time.sleep(30)
        threading.Thread(target=reap_loop, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/forge/cli.py src/forge/webapp.py tests/test_cli.py
git commit -m "feat(cli): forge web — serve UI + proxy + idle reaper"
```

---

## PHASE D — Frontend (React + Vite)

> Frontend components follow the **frontend-design** skill's principles at execution time (deliberate token system, no generic AI aesthetics). The plan fixes structure, data flow, and the SSE contract; visual polish is applied during implementation. Pure logic (SSE parsing, API client) is unit-tested with Vitest; components are verified by `npm run build` + a manual smoke against a running `forge web`.

### Task 15: Vite scaffold + typed API/SSE client

**Files:**
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/index.html`, `web/src/types.ts`, `web/src/api.ts`, `web/src/api.test.ts`

**Interfaces:**
- `web/src/types.ts`: `SessionSummary`, `SessionDetail`, `Message`, `Repo`, `SseEvent = {kind: string; data: any}`.
- `web/src/api.ts`:
  - `listRepos(q): Promise<Repo[]>`, `listSessions(): Promise<SessionSummary[]>`, `getSession(id): Promise<SessionDetail>`, `getDiff(id): Promise<string>`, `openPr(id)`, `stopTurn(id)`, `endSession(id)`.
  - `streamPost(path, body, onEvent: (e: SseEvent)=>void): Promise<void>` — POST + read `text/event-stream` via `fetch` + `ReadableStream` reader, parsing `event:`/`data:` frames.
  - `parseSseChunk(buffer: string): {events: SseEvent[]; rest: string}` — pure, unit-tested.
- `vite.config.ts`: `base: "/"`, `build.outDir: "dist"`, dev `server.proxy` `/api` → `http://127.0.0.1:8099`.

- [ ] **Step 1: Scaffold + write the failing Vitest**

`web/package.json` (key parts):
```json
{
  "name": "forge-web", "private": true, "type": "module",
  "scripts": {"dev": "vite", "build": "vite build", "test": "vitest run"},
  "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
  "devDependencies": {"@vitejs/plugin-react": "^4.3.1", "typescript": "^5.5.4",
    "vite": "^5.4.0", "vitest": "^2.0.5", "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0"}
}
```
`web/src/api.test.ts`:
```ts
import { expect, test } from "vitest";
import { parseSseChunk } from "./api";

test("parses complete SSE frames and keeps remainder", () => {
  const buf = 'event: narration\ndata: {"text":"hi"}\n\nevent: done\ndata: {"m":1}\n\nevent: partial\ndata: {';
  const { events, rest } = parseSseChunk(buf);
  expect(events).toEqual([
    { kind: "narration", data: { text: "hi" } },
    { kind: "done", data: { m: 1 } },
  ]);
  expect(rest.startsWith("event: partial")).toBe(true);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npm install && npm test`
Expected: FAIL (`parseSseChunk` not defined).

- [ ] **Step 3: Implement `api.ts`**

```ts
import type { Repo, SessionSummary, SessionDetail, SseEvent } from "./types";

export function parseSseChunk(buffer: string): { events: SseEvent[]; rest: string } {
  const events: SseEvent[] = [];
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  for (const block of parts) {
    let kind = "message"; let data: any = {};
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) kind = line.slice(6).trim();
      else if (line.startsWith("data:")) { try { data = JSON.parse(line.slice(5).trim()); } catch {} }
    }
    events.push({ kind, data });
  }
  return { events, rest };
}

async function j<T>(p: string): Promise<T> { return (await fetch(p)).json(); }
export const listRepos = (q = "") => j<Repo[]>(`/api/repos?q=${encodeURIComponent(q)}`);
export const listSessions = () => j<SessionSummary[]>("/api/sessions");
export const getSession = (id: string) => j<SessionDetail>(`/api/sessions/${id}`);
export const getDiff = async (id: string) => (await j<{diff: string}>(`/api/sessions/${id}/diff`)).diff;
export const openPr = (id: string) => fetch(`/api/sessions/${id}/pr`, {method: "POST"}).then(r => r.json());
export const stopTurn = (id: string) => fetch(`/api/sessions/${id}/stop`, {method: "POST"});
export const endSession = (id: string) => fetch(`/api/sessions/${id}`, {method: "DELETE"});

export async function streamPost(path: string, body: unknown,
    onEvent: (e: SseEvent) => void): Promise<void> {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify(body)});
  const reader = res.body!.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    const { events, rest } = parseSseChunk(buf); buf = rest;
    events.forEach(onEvent);
  }
}
```
Plus `types.ts`, `vite.config.ts`, `tsconfig.json`, `index.html` with concrete content.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/package.json web/vite.config.ts web/tsconfig.json web/index.html web/src/types.ts web/src/api.ts web/src/api.test.ts
git commit -m "feat(web-ui): vite scaffold + typed API/SSE client (tested)"
```

---

### Task 16: Sidebar + repo picker

**Files:** Create `web/src/Sidebar.tsx`. Modify `web/src/App.tsx` (shell).

**Interfaces:**
- `Sidebar({sessions, activeId, onSelect, onNewSession})` — renders session rows (title/repo, state badge, URL link); "+ New" opens a repo picker (search box → `listRepos(q)`; list of `{name, remote}` rows + a free-text `owner/repo` field). Selecting a local repo calls `onNewSession({repo: path, source: "local"})`; typing `owner/repo` calls `onNewSession({repo, source: "github"})`.

- [ ] **Step 1: Implement `Sidebar.tsx`** (state badges: `provisioning/working/live/idle/failed`; live rows show the URL as a link with `↗`). Wire `App.tsx` to hold `sessions`, poll `listSessions()` every 4s, and manage `activeId`.

- [ ] **Step 2: Verify build** — Run: `cd web && npm run build` → Expected: succeeds, `dist/` produced.

- [ ] **Step 3: Commit**
```bash
git add web/src/Sidebar.tsx web/src/App.tsx
git commit -m "feat(web-ui): sessions sidebar + repo picker"
```

---

### Task 17: Chat panel + live streaming

**Files:** Create `web/src/Chat.tsx`. Modify `web/src/App.tsx`.

**Interfaces:**
- `Chat({session, onUrl, onTurnDone})`:
  - Renders `session.messages` as bubbles (`user`/`assistant`/`system`).
  - Input box → on submit, optimistically append the user bubble, then `streamPost('/api/sessions/{id}/messages', {prompt}, onEvent)`. Maintain a transient "live narration" bubble appended to from `narration`/`tool` events; on `verify` show a chip; on `url` call `onUrl`; on `done` finalize the assistant bubble and call `onTurnDone` (which re-fetches `getSession`).
  - New-session flow: when App creates a session, it calls `streamPost('/api/sessions', {repo, source}, ...)` and routes `session` (capture run_id), `phase`, `url`, `error` events into the same live view.
  - Buttons: **Open PR** (`openPr`), **Stop** (`stopTurn`), **End** (`endSession`). Disable Open PR while a turn streams.
  - `error` events render as a red `system` bubble with a Retry affordance.

- [ ] **Step 1: Implement `Chat.tsx`** with the streaming reducer described above.
- [ ] **Step 2: Verify build** — `cd web && npm run build` → succeeds.
- [ ] **Step 3: Commit**
```bash
git add web/src/Chat.tsx web/src/App.tsx
git commit -m "feat(web-ui): chat panel with live SSE streaming + actions"
```

---

### Task 18: Inspector tabs (Preview / Diff / Verify)

**Files:** Create `web/src/Inspector.tsx`. Modify `web/src/App.tsx`.

**Interfaces:**
- `Inspector({session})` with tabs:
  - **Preview**: `<iframe src={session.web_url}>` + "open in new tab ↗". Detect framing failure (onError / load-timeout) → fall back to a prominent link. Hidden when no `web_url`.
  - **Diff**: on tab open, `getDiff(id)`; render unified diff (parse into files; monospace, +/- line coloring). Refresh button.
  - **Verify**: show latest `verify_ok` (✅/❌) + `diff_files` from `getSession`/`verify` endpoint; expandable raw output captured from the last `verify` SSE event (held in App state).

- [ ] **Step 1: Implement `Inspector.tsx`** (simple unified-diff renderer: split on `diff --git`, color lines by leading `+`/`-`).
- [ ] **Step 2: Verify build** — `cd web && npm run build` → succeeds.
- [ ] **Step 3: Commit**
```bash
git add web/src/Inspector.tsx web/src/App.tsx
git commit -m "feat(web-ui): inspector tabs — preview, diff, verify"
```

---

### Task 19: Build wiring + gitignore

**Files:** Modify `.gitignore`, `pyproject.toml` (package data note), add `web/README.md`.

- [ ] **Step 1:** Add `web/node_modules/` to `.gitignore`. Decide `web/dist/`: commit the built `dist/` so `pip install` users get the UI without Node (document `npm run build` to refresh). Add a note in `web/README.md`: `npm install && npm run build` outputs `dist/`, served by `forge web`.
- [ ] **Step 2: Verify** the FastAPI static mount finds `web/dist` — Run: `cd web && npm run build && cd .. && python -c "from forge.webapp import create_app; from forge.config import Config; from forge.store import Store; from pathlib import Path; import tempfile; d=Path(tempfile.mkdtemp()); create_app(Config(runs_dir=d), Store(d/'f.db'), None)"`
Expected: no error (mount present).
- [ ] **Step 3: Commit**
```bash
git add .gitignore web/README.md web/dist
git commit -m "build(web-ui): commit built dist + ignore node_modules"
```

---

## PHASE E — Integration, smoke, docs

### Task 20: End-to-end integration test (fakes)

**Files:** Create `tests/test_session_e2e.py`

**Interfaces:** Consumes `SessionManager` + the `FakeHost`/`FakeEnv` from `tests/test_session.py` (import or duplicate minimal fakes). Asserts the full arc: `start` → `turn` → `turn` → `open_pr` → `end`, verifying (a) two assistant messages persisted, (b) `claude_session_id` stable across turns, (c) `open_pr` only creates a PR when called (no PR after turns), (d) `end` marks env reaped.

- [ ] **Step 1: Write the test** (full lifecycle assertions).
- [ ] **Step 2: Run** — `python -m pytest tests/test_session_e2e.py -q` → iterate to green.
- [ ] **Step 3: Commit**
```bash
git add tests/test_session_e2e.py
git commit -m "test(session): full lifecycle integration (start→turns→pr→end)"
```

---

### Task 21: Real-Docker node-web session smoke (gated)

**Files:** Create `tests/test_session_smoke.py`

**Interfaces:** Mirror `tests/test_node_web_smoke.py` gating (skip unless Docker + `forge-worker` image present). Clone a tiny known node app fixture (or reuse the existing smoke fixture), `SessionManager.start`, run one `turn` with a visible-change prompt, assert: env state `live`, `web_url` reachable serving changed content, `diff(run_id)` non-empty, and that **no PR** exists until `open_pr` is called (assert against a scratch/fork or mark PR creation xfail offline). Reuse the existing smoke harness helpers.

- [ ] **Step 1: Write the gated smoke test** (use the same `_docker_available()`/image-check pattern as `test_node_web_smoke.py`).
- [ ] **Step 2: Run** — `python -m pytest tests/test_session_smoke.py -q` (skips cleanly without Docker; passes with it).
- [ ] **Step 3: Commit**
```bash
git add tests/test_session_smoke.py
git commit -m "test(session): gated real-Docker node-web session smoke"
```

---

### Task 22: Docs

**Files:** Modify `README.md`; the spec already lives in `docs/specs/`.

- [ ] **Step 1:** Add a "Chat UI (`forge web`)" section to `README.md`: one-time `cd web && npm install && npm run build`, then `forge web` → open `http://127.0.0.1:8099`; explain workspace folder (`FORGE_WORKSPACE_DIR`, default `~/forge-repos`), `FORGE_MAX_SESSIONS`, on-demand PR, one-env-per-session. Update the project-layout block with `session.py`, `webapp.py`, `repos.py`, `probing.py`, `web/`.
- [ ] **Step 2: Verify** full suite green — Run: `python -m pytest -q` → Expected: all pass (skips for Docker-gated).
- [ ] **Step 3: Commit**
```bash
git add README.md
git commit -m "docs: forge web chat UI usage + layout"
```

---

## Self-Review

**Spec coverage:**
- One-session-one-env + concurrent sessions → Tasks 8, 10 (`can_start`, no superseded-reap). ✅
- Repo picker (local folder + GitHub) → Tasks 5, 6 (`clone_local`, `list_repos`), 16. ✅
- Multi-turn resume → Task 9 (`claude_session_id`, `worker_stream_cmd --resume`). ✅
- On-demand PR → Task 10 (`open_pr`), 13, 17. ✅
- Live streaming narration → Tasks 2, 3, 4, 9, 12, 15, 17. ✅
- Diff viewer / sidebar / preview / verify → Tasks 11/13 (endpoints), 16/17/18 (UI). ✅
- Restart-safe reconcile + idle reaper + proxy → Tasks 10, 14. ✅
- Resource cap + heavy-recipe handling → Tasks 6 (`max_live_sessions`), 10, 12 (409). ✅
- Security (127.0.0.1, no tokens to browser) → Tasks 11, 14. ✅
- Error handling surfaced as system messages/errors → Tasks 8, 9 (`error` events), 17 (red bubble + retry). ✅
- Testing (unit/integration/smoke) → Tasks 1-14 unit, 20 integration, 21 smoke. ✅

**Type consistency:** `TurnEvent(kind, data)` used uniformly across `session.py`, `webapp._sse`, and the TS `SseEvent {kind, data}`. `worker_stream_cmd` signature matches `worker_cmd`. `parse_stream_line → StreamEvent(kind, text, result)` consumed in Task 9. `SessionManager(config, store, host, env_factory, clock)` matches `ComposeOrchestrator`'s shape and the Task 8/9/10 tests. `_env_for`/`_register`/`_refresh_url`/`_verify_plan` names consistent across Tasks 8-10.

**Placeholder scan:** No TBD/TODO; every code step carries runnable code; Phase D component tasks intentionally use build-verification (noted) rather than fake code, with pure logic (api.ts) fully TDD'd in Task 15.

**Note on `_verify_plan`:** stored on the instance at `start` (Task 8) and read in `turn`/`open_pr`. Across a `forge web` restart it is recomputed lazily — Task 9/10 guard with `getattr(self, "_verify_plan", None)`; if absent (post-restart turn), `turn` should recompute it from the workspace. Add to Task 9 implementation: if `_verify_plan` missing, rebuild via `parse_verify(build_probe(...))` against the run's workspace before verifying.
