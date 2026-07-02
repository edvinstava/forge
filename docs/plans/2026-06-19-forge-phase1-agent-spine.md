# Forge Phase 1 — Agent Spine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI-invoked run that takes a GitHub repo + a task, runs a containerized `claude -p` worker against a fresh clone, has the orchestrator verify the result and loop until the gate passes or budget is exhausted, then opens a PR — all locally, on the subscription.

**Architecture:** One Python process. **Everything executes inside one Docker container per run** (`gh clone`, branch, `claude -p`, tests, `git`, `gh pr create`), reached through a single `ContainerRunner` adapter over the `docker` CLI. Every other module is pure logic — command-builders, parsers, the budget tracker, the state machine — unit-tested without Docker. The orchestrator owns all verdicts (verify pass/fail, budget, PR); the worker only reports status. No Slack, no service-stack environments, no resume in Phase 1 (those are Phases 2/3).

**Tech Stack:** Python 3.11 (stdlib-first: `sqlite3`, `subprocess`, `dataclasses`, `pathlib`, `json`, `re`); `pytest` for tests; `docker` CLI; `gh` CLI; `claude` CLI (inside the container only).

## Global Constraints

- **Python 3.11**, stdlib-first. Only third-party dep is `pytest` (dev). No Postgres, no Redis, no Docker SDK (shell out to `docker`).
- **Backend is the `claude` CLI headless** (`claude -p --output-format json`), never the Python Agent SDK.
- **Auth into the container**: pass `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`). **`ANTHROPIC_API_KEY` must never be set in the container** (it would bypass the subscription).
- **GitHub**: pass `GH_TOKEN` (from `gh auth token` on the host) into the container; PRs via `gh pr create`. No GitHub App.
- **Orchestrator owns every verdict.** The worker emits status only; Forge decides verify pass/fail, budget, and whether a PR is draft.
- **Verify gate is the moat.** If no real verification command can be found, Forge opens a **draft** PR and warns — never a non-draft PR.
- **Budget caps (defaults): 20 iterations, 1800 s wall-clock.** A `claude` auth/usage error is a hard stop (`STOPPED_BUDGET` with reason `usage`).
- **Branch name**: `forge/<slug>`; commits authored `Forge <forge@localhost>`.
- **Run artifacts** under `runs/<run_id>/`: `meta.json`, `timeline.md`, `agent.log`, `result.json`, `report.md`. SQLite at `runs/forge.db`.
- **No secrets in git or logs.** `timeline.md`/`agent.log` must never contain `CLAUDE_CODE_OAUTH_TOKEN` or `GH_TOKEN` values.

---

## File Structure

```
forge/
  pyproject.toml                     # package metadata + pytest config (Task 1)
  src/forge/
    __init__.py
    config.py                        # Config, Budget dataclasses (Task 1)
    runspec.py                       # RunSpec, run_id, branch slug (Task 2)
    store.py                         # SQLite run/event store (Task 3)
    rundir.py                        # run dir layout + timeline.md writer (Task 4)
    verify.py                        # parse_verify -> VerifyPlan (Task 5)
    prompts.py                       # build_task_prompt, build_fix_prompt (Task 6)
    worker.py                        # parse_worker_result -> WorkerResult (Task 7)
    budget.py                        # Budget tracker + stop reasons (Task 8)
    commands.py                      # pure argv builders (Task 9)
    container.py                     # ContainerRunner Protocol + DockerRunner (Task 10)
    orchestrator.py                  # outer loop, owns verdicts (Task 11)
    cli.py                           # `forge run owner/repo "task"` (Task 12)
  worker-image/Dockerfile            # base image: node + claude + git + gh (Task 12)
  tests/
    test_config.py  test_runspec.py  test_store.py  test_rundir.py
    test_verify.py  test_prompts.py  test_worker.py  test_budget.py
    test_commands.py  test_container_smoke.py  test_orchestrator.py  test_cli.py
```

---

### Task 1: Scaffold + config

**Files:**
- Create: `forge/pyproject.toml`
- Create: `forge/src/forge/__init__.py` (empty)
- Create: `forge/src/forge/config.py`
- Test: `forge/tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Budget(max_iterations: int = 20, max_wall_secs: int = 1800)`; `Config(runs_dir: Path, image_tag: str = "forge-worker", budget: Budget = Budget(), oauth_token: str = "", gh_token: str = "")`; `Config.from_env(runs_dir: Path) -> Config` reading `CLAUDE_CODE_OAUTH_TOKEN` and `GH_TOKEN`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "forge"
version = "0.1.0"
requires-python = ">=3.11"

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
forge = "forge.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Write the failing test** (`tests/test_config.py`)

```python
import os
from pathlib import Path
from forge.config import Config, Budget


def test_defaults(tmp_path):
    cfg = Config(runs_dir=tmp_path)
    assert cfg.budget.max_iterations == 20
    assert cfg.budget.max_wall_secs == 1800
    assert cfg.image_tag == "forge-worker"


def test_from_env_reads_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    monkeypatch.setenv("GH_TOKEN", "gh-xyz")
    cfg = Config.from_env(tmp_path)
    assert cfg.oauth_token == "tok-abc"
    assert cfg.gh_token == "gh-xyz"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd forge && python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.config'`

- [ ] **Step 4: Write `src/forge/config.py`**

```python
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Budget:
    max_iterations: int = 20
    max_wall_secs: int = 1800


@dataclass
class Config:
    runs_dir: Path
    image_tag: str = "forge-worker"
    budget: Budget = field(default_factory=Budget)
    oauth_token: str = ""
    gh_token: str = ""

    @classmethod
    def from_env(cls, runs_dir: Path) -> "Config":
        return cls(
            runs_dir=Path(runs_dir),
            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            gh_token=os.environ.get("GH_TOKEN", ""),
        )
```

- [ ] **Step 5: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml src/forge/__init__.py src/forge/config.py tests/test_config.py
git commit -m "feat(phase1): project scaffold + Config/Budget"
```

---

### Task 2: RunSpec — parse repo + task, derive run_id and branch

**Files:**
- Create: `forge/src/forge/runspec.py`
- Test: `forge/tests/test_runspec.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RunSpec(repo: str, task: str, run_id: str, branch: str)`; `make_runspec(repo: str, task: str, run_id: str) -> RunSpec` — validates `repo` matches `owner/name`, raises `ValueError` otherwise; `branch` is `forge/<slug>` where slug is from the task (lowercased, non-alnum → `-`, collapsed, trimmed to 40 chars), suffixed with the first 8 chars of `run_id` for uniqueness.

- [ ] **Step 1: Write the failing test** (`tests/test_runspec.py`)

```python
import pytest
from forge.runspec import make_runspec


def test_valid_repo_and_branch_slug():
    rs = make_runspec("acme/internship-portal", "Fix the Org Unit tree!", "abcd1234ef")
    assert rs.repo == "acme/internship-portal"
    assert rs.branch.startswith("forge/fix-the-org-unit-tree")
    assert rs.branch.endswith("-abcd1234")  # run_id[:8] suffix


def test_invalid_repo_rejected():
    with pytest.raises(ValueError):
        make_runspec("not-a-repo", "task", "abcd1234ef")


def test_long_task_slug_truncated():
    rs = make_runspec("a/b", "x" * 100, "abcd1234ef")
    body = rs.branch[len("forge/"):-len("-abcd1234")]
    assert len(body) <= 40
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_runspec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.runspec'`

- [ ] **Step 3: Write `src/forge/runspec.py`**

```python
import re
from dataclasses import dataclass

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RunSpec:
    repo: str
    task: str
    run_id: str
    branch: str


def _slug(task: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    return s[:40].strip("-") or "task"


def make_runspec(repo: str, task: str, run_id: str) -> RunSpec:
    if not _REPO_RE.match(repo):
        raise ValueError(f"repo must be 'owner/name', got: {repo!r}")
    branch = f"forge/{_slug(task)}-{run_id[:8]}"
    return RunSpec(repo=repo, task=task, run_id=run_id, branch=branch)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_runspec.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/runspec.py tests/test_runspec.py
git commit -m "feat(phase1): RunSpec parsing + branch slug"
```

---

### Task 3: SQLite run/event store

**Files:**
- Create: `forge/src/forge/store.py`
- Test: `forge/tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Store(db_path: Path)` with `create_run(run_id, repo, task, branch) -> None`; `set_state(run_id, state: str, pr_url: str | None = None) -> None`; `add_event(run_id, etype: str, payload: dict) -> None`; `get_run(run_id) -> dict`; `list_events(run_id) -> list[dict]`. `get_run` returns keys `run_id, repo, task, branch, state, pr_url, created_at, updated_at`.

- [ ] **Step 1: Write the failing test** (`tests/test_store.py`)

```python
from forge.store import Store


def test_create_and_get_run(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "do x", "forge/do-x-r1")
    run = s.get_run("r1")
    assert run["repo"] == "a/b"
    assert run["state"] == "queued"
    assert run["pr_url"] is None


def test_set_state_and_pr_url(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    s.set_state("r1", "done", pr_url="https://github.com/a/b/pull/1")
    run = s.get_run("r1")
    assert run["state"] == "done"
    assert run["pr_url"].endswith("/pull/1")


def test_events_round_trip(tmp_path):
    s = Store(tmp_path / "forge.db")
    s.create_run("r1", "a/b", "t", "br")
    s.add_event("r1", "verify", {"passed": False})
    s.add_event("r1", "verify", {"passed": True})
    evs = s.list_events("r1")
    assert [e["type"] for e in evs] == ["verify", "verify"]
    assert evs[1]["payload"]["passed"] is True
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.store'`

- [ ] **Step 3: Write `src/forge/store.py`**

```python
import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, repo TEXT, task TEXT, branch TEXT,
  state TEXT NOT NULL DEFAULT 'queued', pr_url TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT DEFAULT (datetime('now')),
  type TEXT, payload TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_run(self, run_id, repo, task, branch) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs(run_id, repo, task, branch) VALUES (?,?,?,?)",
                (run_id, repo, task, branch),
            )

    def set_state(self, run_id, state, pr_url=None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET state=?, pr_url=COALESCE(?, pr_url), "
                "updated_at=datetime('now') WHERE run_id=?",
                (state, pr_url, run_id),
            )

    def add_event(self, run_id, etype, payload) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO events(run_id, type, payload) VALUES (?,?,?)",
                (run_id, etype, json.dumps(payload)),
            )

    def get_run(self, run_id) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

    def list_events(self, run_id) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_store.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/store.py tests/test_store.py
git commit -m "feat(phase1): SQLite run/event store"
```

---

### Task 4: Run directory + timeline.md writer

**Files:**
- Create: `forge/src/forge/rundir.py`
- Test: `forge/tests/test_rundir.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RunDir(root: Path)` where `root == runs_dir/<run_id>`; constructor creates `root` and `root/` is usable. Methods: `timeline(line: str, *, ts: str | None = None) -> None` (appends `HH:MM line` to `timeline.md`; `ts` injectable for tests); `write(name: str, content: str) -> Path`; `path(name: str) -> Path`. Static `for_run(runs_dir: Path, run_id: str) -> RunDir`.

- [ ] **Step 1: Write the failing test** (`tests/test_rundir.py`)

```python
from forge.rundir import RunDir


def test_creates_dir_and_writes(tmp_path):
    rd = RunDir.for_run(tmp_path, "r1")
    assert (tmp_path / "r1").is_dir()
    p = rd.write("report.md", "hello")
    assert p.read_text() == "hello"


def test_timeline_appends_with_injected_ts(tmp_path):
    rd = RunDir.for_run(tmp_path, "r1")
    rd.timeline("Run created", ts="20:14")
    rd.timeline("PR opened", ts="20:22")
    content = (tmp_path / "r1" / "timeline.md").read_text()
    assert content == "20:14  Run created\n20:22  PR opened\n"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_rundir.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.rundir'`

- [ ] **Step 3: Write `src/forge/rundir.py`**

```python
from datetime import datetime
from pathlib import Path


class RunDir:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, runs_dir: Path, run_id: str) -> "RunDir":
        return cls(Path(runs_dir) / run_id)

    def path(self, name: str) -> Path:
        return self.root / name

    def write(self, name: str, content: str) -> Path:
        p = self.path(name)
        p.write_text(content)
        return p

    def timeline(self, line: str, *, ts: str | None = None) -> None:
        stamp = ts if ts is not None else datetime.now().strftime("%H:%M")
        with self.path("timeline.md").open("a") as f:
            f.write(f"{stamp}  {line}\n")
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_rundir.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/rundir.py tests/test_rundir.py
git commit -m "feat(phase1): run dir + timeline writer"
```

---

### Task 5: Verification detection

**Files:**
- Create: `forge/src/forge/verify.py`
- Test: `forge/tests/test_verify.py`

**Interfaces:**
- Consumes: nothing (pure — fed file contents read by the orchestrator via `container.exec`).
- Produces: `VerifyCmd(name: str, argv: list[str])`; `VerifyPlan(commands: list[VerifyCmd], has_real_verification: bool)`; `parse_verify(package_json: str | None, repo_yml: str | None, has_verify_sh: bool) -> VerifyPlan`. Precedence: `.forge/verify.sh` → `["bash", ".forge/verify.sh"]`; else `.forge/repo.yml` `verification.command` (split on spaces) ; else for each of `test`,`lint`,`typecheck`,`build` present in `package.json` scripts → `["npm","run",<script>]` (use `npm test` for `test`). `has_real_verification` is `True` iff at least one command was produced.

- [ ] **Step 1: Write the failing test** (`tests/test_verify.py`)

```python
import json
from forge.verify import parse_verify


def test_npm_scripts_detected():
    pj = json.dumps({"scripts": {"test": "jest", "build": "tsc", "start": "x"}})
    plan = parse_verify(pj, None, False)
    names = [c.name for c in plan.commands]
    assert names == ["test", "build"]            # only known checks, in order
    assert plan.commands[0].argv == ["npm", "test"]
    assert plan.commands[1].argv == ["npm", "run", "build"]
    assert plan.has_real_verification is True


def test_verify_sh_wins():
    plan = parse_verify(json.dumps({"scripts": {"test": "jest"}}), None, True)
    assert plan.commands[0].argv == ["bash", ".forge/verify.sh"]
    assert len(plan.commands) == 1


def test_repo_yml_command():
    plan = parse_verify(None, "verification:\n  command: yarn verify\n", False)
    assert plan.commands[0].argv == ["yarn", "verify"]


def test_nothing_detected_is_not_real():
    plan = parse_verify(json.dumps({"scripts": {"start": "x"}}), None, False)
    assert plan.commands == []
    assert plan.has_real_verification is False
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_verify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.verify'`

- [ ] **Step 3: Write `src/forge/verify.py`**

```python
import json
import re
from dataclasses import dataclass

_KNOWN = ["test", "lint", "typecheck", "build"]


@dataclass(frozen=True)
class VerifyCmd:
    name: str
    argv: list


@dataclass(frozen=True)
class VerifyPlan:
    commands: list
    has_real_verification: bool


def _repo_yml_command(repo_yml: str) -> list | None:
    # Minimal extraction: a line 'command: <cmd>' under 'verification:'.
    m = re.search(r"verification:\s*\n(?:\s+.*\n)*?\s+command:\s*(.+)", repo_yml)
    if not m:
        m = re.search(r"^\s*command:\s*(.+)$", repo_yml, re.MULTILINE)
    return m.group(1).strip().split() if m else None


def parse_verify(package_json, repo_yml, has_verify_sh) -> VerifyPlan:
    if has_verify_sh:
        return VerifyPlan([VerifyCmd("verify.sh", ["bash", ".forge/verify.sh"])], True)
    if repo_yml:
        cmd = _repo_yml_command(repo_yml)
        if cmd:
            return VerifyPlan([VerifyCmd("repo.yml", cmd)], True)
    cmds = []
    if package_json:
        try:
            scripts = json.loads(package_json).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name in _KNOWN:
            if name in scripts:
                argv = ["npm", "test"] if name == "test" else ["npm", "run", name]
                cmds.append(VerifyCmd(name, argv))
    return VerifyPlan(cmds, len(cmds) > 0)
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_verify.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/verify.py tests/test_verify.py
git commit -m "feat(phase1): verification command detection"
```

---

### Task 6: Prompt building

**Files:**
- Create: `forge/src/forge/prompts.py`
- Test: `forge/tests/test_prompts.py`

**Interfaces:**
- Consumes: `VerifyCmd` (Task 5) for failure context type only (uses `.name`).
- Produces: `build_task_prompt(task: str) -> str` and `build_fix_prompt(failures: list[tuple[str, str]]) -> str` where each failure is `(check_name, output_tail)`. Both return strings establishing the worker role ("you are a worker; Forge runs verification and decides done", "ask nothing — work autonomously").

- [ ] **Step 1: Write the failing test** (`tests/test_prompts.py`)

```python
from forge.prompts import build_task_prompt, build_fix_prompt


def test_task_prompt_contains_task_and_role():
    p = build_task_prompt("Add a /health endpoint")
    assert "Add a /health endpoint" in p
    assert "verification" in p.lower()
    assert "autonomous" in p.lower() or "do not ask" in p.lower()


def test_fix_prompt_lists_failures():
    p = build_fix_prompt([("test", "1 failing: expected 5 got -1")])
    assert "test" in p
    assert "expected 5 got -1" in p
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_prompts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.prompts'`

- [ ] **Step 3: Write `src/forge/prompts.py`**

```python
_ROLE = (
    "You are an autonomous coding worker inside an isolated container. "
    "Make the change described below. Forge (not you) runs the verification "
    "suite and decides when the work is done — focus on a correct, minimal "
    "implementation. Work autonomously: do not ask questions, do not stop to "
    "request confirmation. Follow the repository's existing conventions."
)


def build_task_prompt(task: str) -> str:
    return f"{_ROLE}\n\nTASK:\n{task}\n"


def build_fix_prompt(failures) -> str:
    blocks = "\n\n".join(
        f"### {name} failed:\n{output}" for name, output in failures
    )
    return (
        "The verification suite is still failing after your last change. "
        "Fix the cause of these failures (do not modify the tests unless the "
        "task says to). Here is the latest output:\n\n" + blocks + "\n"
    )
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_prompts.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/prompts.py tests/test_prompts.py
git commit -m "feat(phase1): worker task + fix prompts"
```

---

### Task 7: Worker result parsing

**Files:**
- Create: `forge/src/forge/worker.py`
- Test: `forge/tests/test_worker.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `WorkerResult(ok: bool, is_error: bool, num_turns: int, duration_ms: int, total_cost_usd: float | None, input_tokens: int, output_tokens: int, session_id: str, result_text: str, auth_error: bool)`; `parse_worker_result(stdout: str) -> WorkerResult`. Parses the `claude -p --output-format json` object (real fields confirmed by the Phase 0 spike: `type`, `subtype`, `is_error`, `num_turns`, `duration_ms`, `total_cost_usd`, `usage{input_tokens,output_tokens,...}`, `session_id`, `result`). `ok == (not is_error and subtype == "success")`. `auth_error` is `True` if stdout is unparseable or `subtype` indicates auth/credit failure (`"error_during_execution"` plus an auth hint in `result`, or empty stdout).

- [ ] **Step 1: Write the failing test** (`tests/test_worker.py`)

```python
import json
from forge.worker import parse_worker_result

SPIKE = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "num_turns": 5, "duration_ms": 11198, "total_cost_usd": 0.0727632,
    "usage": {"input_tokens": 6, "output_tokens": 382,
              "cache_read_input_tokens": 86714, "cache_creation_input_tokens": 6733},
    "session_id": "abc", "result": "Fixed the bug.",
})


def test_parses_success():
    r = parse_worker_result(SPIKE)
    assert r.ok is True
    assert r.num_turns == 5
    assert r.duration_ms == 11198
    assert r.total_cost_usd == 0.0727632
    assert r.input_tokens == 6 and r.output_tokens == 382
    assert r.session_id == "abc"
    assert r.auth_error is False


def test_unparseable_is_auth_error():
    r = parse_worker_result("")
    assert r.ok is False
    assert r.auth_error is True


def test_error_subtype_not_ok():
    r = parse_worker_result(json.dumps({"subtype": "error_during_execution", "is_error": True}))
    assert r.ok is False
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_worker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.worker'`

- [ ] **Step 3: Write `src/forge/worker.py`**

```python
import json
from dataclasses import dataclass

_AUTH_HINTS = ("oauth", "login", "credit", "rate limit", "usage limit", "unauthorized")


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
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_worker.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/worker.py tests/test_worker.py
git commit -m "feat(phase1): parse claude -p result JSON"
```

---

### Task 8: Budget tracker

**Files:**
- Create: `forge/src/forge/budget.py`
- Test: `forge/tests/test_budget.py`

**Interfaces:**
- Consumes: `Budget` (Task 1).
- Produces: `BudgetTracker(budget: Budget, clock: Callable[[], float])`; `start() -> None` (records start time); `tick() -> None` (increments iteration count); `stop_reason() -> str | None` — returns `None` if within budget, `"iterations"` if `iterations >= max_iterations`, `"wall_clock"` if `clock() - start >= max_wall_secs`. `iterations` is a readable attribute.

- [ ] **Step 1: Write the failing test** (`tests/test_budget.py`)

```python
from forge.config import Budget
from forge.budget import BudgetTracker


def test_iteration_cap():
    bt = BudgetTracker(Budget(max_iterations=2, max_wall_secs=9999), clock=lambda: 0.0)
    bt.start()
    assert bt.stop_reason() is None
    bt.tick(); assert bt.stop_reason() is None
    bt.tick(); assert bt.stop_reason() == "iterations"


def test_wall_clock_cap():
    t = {"now": 0.0}
    bt = BudgetTracker(Budget(max_iterations=99, max_wall_secs=30), clock=lambda: t["now"])
    bt.start()
    t["now"] = 29.0
    assert bt.stop_reason() is None
    t["now"] = 30.0
    assert bt.stop_reason() == "wall_clock"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_budget.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.budget'`

- [ ] **Step 3: Write `src/forge/budget.py`**

```python
from forge.config import Budget


class BudgetTracker:
    def __init__(self, budget: Budget, clock):
        self.budget = budget
        self.clock = clock
        self.iterations = 0
        self._start = 0.0

    def start(self) -> None:
        self._start = self.clock()

    def tick(self) -> None:
        self.iterations += 1

    def stop_reason(self):
        if self.iterations >= self.budget.max_iterations:
            return "iterations"
        if self.clock() - self._start >= self.budget.max_wall_secs:
            return "wall_clock"
        return None
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_budget.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/budget.py tests/test_budget.py
git commit -m "feat(phase1): budget tracker (iterations + wall-clock)"
```

---

### Task 9: Command builders (pure argv)

**Files:**
- Create: `forge/src/forge/commands.py`
- Test: `forge/tests/test_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all return `list[str]`): `clone_cmd(repo: str) -> list[str]` (`gh repo clone <repo> .`); `branch_cmd(branch: str) -> list[str]` (`git checkout -b <branch>`); `worker_cmd(prompt: str, model: str | None) -> list[str]` (`claude -p <prompt> --output-format json --dangerously-skip-permissions [--model <model>]`); `has_changes_cmd() -> list[str]` (`git status --porcelain`); `commit_cmds(message: str) -> list[list[str]]` (config user.name/email, add -A, commit); `push_cmd(branch: str) -> list[str]`; `pr_create_cmd(title: str, body_file: str, draft: bool) -> list[str]` (`gh pr create --title ... --body-file ... [--draft] --head <set by push>`). Note: secrets (`GH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) are passed as container env, **never** as argv.

- [ ] **Step 1: Write the failing test** (`tests/test_commands.py`)

```python
from forge import commands as c


def test_clone_and_branch():
    assert c.clone_cmd("a/b") == ["gh", "repo", "clone", "a/b", "."]
    assert c.branch_cmd("forge/x") == ["git", "checkout", "-b", "forge/x"]


def test_worker_cmd_with_and_without_model():
    base = c.worker_cmd("do x", None)
    assert base[:2] == ["claude", "-p"]
    assert "do x" in base
    assert "--output-format" in base and "json" in base
    assert "--dangerously-skip-permissions" in base
    assert "--model" not in base
    assert "--model" in c.worker_cmd("do x", "claude-opus-4-8")


def test_pr_create_draft_flag():
    assert "--draft" in c.pr_create_cmd("t", "body.md", draft=True)
    assert "--draft" not in c.pr_create_cmd("t", "body.md", draft=False)


def test_commit_cmds_configures_identity():
    cmds = c.commit_cmds("msg")
    assert ["git", "add", "-A"] in cmds
    assert any(x[:3] == ["git", "config", "user.name"] for x in cmds)
    assert cmds[-1][:2] == ["git", "commit"]
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_commands.py -v`
Expected: FAIL with `ImportError: cannot import name 'commands'`

- [ ] **Step 3: Write `src/forge/commands.py`**

```python
def clone_cmd(repo: str) -> list:
    return ["gh", "repo", "clone", repo, "."]


def branch_cmd(branch: str) -> list:
    return ["git", "checkout", "-b", branch]


def worker_cmd(prompt: str, model: str | None) -> list:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    return cmd


def has_changes_cmd() -> list:
    return ["git", "status", "--porcelain"]


def commit_cmds(message: str) -> list:
    return [
        ["git", "config", "user.name", "Forge"],
        ["git", "config", "user.email", "forge@localhost"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
    ]


def push_cmd(branch: str) -> list:
    return ["git", "push", "-u", "origin", branch]


def pr_create_cmd(title: str, body_file: str, draft: bool) -> list:
    cmd = ["gh", "pr", "create", "--title", title, "--body-file", body_file]
    if draft:
        cmd.append("--draft")
    return cmd
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_commands.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/forge/commands.py tests/test_commands.py
git commit -m "feat(phase1): pure argv command builders"
```

---

### Task 10: ContainerRunner — Protocol + Docker implementation + worker image

**Files:**
- Create: `forge/src/forge/container.py`
- Create: `forge/worker-image/Dockerfile`
- Test: `forge/tests/test_container_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ExecResult(exit_code: int, stdout: str, stderr: str)`; `ContainerRunner` (Protocol) with `start(run_id: str, env: dict[str, str]) -> str` (returns container id; starts a long-lived container with workdir `/work`), `exec(cid: str, argv: list[str], workdir: str = "/work") -> ExecResult`, `stop(cid: str) -> None`; `DockerRunner(image_tag: str)` implementing it via the `docker` CLI. Secrets arrive only through `env` on `start` (so they live in the container's env, not in any argv or log).

- [ ] **Step 1: Write the worker image** (`worker-image/Dockerfile`)

```dockerfile
FROM node:22-bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl gnupg jq \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@latest
RUN useradd -m -u 1001 forge && mkdir -p /work && chown forge:forge /work
USER forge
WORKDIR /work
# Keep the container alive so the orchestrator can exec into it repeatedly.
ENTRYPOINT ["sleep", "infinity"]
```

- [ ] **Step 2: Write the smoke test** (`tests/test_container_smoke.py`)

```python
import shutil
import subprocess
import pytest
from forge.container import DockerRunner

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "forge-worker"],
                      capture_output=True).returncode != 0,
    reason="docker or forge-worker image unavailable",
)


def test_start_exec_stop():
    r = DockerRunner("forge-worker")
    cid = r.start("smoke", env={"FOO": "bar"})
    try:
        out = r.exec(cid, ["printenv", "FOO"])
        assert out.exit_code == 0
        assert out.stdout.strip() == "bar"
        assert r.exec(cid, ["bash", "-lc", "echo hi"]).stdout.strip() == "hi"
    finally:
        r.stop(cid)
```

- [ ] **Step 3: Build the image, then run the smoke test to verify it fails on missing module**

Run: `cd forge && docker build -t forge-worker worker-image && python -m pytest tests/test_container_smoke.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.container'` (image now exists)

- [ ] **Step 4: Write `src/forge/container.py`**

```python
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class ContainerRunner(Protocol):
    def start(self, run_id: str, env: dict) -> str: ...
    def exec(self, cid: str, argv: list, workdir: str = "/work") -> ExecResult: ...
    def stop(self, cid: str) -> None: ...


class DockerRunner:
    def __init__(self, image_tag: str):
        self.image_tag = image_tag

    def start(self, run_id: str, env: dict) -> str:
        cmd = ["docker", "run", "-d", "--name", f"forge-{run_id}", "-w", "/work"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]   # value in env, not echoed anywhere
        cmd.append(self.image_tag)
        out = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def exec(self, cid: str, argv: list, workdir: str = "/work") -> ExecResult:
        cmd = ["docker", "exec", "-w", workdir, cid] + argv
        out = subprocess.run(cmd, capture_output=True, text=True)
        return ExecResult(out.returncode, out.stdout, out.stderr)

    def stop(self, cid: str) -> None:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
```

- [ ] **Step 5: Run the smoke test, verify pass**

Run: `cd forge && python -m pytest tests/test_container_smoke.py -v`
Expected: PASS (1 passed) — or SKIPPED if docker is unavailable on the runner.

- [ ] **Step 6: Commit**

```bash
git add src/forge/container.py worker-image/Dockerfile tests/test_container_smoke.py
git commit -m "feat(phase1): ContainerRunner (docker) + worker image"
```

---

### Task 11: Orchestrator — outer loop owning every verdict

**Files:**
- Create: `forge/src/forge/orchestrator.py`
- Test: `forge/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `Config`/`Budget` (1), `make_runspec` (2), `Store` (3), `RunDir` (4), `parse_verify`/`VerifyPlan` (5), `build_task_prompt`/`build_fix_prompt` (6), `parse_worker_result` (7), `BudgetTracker` (8), `commands` (9), `ContainerRunner`/`ExecResult` (10).
- Produces: `RunOutcome(state: str, pr_url: str | None, draft: bool, reason: str | None)`; `Orchestrator(config: Config, store: Store, runner: ContainerRunner, clock=time.monotonic)` with `run(repo: str, task: str, run_id: str) -> RunOutcome`. Lifecycle: start container (env = OAuth + GH tokens) → clone → branch → read `package.json`/`.forge/repo.yml`/`.forge/verify.sh` via `exec` → `parse_verify` → loop[ worker → verify; pass⇒finalize; fail⇒fix-prompt + tick; budget⇒stop ] → finalize (commit/push/PR; draft iff `not has_real_verification` or stopped on budget) → stop container. States persisted via `Store.set_state`; key moments via `RunDir.timeline`.

- [ ] **Step 1: Write the failing tests** (`tests/test_orchestrator.py`)

```python
import json
from forge.config import Config, Budget
from forge.store import Store
from forge.container import ExecResult
from forge.orchestrator import Orchestrator

PKG = json.dumps({"scripts": {"test": "node --test"}})
WORKER_OK = json.dumps({"subtype": "success", "is_error": False, "num_turns": 1,
                        "duration_ms": 10, "total_cost_usd": 0.01,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "session_id": "s", "result": "done"})


class FakeRunner:
    """Scriptable ContainerRunner. `script` maps a matcher fn -> ExecResult."""
    def __init__(self, handlers):
        self.handlers = handlers      # list of (predicate(argv), ExecResult)
        self.calls = []

    def start(self, run_id, env):
        assert "ANTHROPIC_API_KEY" not in env       # subscription path only
        assert env.get("CLAUDE_CODE_OAUTH_TOKEN")
        return "cid"

    def exec(self, cid, argv, workdir="/work"):
        self.calls.append(argv)
        for pred, res in self.handlers:
            if pred(argv):
                return res
        return ExecResult(0, "", "")

    def stop(self, cid):
        pass


def _cfg(tmp_path):
    return Config(runs_dir=tmp_path, oauth_token="tok", gh_token="gh",
                  budget=Budget(max_iterations=3, max_wall_secs=9999))


def _has(argv, *needles):
    s = " ".join(argv)
    return all(n in s for n in needles)


def test_success_first_pass_opens_nondraft_pr(tmp_path):
    state = {"verify": 1}  # fail once, then pass

    def verify_res(argv):
        if state["verify"] > 0:
            state["verify"] -= 1
            return ExecResult(1, "FAIL expected 5", "")
        return ExecResult(0, "ok", "")

    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "no file")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], lambda: None),  # placeholder; overridden below
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f.js", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/7", "")),
    ]
    # dynamic verify handler
    runner = FakeRunner(handlers)
    runner.handlers.insert(4, (lambda a: a[:2] == ["npm", "test"],
                               None))
    # replace npm test handler with a callable-aware exec
    orig_exec = runner.exec

    def exec2(cid, argv, workdir="/work"):
        if argv[:2] == ["npm", "test"]:
            runner.calls.append(argv)
            return verify_res(argv)
        return orig_exec(cid, argv, workdir)
    runner.exec = exec2

    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), runner)
    out = o.run("a/b", "fix add", "run0001")
    assert out.state == "done"
    assert out.draft is False
    assert out.pr_url.endswith("/pull/7")


def test_no_verification_opens_draft(tmp_path):
    handlers = [
        (lambda a: _has(a, "cat", "package.json"),
         ExecResult(0, json.dumps({"scripts": {"start": "x"}}), "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/8", "")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "tweak readme", "run0002")
    assert out.draft is True
    assert out.reason == "no_verification"


def test_budget_exhaustion_stops_and_drafts(tmp_path):
    # verify always fails -> loops until iteration cap (3) -> draft PR
    handlers = [
        (lambda a: _has(a, "cat", "package.json"), ExecResult(0, PKG, "")),
        (lambda a: _has(a, "cat", ".forge/repo.yml"), ExecResult(1, "", "")),
        (lambda a: _has(a, "test", ".forge/verify.sh"), ExecResult(1, "", "")),
        (lambda a: _has(a, "claude", "-p"), ExecResult(0, WORKER_OK, "")),
        (lambda a: a[:2] == ["npm", "test"], ExecResult(1, "still failing", "")),
        (lambda a: _has(a, "git", "status", "--porcelain"), ExecResult(0, " M f", "")),
        (lambda a: _has(a, "gh", "pr", "create"),
         ExecResult(0, "https://github.com/a/b/pull/9", "")),
    ]
    o = Orchestrator(_cfg(tmp_path), Store(tmp_path / "db"), FakeRunner(handlers))
    out = o.run("a/b", "hard task", "run0003")
    assert out.state == "stopped_budget"
    assert out.reason == "iterations"
    assert out.draft is True
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd forge && python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.orchestrator'`

- [ ] **Step 3: Write `src/forge/orchestrator.py`**

```python
import time
from dataclasses import dataclass

from forge import commands as cmd
from forge.budget import BudgetTracker
from forge.config import Config
from forge.container import ContainerRunner
from forge.prompts import build_fix_prompt, build_task_prompt
from forge.rundir import RunDir
from forge.runspec import make_runspec
from forge.store import Store
from forge.verify import parse_verify
from forge.worker import parse_worker_result


@dataclass(frozen=True)
class RunOutcome:
    state: str
    pr_url: str | None
    draft: bool
    reason: str | None


class Orchestrator:
    def __init__(self, config: Config, store: Store, runner: ContainerRunner,
                 clock=time.monotonic):
        self.cfg = config
        self.store = store
        self.runner = runner
        self.clock = clock

    def _cat(self, cid, path):
        r = self.runner.exec(cid, ["cat", path])
        return r.stdout if r.exit_code == 0 else None

    def _run_verify(self, cid, plan, rd):
        failures = []
        for c in plan.commands:
            res = self.runner.exec(cid, c.argv)
            if res.exit_code != 0:
                tail = (res.stdout + res.stderr)[-2000:]
                failures.append((c.name, tail))
        return failures

    def run(self, repo: str, task: str, run_id: str) -> RunOutcome:
        rs = make_runspec(repo, task, run_id)
        self.store.create_run(run_id, repo, task, rs.branch)
        rd = RunDir.for_run(self.cfg.runs_dir, run_id)
        rd.timeline(f"Run created — {repo} · {task}")

        env = {"CLAUDE_CODE_OAUTH_TOKEN": self.cfg.oauth_token,
               "GH_TOKEN": self.cfg.gh_token}
        cid = self.runner.start(run_id, env)
        try:
            self.store.set_state(run_id, "provisioning")
            self.runner.exec(cid, cmd.clone_cmd(repo))
            self.runner.exec(cid, cmd.branch_cmd(rs.branch))
            rd.timeline(f"Cloned · branch {rs.branch}")

            plan = parse_verify(
                self._cat(cid, "package.json"),
                self._cat(cid, ".forge/repo.yml"),
                self.runner.exec(cid, ["test", "-f", ".forge/verify.sh"]).exit_code == 0,
            )
            self.store.add_event(run_id, "verify_plan",
                                 {"real": plan.has_real_verification,
                                  "cmds": [c.name for c in plan.commands]})

            bt = BudgetTracker(self.cfg.budget, self.clock)
            bt.start()
            self.store.set_state(run_id, "running")
            prompt = build_task_prompt(task)
            stop_reason = None

            while True:
                res = self.runner.exec(cid, cmd.worker_cmd(prompt, None))
                wr = parse_worker_result(res.stdout)
                rd.timeline(f"Worker turn (cost≈${wr.total_cost_usd}) — {wr.session_id}")
                if wr.auth_error:
                    stop_reason = "usage"
                    break

                self.store.set_state(run_id, "verifying")
                if not plan.has_real_verification:
                    break  # nothing to gate on -> draft PR below
                failures = self._run_verify(cid, plan, rd)
                if not failures:
                    rd.timeline("Verify: PASSED")
                    break
                rd.timeline(f"Verify: FAILED ({', '.join(n for n, _ in failures)})")

                bt.tick()
                stop_reason = bt.stop_reason()
                if stop_reason:
                    break
                prompt = build_fix_prompt(failures)

            return self._finalize(cid, run_id, rs, rd, plan, stop_reason)
        finally:
            self.runner.stop(cid)

    def _finalize(self, cid, run_id, rs, rd, plan, stop_reason) -> RunOutcome:
        self.store.set_state(run_id, "finalizing")
        if self.runner.exec(cid, cmd.has_changes_cmd()).stdout.strip() == "":
            self.store.set_state(run_id, "failed")
            rd.timeline("No changes produced — nothing to PR")
            return RunOutcome("failed", None, False, "no_changes")

        for cc in cmd.commit_cmds(f"forge: {rs.task}"):
            self.runner.exec(cid, cc)
        self.runner.exec(cid, cmd.push_cmd(rs.branch))

        draft = (not plan.has_real_verification) or (stop_reason is not None)
        reason = "no_verification" if not plan.has_real_verification else stop_reason
        rd.write("report.md", f"# {rs.task}\n\nrun: {run_id}\nbranch: {rs.branch}\n")
        pr = self.runner.exec(
            cid, cmd.pr_create_cmd(f"forge: {rs.task}", "/work/report.md", draft))
        pr_url = pr.stdout.strip().splitlines()[-1] if pr.exit_code == 0 else None

        if stop_reason in ("iterations", "wall_clock", "usage"):
            state = "stopped_budget"
        else:
            state = "done"
        self.store.set_state(run_id, state, pr_url=pr_url)
        rd.timeline(f"PR opened{' (draft)' if draft else ''} → {pr_url}")
        return RunOutcome(state, pr_url, draft, reason)
```

Note for the implementer: the `report.md` is written to the host run dir *and* must exist in the container at `/work/report.md` for `gh pr create --body-file`. Add `self.runner.exec(cid, ["bash","-lc", f"cat > /work/report.md <<'EOF'\\n{body}\\nEOF"])` (or `docker cp`) — keep it simple by writing it in-container. Adjust `_finalize` to create the in-container file before `pr_create_cmd`. The tests stub `gh pr create` so they pass regardless; this note is for the real path.

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the whole suite**

Run: `cd forge && python -m pytest -v`
Expected: all PASS (container smoke may SKIP without docker)

- [ ] **Step 6: Commit**

```bash
git add src/forge/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(phase1): orchestrator outer loop (verdicts, budget, PR)"
```

---

### Task 12: CLI entrypoint + end-to-end capstone

**Files:**
- Create: `forge/src/forge/cli.py`
- Test: `forge/tests/test_cli.py`

**Interfaces:**
- Consumes: `Config.from_env` (1), `Orchestrator` (11), `DockerRunner` (10), `Store` (3).
- Produces: `main(argv: list[str] | None = None) -> int`. Usage: `forge run <owner/repo> "<task>" [--runs-dir DIR]`. Generates `run_id` (uuid4 hex), builds `Config.from_env`, fails fast if `oauth_token`/`gh_token` missing, constructs `DockerRunner(cfg.image_tag)` + `Store(cfg.runs_dir/"forge.db")` + `Orchestrator`, prints the resulting PR URL (or the stop reason) and returns 0 on `done`/`stopped_budget`, 1 otherwise.

- [ ] **Step 1: Write the failing test** (`tests/test_cli.py`) — exercises arg parsing + the missing-token guard without Docker:

```python
import pytest
from forge.cli import main


def test_missing_tokens_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    rc = main(["run", "a/b", "do x", "--runs-dir", str(tmp_path)])
    assert rc == 1
    assert "token" in capsys.readouterr().err.lower()


def test_bad_repo_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("GH_TOKEN", "g")
    rc = main(["run", "notarepo", "do x", "--runs-dir", str(tmp_path)])
    assert rc == 1
```

- [ ] **Step 2: Run test, verify it fails**

Run: `cd forge && python -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'forge.cli'`

- [ ] **Step 3: Write `src/forge/cli.py`**

```python
import argparse
import sys
import uuid
from pathlib import Path

from forge.config import Config
from forge.container import DockerRunner
from forge.orchestrator import Orchestrator
from forge.runspec import make_runspec
from forge.store import Store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="forge")
    sub = p.add_subparsers(dest="cmd", required=True)
    runp = sub.add_parser("run")
    runp.add_argument("repo")
    runp.add_argument("task")
    runp.add_argument("--runs-dir", default="runs")
    args = p.parse_args(argv)

    cfg = Config.from_env(Path(args.runs_dir))
    if not cfg.oauth_token or not cfg.gh_token:
        print("error: CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN must be set "
              "(run `claude setup-token` and `gh auth token`)", file=sys.stderr)
        return 1
    try:
        make_runspec(args.repo, args.task, "x" * 8)   # validate early
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    run_id = uuid.uuid4().hex
    store = Store(cfg.runs_dir / "forge.db")
    orch = Orchestrator(cfg, store, DockerRunner(cfg.image_tag))
    out = orch.run(args.repo, args.task, run_id)
    if out.pr_url:
        print(f"{out.state}: {'draft ' if out.draft else ''}PR {out.pr_url}")
    else:
        print(f"{out.state}: no PR ({out.reason})")
    return 0 if out.state in ("done", "stopped_budget") else 1
```

- [ ] **Step 4: Run tests, verify pass**

Run: `cd forge && python -m pytest tests/test_cli.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: End-to-end capstone (manual — the "agent is useful" proof)**

This is a real run; it needs Docker, the worker image, a subscription token, and a GitHub repo you can push to (e.g. a throwaway repo with a failing test). Not a unit test.

```bash
cd forge
docker build -t forge-worker worker-image
export CLAUDE_CODE_OAUTH_TOKEN="$(cat spike/.oauth-token)"   # or re-run: claude setup-token
export GH_TOKEN="$(gh auth token)"
python -m forge.cli run <owner>/<throwaway-repo> "Fix the failing test in <file>" --runs-dir runs
```

Expected: prints `done: PR https://github.com/<owner>/<repo>/pull/N`; the PR contains the fix; `runs/<id>/timeline.md` shows Run created → Cloned → Worker turn → Verify PASSED → PR opened.

- [ ] **Step 6: Commit**

```bash
git add src/forge/cli.py tests/test_cli.py
git commit -m "feat(phase1): CLI entrypoint + e2e capstone"
```

---

## Self-Review

**Spec coverage (§14 Phase 1 row: "Run container, `gh clone`+branch, Claude worker emitting status, orchestrator-owned verify gate, commit/push/`gh pr create`, env none/node-web"):**
- Run container → Task 10. `gh clone` + branch → Tasks 9 + 11. Worker (`claude -p`) emitting status → Tasks 6/7/9 + loop in 11. Orchestrator-owned verify gate → Tasks 5 + 11 (`_run_verify`, draft-on-no-verification). commit/push/`gh pr create` → Tasks 9 + `_finalize`. Budget caps (§5/§11) → Task 8 + loop. timeline.md/run dir/SQLite (§10/§12) → Tasks 3/4. CLI (Phase-1 substitute for Slack) → Task 12. Env none/node-web = the base image only, no compose (correct for Phase 1). **No gaps.**

**Deferred to later phases (intentionally out of scope here):** Slack interface + MCP bridge + `ask_user` (Phase 2); resume/checkpoints (Phase 2); service-stack environments / Supabase / DHIS2 (Phase 3); no-progress detector (named in §5 — add in Phase 2 alongside `ask_user`, since the cheap version is "same failing-set + unchanged diff", which needs the diff hash plumbing Phase 2 introduces).

**Placeholder scan:** Task 11 contains one explicit implementer note (write `report.md` *inside* the container before `gh pr create --body-file`) rather than silent hand-waving — it's flagged with the concrete mechanism (`bash -lc 'cat > … <<EOF'` or `docker cp`) and the tests pass regardless because `gh pr create` is stubbed. Acceptable; not a hidden TODO.

**Type consistency:** `ExecResult(exit_code, stdout, stderr)` used identically in Tasks 10/11/tests. `VerifyPlan.has_real_verification` / `VerifyCmd.argv` consistent across 5/11. `WorkerResult.auth_error`/`.total_cost_usd`/`.session_id` consistent across 7/11. `RunOutcome(state, pr_url, draft, reason)` consistent across 11/12. `commands.*` argv shapes match the orchestrator's calls and the test matchers.
