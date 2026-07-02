# Forge Environment Layer — Clickable URL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `forge run` stand the target app up inside its per-run container, publish its web port, health-gate it, keep it **warm** after the PR, and print a **clickable localhost URL** with the fix running — plus commit as the user and give the worker cross-iteration memory.

**Architecture:** Extend today's single-container spine (not a rewrite). The run container keeps running after the worker finishes; the orchestrator starts the repo's web/dev server inside it, publishes the container's web port to an auto-assigned host port, polls health, and records a live **env** in SQLite. A fresh `forge run` reaps the previous env (concurrency 1); `forge down`/`forge status` manage envs out of band. This is the spec's `none`/single-service slice; the Caddy proxy (`*.forge.localhost`), multi-service Compose, and the Next+Supabase / DHIS2+CHAP templates land in the follow-on plans listed below.

**Tech Stack:** Python 3.11+, stdlib only (subprocess, sqlite3, dataclasses, argparse), pytest. Docker CLI. `claude -p` headless worker (subscription auth). No new third-party deps.

## Plan sequence (this is plan 1 of 4)

1. **THIS PLAN — Clickable URL (single-service, raw `localhost:<port>`).** Run the app, publish the port, health-gate, warm/reap, URL, commit-as-you, `--resume`.
2. **Proxy + daemon.** Caddy container + `forged` for stable `run-<id>.forge.localhost` and idle-TTL auto-reap.
3. **Multi-service Compose + Next+Supabase template.** `docker compose -p forge-<id>`, recipe wrapping, seed via `supabase db reset`, wire env, auto-login. Proving ground.
4. **DHIS2+CHAP template.** Three-service assembly, baked DHIS2 seed, DHIS2 Route bootstrap, fix-core-or-frontend. `forge bake`.

## Global Constraints

- **Subscription-only auth.** Worker is `claude -p ... --output-format json --dangerously-skip-permissions`, authed by `CLAUDE_CODE_OAUTH_TOKEN`. Never set `ANTHROPIC_API_KEY`; never call a metered API anywhere.
- **Orchestrator owns all verdicts** (success / PR / budget / verification). The app running is a precondition + evidence, never a substitute for the verify gate.
- **Secrets never logged.** Token values live only in the container's env; never put them in argv that gets logged (existing rule in `container.py`).
- **Concurrency = 1.** At most one live env; a new run reaps the prior one.
- **Local only.** No `git push` / PR network actions during development; work on branches. (Production behavior unchanged; this is a dev-time constraint for this build.)
- **Follow existing patterns:** pure argv builders (`commands.py`), `Protocol` + fake in tests (`container.py` / `test_orchestrator.py`), dataclasses, `runs/<id>/timeline.md`.
- **Python ≥ 3.11**, `pytest>=8`, package layout `src/forge/`, tests in `tests/`.

---

## File Structure

| File | New/Mod | Responsibility |
|---|---|---|
| `src/forge/config.py` | Mod | + `git_author_name`, `git_author_email`, `web_port` default, `health_timeout_secs`, `health_path` |
| `src/forge/commands.py` | Mod | commit author = user; `worker_cmd(prompt, session_id)` adds `--resume`; new argv builders: `publish run`, `port lookup`, `start-app`, `health-poll`, `kill-app` |
| `src/forge/container.py` | Mod | `DockerRunner.start` publishes a web port (`-p 127.0.0.1::<port>`); add `port(cid, container_port)`; add `exec_detached` |
| `src/forge/appserver.py` | New | Pure: detect the web start command + in-container port from repo probe (`repo.yml` → package.json scripts → default) |
| `src/forge/health.py` | New | Pure: build the health-poll argv; classify a curl exit/loop result as ready/not |
| `src/forge/envreg.py` | New | Pure helpers for env lifecycle: URL formatting, reap selection (TTL / supersede) |
| `src/forge/store.py` | Mod | + `envs` table + CRUD (`create_env`, `set_env_state`, `touch_env`, `get_env`, `list_envs`, `mark_reaped`) |
| `src/forge/orchestrator.py` | Mod | start app → health → register env → keep warm (no unconditional stop); reap prior env on start; pass session id for `--resume`; commit-as-you; surface URL in outcome |
| `src/forge/lifecycle.py` | New | `reap_env(runner, store, run_id)`, `reap_superseded(runner, store, keep_run_id)` — imperative, fake-tested |
| `src/forge/cli.py` | Mod | `run` prints URL; new subcommands `status`, `down <run_id>` |
| `tests/test_appserver.py` … | New | one test module per new pure module |

---

## Task 1: Env registry in the Store

**Files:**
- Modify: `src/forge/store.py`
- Test: `tests/test_store_envs.py`

**Interfaces:**
- Produces:
  - `Store.create_env(run_id: str, project: str, web_url: str | None, web_port: int | None, state: str) -> None`
  - `Store.set_env_state(run_id: str, state: str, web_url: str | None = None) -> None`
  - `Store.touch_env(run_id: str) -> None`
  - `Store.get_env(run_id: str) -> dict` (`{}` if absent)
  - `Store.list_envs(states: tuple[str, ...] | None = None) -> list[dict]`
  - `Store.mark_reaped(run_id: str) -> None`
  - env states: `"starting" | "live" | "reaped" | "failed"`

- [ ] **Step 1 — failing test**

```python
# tests/test_store_envs.py
from forge.store import Store

def test_create_and_get_env(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "forge-r1", "http://localhost:5051", 5051, "live")
    e = s.get_env("r1")
    assert e["state"] == "live" and e["web_url"] == "http://localhost:5051" and e["web_port"] == 5051

def test_list_envs_filters_by_state(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "p1", None, None, "live")
    s.create_env("r2", "p2", None, None, "reaped")
    assert [e["run_id"] for e in s.list_envs(states=("live",))] == ["r1"]

def test_mark_reaped(tmp_path):
    s = Store(tmp_path / "f.db")
    s.create_env("r1", "p1", "u", 1, "live")
    s.mark_reaped("r1")
    assert s.get_env("r1")["state"] == "reaped"
```

- [ ] **Step 2 — run, verify fail**: `python -m pytest tests/test_store_envs.py -v` → FAIL (no `create_env`).

- [ ] **Step 3 — implement** (add to `store.py`):

```python
# add to _SCHEMA
CREATE TABLE IF NOT EXISTS envs (
  run_id TEXT PRIMARY KEY, project TEXT, web_url TEXT, web_port INTEGER,
  state TEXT NOT NULL DEFAULT 'starting',
  created_at TEXT DEFAULT (datetime('now')),
  last_seen_at TEXT DEFAULT (datetime('now')),
  reaped_at TEXT
);
```

```python
    def create_env(self, run_id, project, web_url, web_port, state) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO envs(run_id, project, web_url, web_port, state) "
                "VALUES (?,?,?,?,?)", (run_id, project, web_url, web_port, state))

    def set_env_state(self, run_id, state, web_url=None) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET state=?, web_url=COALESCE(?, web_url), "
                      "last_seen_at=datetime('now') WHERE run_id=?", (state, web_url, run_id))

    def touch_env(self, run_id) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET last_seen_at=datetime('now') WHERE run_id=?", (run_id,))

    def get_env(self, run_id) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT * FROM envs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

    def list_envs(self, states=None) -> list:
        q = "SELECT * FROM envs"
        args = ()
        if states:
            q += " WHERE state IN (%s)" % ",".join("?" * len(states))
            args = tuple(states)
        q += " ORDER BY created_at"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def mark_reaped(self, run_id) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET state='reaped', reaped_at=datetime('now') "
                      "WHERE run_id=?", (run_id,))
```

- [ ] **Step 4 — run, verify pass**: `python -m pytest tests/test_store_envs.py -v` → PASS.
- [ ] **Step 5 — commit**: `git add -A && git commit -m "feat(env): SQLite env registry"`

---

## Task 2: App-server detection (pure)

**Files:**
- Create: `src/forge/appserver.py`
- Test: `tests/test_appserver.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class AppSpec: start_argv: list[str]; port: int; health_path: str; ok: bool`
  - `detect_appserver(repo_yml: str | None, package_json: str | None, default_port: int = 3000) -> AppSpec`
  - Precedence: `.forge/repo.yml` `playwright.start` (+ optional `port`) → `package.json` `dev` else `start` script (forced to `default_port` via `PORT` env, expressed as argv `["sh","-lc", f"PORT={port} npm run <script>"]`) → `ok=False` if none.

- [ ] **Step 1 — failing test**

```python
# tests/test_appserver.py
from forge.appserver import detect_appserver

def test_repo_yml_start_wins():
    spec = detect_appserver("playwright:\n  start: yarn dev\n  port: 4000\n", None)
    assert spec.ok and spec.port == 4000 and spec.start_argv == ["sh", "-lc", "PORT=4000 yarn dev"]

def test_package_json_dev_script():
    spec = detect_appserver(None, '{"scripts":{"dev":"next dev"}}')
    assert spec.ok and spec.port == 3000 and spec.start_argv == ["sh", "-lc", "PORT=3000 npm run dev"]

def test_package_json_start_fallback():
    spec = detect_appserver(None, '{"scripts":{"start":"node server.js"}}')
    assert spec.start_argv == ["sh", "-lc", "PORT=3000 npm run start"]

def test_no_server_detected():
    spec = detect_appserver(None, '{"scripts":{"test":"jest"}}')
    assert not spec.ok
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# src/forge/appserver.py
import json
import re
from dataclasses import dataclass

@dataclass(frozen=True)
class AppSpec:
    start_argv: list
    port: int
    health_path: str
    ok: bool

def _repo_yml_start(repo_yml):
    start = re.search(r"playwright:\s*\n(?:\s+.*\n)*?\s+start:\s*(.+)", repo_yml)
    if not start:
        return None
    port = re.search(r"playwright:\s*\n(?:\s+.*\n)*?\s+port:\s*(\d+)", repo_yml)
    return start.group(1).strip(), int(port.group(1)) if port else None

def detect_appserver(repo_yml, package_json, default_port=3000) -> AppSpec:
    if repo_yml:
        hit = _repo_yml_start(repo_yml)
        if hit:
            cmd, port = hit
            port = port or default_port
            return AppSpec(["sh", "-lc", f"PORT={port} {cmd}"], port, "/", True)
    if package_json:
        try:
            scripts = json.loads(package_json).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for name in ("dev", "start"):
            if name in scripts:
                return AppSpec(["sh", "-lc", f"PORT={default_port} npm run {name}"],
                               default_port, "/", True)
    return AppSpec([], default_port, "/", False)
```

- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(env): detect web dev-server command + port"`

---

## Task 3: Health-poll command builder (pure)

**Files:**
- Create: `src/forge/health.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Produces: `health_poll_argv(port: int, path: str, timeout_secs: int) -> list[str]` — a single bash argv that loops `curl -fس` against `http://localhost:<port><path>` until 200 or timeout, exiting 0/!=0. (Run inside the container.)

- [ ] **Step 1 — failing test**

```python
# tests/test_health.py
from forge.health import health_poll_argv

def test_health_poll_argv_shape():
    argv = health_poll_argv(3000, "/", 90)
    assert argv[0] == "bash" and argv[1] == "-lc"
    body = argv[2]
    assert "http://localhost:3000/" in body and "90" in body and "curl" in body
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# src/forge/health.py
def health_poll_argv(port, path, timeout_secs) -> list:
    url = f"http://localhost:{port}{path}"
    script = (
        f'for i in $(seq 1 {timeout_secs}); do '
        f'if curl -fs -o /dev/null "{url}"; then exit 0; fi; sleep 1; done; '
        f'echo "health timeout: {url}" >&2; exit 1'
    )
    return ["bash", "-lc", script]
```

- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(env): health-poll argv builder"`

---

## Task 4: Reap selection + URL formatting (pure)

**Files:**
- Create: `src/forge/envreg.py`
- Test: `tests/test_envreg.py`

**Interfaces:**
- Produces:
  - `web_url(host_port: int) -> str` → `f"http://localhost:{host_port}"`
  - `superseded_run_ids(live_envs: list[dict], keep_run_id: str) -> list[str]` — live envs whose `run_id != keep_run_id` (concurrency 1: reap all others).

- [ ] **Step 1 — failing test**

```python
# tests/test_envreg.py
from forge.envreg import web_url, superseded_run_ids

def test_web_url():
    assert web_url(5051) == "http://localhost:5051"

def test_superseded_excludes_keeper():
    envs = [{"run_id": "a"}, {"run_id": "b"}, {"run_id": "keep"}]
    assert superseded_run_ids(envs, "keep") == ["a", "b"]
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# src/forge/envreg.py
def web_url(host_port) -> str:
    return f"http://localhost:{host_port}"

def superseded_run_ids(live_envs, keep_run_id) -> list:
    return [e["run_id"] for e in live_envs if e["run_id"] != keep_run_id]
```

- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(env): url + supersede helpers"`

---

## Task 5: Commit-as-you + worker `--resume` (pure argv)

**Files:**
- Modify: `src/forge/commands.py`, `src/forge/config.py`
- Test: `tests/test_commands.py` (extend)

**Interfaces:**
- Changed: `commit_cmds(message, name, email) -> list[list[str]]` (was hardcoded `Forge`/`forge@localhost`).
- Changed: `worker_cmd(prompt, model=None, session_id=None) -> list[str]` — append `--resume <session_id>` when `session_id` is truthy.
- `Config` gains `git_author_name: str = ""`, `git_author_email: str = ""`, populated from env `FORGE_GIT_NAME` / `FORGE_GIT_EMAIL`, falling back to `gh`/git later (caller decides). Defaults must NOT be `Forge`.

- [ ] **Step 1 — failing tests** (extend `tests/test_commands.py`):

```python
from forge import commands as cmd

def test_commit_cmds_use_supplied_identity():
    cmds = cmd.commit_cmds("msg", "Dev", "dev@example.com")
    assert ["git", "config", "user.name", "Dev"] in cmds
    assert ["git", "config", "user.email", "dev@example.com"] in cmds

def test_worker_cmd_resumes_when_session_given():
    assert "--resume" in cmd.worker_cmd("p", None, "sess-1")
    assert "sess-1" in cmd.worker_cmd("p", None, "sess-1")

def test_worker_cmd_no_resume_first_turn():
    assert "--resume" not in cmd.worker_cmd("p", None, None)
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# commands.py
def worker_cmd(prompt, model=None, session_id=None) -> list:
    cmd = ["claude", "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd

def commit_cmds(message, name, email) -> list:
    return [
        ["git", "config", "user.name", name],
        ["git", "config", "user.email", email],
        ["git", "add", "-A"],
        ["git", "commit", "-m", message],
    ]
```

```python
# config.py — add fields + env wiring
    git_author_name: str = ""
    git_author_email: str = ""
# in from_env:
        git_author_name=os.environ.get("FORGE_GIT_NAME", ""),
        git_author_email=os.environ.get("FORGE_GIT_EMAIL", ""),
```

- [ ] **Step 4 — update callers in `orchestrator.py`** (Task 8 covers wiring; here just keep signature consumers compiling — the orchestrator change ships in Task 8). Run `python -m pytest tests/test_commands.py -v` → PASS.
- [ ] **Step 5 — commit**: `git commit -am "feat(env): commit-as-user + worker --resume argv"`

---

## Task 6: DockerRunner publishes a web port

**Files:**
- Modify: `src/forge/container.py`, `src/forge/commands.py`
- Test: `tests/test_container_publish.py` (pure argv), existing `tests/test_container_smoke.py` still gated on Docker.

**Interfaces:**
- Changed: `DockerRunner.start(run_id, env, publish_port: int | None = None) -> str` — when set, add `-p 127.0.0.1::<publish_port>`.
- Produces: `DockerRunner.port(cid: str, container_port: int) -> int | None` — parse `docker port` → host port.
- Produces: `DockerRunner.exec_detached(cid, argv, workdir="/work") -> None` — `docker exec -d` (start dev server in background).
- New pure builder `port_lookup_cmd(cid, container_port) -> list[str]` and `parse_host_port(docker_port_stdout: str) -> int | None`.

- [ ] **Step 1 — failing test**

```python
# tests/test_container_publish.py
from forge.commands import parse_host_port

def test_parse_host_port_ipv4():
    assert parse_host_port("127.0.0.1:5051\n") == 5051

def test_parse_host_port_multiline_picks_first():
    assert parse_host_port("127.0.0.1:5051\n[::1]:5051\n") == 5051

def test_parse_host_port_empty():
    assert parse_host_port("") is None
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# commands.py
import re as _re
def parse_host_port(stdout) -> int | None:
    m = _re.search(r":(\d+)\s*$", (stdout or "").strip().splitlines()[0]) \
        if (stdout or "").strip() else None
    return int(m.group(1)) if m else None
```

```python
# container.py — Protocol + DockerRunner
class ContainerRunner(Protocol):
    def start(self, run_id: str, env: dict, publish_port: int | None = None) -> str: ...
    def exec(self, cid: str, argv: list, workdir: str = "/work") -> ExecResult: ...
    def exec_detached(self, cid: str, argv: list, workdir: str = "/work") -> None: ...
    def port(self, cid: str, container_port: int) -> int | None: ...
    def stop(self, cid: str) -> None: ...

# DockerRunner.start: build cmd then, before image:
        if publish_port is not None:
            cmd += ["-p", f"127.0.0.1::{publish_port}"]

# DockerRunner additions:
    def exec_detached(self, cid, argv, workdir="/work") -> None:
        subprocess.run(["docker", "exec", "-d", "-w", workdir, cid] + argv,
                       capture_output=True)

    def port(self, cid, container_port) -> int | None:
        from forge.commands import parse_host_port
        out = subprocess.run(["docker", "port", cid, str(container_port)],
                             capture_output=True, text=True)
        return parse_host_port(out.stdout) if out.returncode == 0 else None
```

- [ ] **Step 4 — run**: `python -m pytest tests/test_container_publish.py -v` → PASS. (Smoke test unchanged.)
- [ ] **Step 5 — commit**: `git commit -am "feat(env): publish web port + background exec + port lookup"`

---

## Task 7: Lifecycle — reap (imperative, fake-tested)

**Files:**
- Create: `src/forge/lifecycle.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `ContainerRunner`, `Store`.
- Produces:
  - `reap_env(runner, store, run_id) -> None` — `runner.stop(f"forge-{run_id}")`, `store.mark_reaped(run_id)`.
  - `reap_superseded(runner, store, keep_run_id) -> list[str]` — reap every other `live` env; return reaped ids.

- [ ] **Step 1 — failing test** (use a fake runner that records `stop` calls; pattern mirrors `tests/test_orchestrator.py`):

```python
# tests/test_lifecycle.py
from forge.lifecycle import reap_env, reap_superseded
from forge.store import Store

class FakeRunner:
    def __init__(self): self.stopped = []
    def stop(self, cid): self.stopped.append(cid)

def test_reap_env(tmp_path):
    s = Store(tmp_path / "f.db"); s.create_env("r1", "forge-r1", "u", 1, "live")
    r = FakeRunner(); reap_env(r, s, "r1")
    assert r.stopped == ["forge-r1"] and s.get_env("r1")["state"] == "reaped"

def test_reap_superseded_keeps_one(tmp_path):
    s = Store(tmp_path / "f.db")
    for rid in ("a", "b", "keep"): s.create_env(rid, f"forge-{rid}", "u", 1, "live")
    r = FakeRunner()
    reaped = reap_superseded(r, s, "keep")
    assert set(reaped) == {"a", "b"} and s.get_env("keep")["state"] == "live"
    assert set(r.stopped) == {"forge-a", "forge-b"}
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement**

```python
# src/forge/lifecycle.py
from forge.envreg import superseded_run_ids

def reap_env(runner, store, run_id) -> None:
    runner.stop(f"forge-{run_id}")
    store.mark_reaped(run_id)

def reap_superseded(runner, store, keep_run_id) -> list:
    ids = superseded_run_ids(store.list_envs(states=("live", "starting")), keep_run_id)
    for rid in ids:
        reap_env(runner, store, rid)
    return ids
```

- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(env): env reaping (supersede + explicit)"`

---

## Task 8: Orchestrator — start app, health-gate, keep warm, register URL

**Files:**
- Modify: `src/forge/orchestrator.py`
- Test: `tests/test_orchestrator.py` (extend the existing fake-runner harness)

**Interfaces:**
- Consumes: Tasks 1–7. The existing fake runner in `test_orchestrator.py` gains `exec_detached`, `port`, and accepts `publish_port` in `start`.
- Changed: `RunOutcome` gains `web_url: str | None`.
- Behavior:
  1. On start: `reap_superseded(runner, store, run_id)` (concurrency 1).
  2. `runner.start(run_id, env, publish_port=app.port)` where `app = detect_appserver(repo.yml, package.json)`.
  3. After clone/branch, capture the worker `session_id` from the first `parse_worker_result`; pass it to subsequent `worker_cmd(..., session_id=sid)` for `--resume`.
  4. After the verify loop succeeds (or before finalize), if `app.ok`: `exec_detached(cid, app.start_argv)`, then `exec(cid, health_poll_argv(app.port, app.health_path, timeout))`. On health pass: `hp = runner.port(cid, app.port)`, `url = web_url(hp)`, `store.create_env(run_id, f"forge-{run_id}", url, hp, "live")`, timeline `App live → {url}`. On health fail: `store.create_env(..., state="failed")`, timeline warns, URL is None (run still proceeds to PR).
  5. Commit uses `cfg.git_author_name/email` (fallback to a non-empty default derived from env; never `Forge`).
  6. **Do NOT stop the container in `finally`.** The container stays up so the URL is live. Reaping happens on next run / `forge down`.

- [ ] **Step 1 — failing test** (extend harness; assert URL surfaced + container not stopped):

```python
# tests/test_orchestrator.py (additions)
def test_app_started_and_url_registered(orch_env):
    # fake runner: package.json has dev script; port() returns 5051; health exec exits 0
    out = run_orch(orch_env, package_json='{"scripts":{"dev":"next dev","test":"jest"}}')
    assert out.web_url == "http://localhost:5051"
    assert orch_env.store.get_env(out_run_id)["state"] == "live"
    assert f"forge-{out_run_id}" not in orch_env.runner.stopped  # kept warm

def test_health_fail_still_opens_pr_but_no_url(orch_env):
    out = run_orch(orch_env, health_exit=1)
    assert out.web_url is None
    assert orch_env.store.get_env(out_run_id)["state"] == "failed"
```

(Adapt to the existing test harness's actual fixtures/builders — extend the current `FakeRunner` with `exec_detached`/`port`, and thread `publish_port` through its `start`.)

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement** the orchestrator changes per the Behavior list. Key diffs:
  - Add `web_url: str | None` to `RunOutcome`.
  - Replace `commit_cmds(f"forge: {rs.task}")` with `commit_cmds(f"forge: {rs.task}", self.cfg.git_author_name or "Forge User", self.cfg.git_author_email or "forge@local")` — **the orchestrator passes real identity from config; CLI populates config from `gh`/git (Task 10).**
  - Capture `sid = wr.session_id` after the first worker turn; subsequent loop iterations call `cmd.worker_cmd(build_fix_prompt(failures), None, sid)`.
  - Insert the start-app/health/register block before `_finalize` returns, gated on `app.ok`.
  - Remove `self.runner.stop(cid)` from the `finally`; instead the container persists. (Add a `try/except` around the body that, on hard error, reaps via `lifecycle.reap_env` so failures don't leak containers.)

- [ ] **Step 4 — run**: `python -m pytest tests/test_orchestrator.py -v` → PASS.
- [ ] **Step 5 — commit**: `git commit -am "feat(env): orchestrator starts app, health-gates, keeps env warm, surfaces URL"`

---

## Task 9: CLI — print URL, `status`, `down`

**Files:**
- Modify: `src/forge/cli.py`
- Test: `tests/test_cli.py` (extend)

**Interfaces:**
- `forge run …` prints `… PR <url>` AND `app: <web_url>` when present (and attempts `open <web_url>` on darwin, behind `--open/--no-open`, default open).
- `forge status` lists envs (`run_id  state  web_url`).
- `forge down <run_id>` → `lifecycle.reap_env(DockerRunner(...), store, run_id)`, prints `reaped <run_id>`.

- [ ] **Step 1 — failing test** (CLI arg parsing + status/down dispatch with a fake store/runner injected, mirroring existing `test_cli.py` style; assert exit codes + printed lines).
- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement** the three subcommands; `run` prints the URL line and conditionally calls `subprocess.run(["open", url])` guarded by `sys.platform == "darwin"` and the `--open` flag.
- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(cli): print app URL, status, down"`

---

## Task 10: Wire user git identity from `gh`/git into Config

**Files:**
- Modify: `src/forge/cli.py`, `src/forge/config.py`
- Test: `tests/test_config_identity.py`

**Interfaces:**
- `Config.from_env` already reads `FORGE_GIT_NAME/EMAIL`. CLI fills blanks by querying, in order: `FORGE_GIT_*` env → `git config user.name/email` on host → `gh api user` (`.name`,`.email`). Pure helper `resolve_identity(env_name, env_email, git_name, git_email, gh_name, gh_email) -> tuple[str,str]` is unit-tested; the imperative `gh`/`git` calls live in `cli.py`.

- [ ] **Step 1 — failing test**

```python
# tests/test_config_identity.py
from forge.cli import resolve_identity

def test_env_wins():
    assert resolve_identity("E","e@x", "G","g@x", "H","h@x") == ("E","e@x")

def test_falls_back_to_git_then_gh():
    assert resolve_identity("","", "G","g@x", "H","h@x") == ("G","g@x")
    assert resolve_identity("","", "","", "H","h@x") == ("H","h@x")
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement** `resolve_identity` (first non-empty name and email independently) and call it in `cli.main` before constructing the orchestrator, populating `cfg.git_author_name/email`.
- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(cli): resolve real git identity (env→git→gh)"`

---

## Task 11: Worker prompt knows the app + reproduces first

**Files:**
- Modify: `src/forge/prompts.py`, `src/forge/orchestrator.py`
- Test: `tests/test_prompts.py` (extend)

**Interfaces:**
- `build_task_prompt(task, app_url: str | None = None) -> str` — when `app_url` is set, append: "A live instance of this app is running at {app_url} (and on its in-container port). Before changing code, reproduce the reported problem against it; after your fix, confirm the symptom is gone."
- Orchestrator passes the **in-container** URL (`http://localhost:{app.port}`) so the worker can curl it. (The app is started before the first worker turn when `app.ok`; if startup is slow this is best-effort — health gate still runs later.)

- [ ] **Step 1 — failing test**

```python
# tests/test_prompts.py
from forge.prompts import build_task_prompt
def test_task_prompt_includes_app_url():
    p = build_task_prompt("fix X", "http://localhost:3000")
    assert "http://localhost:3000" in p and "reproduce" in p.lower()
def test_task_prompt_without_app_url_unchanged():
    assert "localhost" not in build_task_prompt("fix X")
```

- [ ] **Step 2 — run, verify fail.**
- [ ] **Step 3 — implement** the conditional suffix.
- [ ] **Step 4 — run, verify pass.**
- [ ] **Step 5 — commit**: `git commit -am "feat(worker): tell worker the live app URL + reproduce-first"`

---

## Task 12: Full suite + manual e2e checklist

**Files:** none (verification task).

- [ ] **Step 1** — `python -m pytest -q` → all green.
- [ ] **Step 2** — Build worker image with `curl` available: confirm `worker-image/Dockerfile` installs `curl` (it does) and rebuild: `docker build -t forge-worker worker-image/`.
- [ ] **Step 3 — manual e2e** (documented, run by the user; Docker required):
  ```bash
  export CLAUDE_CODE_OAUTH_TOKEN=$(... )   # subscription token
  export GH_TOKEN=$(gh auth token)
  forge run <owner/simple-next-app> "make a trivial visible change"
  # expect: "app: http://localhost:<port>"  → open it → see the running app
  forge status         # lists the live env
  forge down <run_id>  # env reaped
  ```
- [ ] **Step 4 — commit** any fixes found: `git commit -am "test: full suite green + e2e checklist"`

---

## Self-Review

- **Spec coverage (this increment):** clickable URL (T8/T9 ✓), contained per-run + warm/reap (T7/T8 ✓, concurrency-1 supersede), repo-first app detection (T2 ✓, single-service slice; full recipe resolution is plan 3), commit-as-you (T5/T10 ✓), worker memory via `--resume` (T5/T8 ✓), reproduce-first/app-aware worker (T11 ✓). **Deferred to later plans (noted in header):** Caddy `*.forge.localhost` proxy (plan 2), multi-service Compose + Next+Supabase seed/login (plan 3), DHIS2+CHAP + `bake` (plan 4), Playwright screenshots in PR body (plan 3). Subscription-only + orchestrator-owned verdicts: unchanged invariants, preserved.
- **Placeholder scan:** none — every step has concrete code/commands. The only intentional caller-coupling note is T5→T8 (signature lands in T5, orchestrator wiring in T8), called out explicitly.
- **Type consistency:** `AppSpec.start_argv/port/health_path/ok`, `web_url(host_port)`, `health_poll_argv(port,path,timeout)`, `worker_cmd(prompt,model,session_id)`, `commit_cmds(message,name,email)`, `DockerRunner.start(...,publish_port)`, env states `starting|live|reaped|failed`, `RunOutcome.web_url` — used consistently across tasks.
