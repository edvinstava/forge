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
CREATE TABLE IF NOT EXISTS envs (
  run_id TEXT PRIMARY KEY, project TEXT, web_url TEXT, web_port INTEGER,
  web_service TEXT,
  state TEXT NOT NULL DEFAULT 'starting',
  runtime_facts TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  last_seen_at TEXT DEFAULT (datetime('now')),
  reaped_at TEXT,
  asleep_at TEXT,
  state_reason TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now')), meta TEXT
);
CREATE TABLE IF NOT EXISTS supabase_ports (
  run_id TEXT PRIMARY KEY, port_offset INTEGER NOT NULL, project TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS slack_threads (
  thread_ts TEXT PRIMARY KEY, channel TEXT NOT NULL, run_id TEXT NOT NULL,
  anchor_ts TEXT, created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS checkpoints (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  ctype TEXT NOT NULL, payload TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  answer TEXT,
  created_at TEXT DEFAULT (datetime('now')), answered_at TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

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

    # --- env registry (live per-run app environments) ---

    def create_env(self, run_id, project, web_url, web_port, state,
                   web_service=None, runtime_facts=None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO envs"
                "(run_id, project, web_url, web_port, state, web_service, "
                "runtime_facts) VALUES (?,?,?,?,?,?,?)",
                (run_id, project, web_url, web_port, state, web_service,
                 runtime_facts))

    def set_snapshot_lockhash(self, run_id, h) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET snapshot_lockhash=? WHERE run_id=?", (h, run_id))

    def set_env_state(self, run_id, state, web_url=None) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET state=?, web_url=COALESCE(?, web_url), "
                      "last_seen_at=datetime('now') WHERE run_id=?",
                      (state, web_url, run_id))

    def touch_env(self, run_id) -> None:
        with self._conn() as c:
            c.execute("UPDATE envs SET last_seen_at=datetime('now') WHERE run_id=?",
                      (run_id,))

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

    def mark_asleep(self, run_id, reason=None) -> None:
        """Sleep a session: env torn down but resumable. Updates both tables
        so the UI (which reads runs.state) never shows a stale 'running'.
        `reason` records WHY (idle/web/slack/restart) so notifiers can label
        the transition — or stay silent when the surface already announced it."""
        with self._conn() as c:
            c.execute("UPDATE envs SET state='asleep', asleep_at=datetime('now'), "
                      "state_reason=? WHERE run_id=?", (reason, run_id))
            c.execute("UPDATE runs SET state='asleep', updated_at=datetime('now') "
                      "WHERE run_id=?", (run_id,))

    def mark_deleted(self, run_id, reason=None) -> None:
        """Tombstone a session: workspace removed, row + messages kept."""
        with self._conn() as c:
            c.execute("UPDATE envs SET state='deleted', state_reason=? "
                      "WHERE run_id=?", (reason, run_id))
            c.execute("UPDATE runs SET state='deleted', updated_at=datetime('now') "
                      "WHERE run_id=?", (run_id,))

    # --- per-run Supabase port-block reservations ---

    def reserve_supabase(self, run_id, offset, project) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO supabase_ports"
                      "(run_id, port_offset, project) VALUES (?,?,?)",
                      (run_id, offset, project))

    def release_supabase(self, run_id) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM supabase_ports WHERE run_id=?", (run_id,))

    def list_supabase(self) -> list:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT run_id, port_offset AS offset, project FROM supabase_ports"
            ).fetchall()]

    def get_supabase(self, run_id) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT run_id, port_offset AS offset, project "
                "FROM supabase_ports WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else {}

    # --- slack thread <-> run mapping ---

    def link_slack_thread(self, thread_ts, channel, run_id, anchor_ts) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO slack_threads"
                      "(thread_ts, channel, run_id, anchor_ts) VALUES (?,?,?,?)",
                      (thread_ts, channel, run_id, anchor_ts))

    def run_for_thread(self, thread_ts) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT run_id FROM slack_threads WHERE thread_ts=?",
                            (thread_ts,)).fetchone()
        return row["run_id"] if row else None

    def slack_thread_for_run(self, run_id) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM slack_threads WHERE run_id=? "
                            "ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return dict(row) if row else None

    def _migrate(self, c) -> None:
        cols = {r[1] for r in c.execute("PRAGMA table_info(runs)").fetchall()}
        for col in ("claude_session_id", "repo_source", "title",
                    "lifecycle_state", "plan_json",
                    "model", "batch_id", "queue_error", "auto_draft",
                    "agent_provider", "attachments_json"):
            if col not in cols:
                c.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        env_cols = {r[1] for r in c.execute("PRAGMA table_info(envs)").fetchall()}
        if "asleep_at" not in env_cols:
            c.execute("ALTER TABLE envs ADD COLUMN asleep_at TEXT")
        if "snapshot_lockhash" not in env_cols:
            c.execute("ALTER TABLE envs ADD COLUMN snapshot_lockhash TEXT")
        if "runtime_facts" not in env_cols:
            c.execute("ALTER TABLE envs ADD COLUMN runtime_facts TEXT")
        if "state_reason" not in env_cols:
            c.execute("ALTER TABLE envs ADD COLUMN state_reason TEXT")

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
                           repo_source=None, title=None,
                           agent_provider=None) -> None:
        # agent_provider records which CLI minted claude_session_id — a session
        # id is only resumable by the provider that created it.
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET claude_session_id=COALESCE(?, claude_session_id), "
                "repo_source=COALESCE(?, repo_source), title=COALESCE(?, title), "
                "agent_provider=COALESCE(?, agent_provider), "
                "updated_at=datetime('now') WHERE run_id=?",
                (claude_session_id, repo_source, title, agent_provider, run_id))

    def set_lifecycle_state(self, run_id, state) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET lifecycle_state=?, updated_at=datetime('now') "
                      "WHERE run_id=?", (state, run_id))

    # --- fire-and-forget batch queue ---

    def set_queue_fields(self, run_id, *, model=None, batch_id=None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET model=COALESCE(?, model), "
                "batch_id=COALESCE(?, batch_id), updated_at=datetime('now') "
                "WHERE run_id=?", (model, batch_id, run_id))

    def set_run_target(self, run_id, *, repo=None, branch=None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET repo=COALESCE(?, repo), "
                "branch=COALESCE(?, branch), updated_at=datetime('now') "
                "WHERE run_id=?", (repo, branch, run_id))

    def set_task(self, run_id, task) -> None:
        """Record the canonical build task on the run row. plan_task sets it so
        PR-title fallback and branch naming see the real ask even when the run
        was created before the task was known (interactive/Slack starts)."""
        with self._conn() as c:
            c.execute("UPDATE runs SET task=?, updated_at=datetime('now') "
                      "WHERE run_id=?", (task, run_id))

    def set_attachments(self, run_id, names_json) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET attachments_json=? WHERE run_id=?",
                      (names_json, run_id))

    def set_queue_error(self, run_id, error) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET queue_error=?, updated_at=datetime('now') "
                      "WHERE run_id=?", (error, run_id))

    def claim_queued(self, limit: int) -> list:
        """Atomically move the oldest `limit` queued rows to 'running' and return
        them. One connection/transaction so a row can never be double-dispatched:
        two concurrent callers cannot both claim the same row."""
        if limit <= 0:
            return []
        with self._conn() as c:
            rows = c.execute(
                "SELECT run_id FROM runs WHERE state='queued' "
                "ORDER BY created_at, run_id LIMIT ?", (limit,)).fetchall()
            ids = [r["run_id"] for r in rows]
            if not ids:
                return []
            qs = ",".join("?" * len(ids))
            c.execute(f"UPDATE runs SET state='running', updated_at=datetime('now') "
                      f"WHERE run_id IN ({qs})", ids)
            claimed = c.execute(
                f"SELECT * FROM runs WHERE run_id IN ({qs})", ids).fetchall()
        by_id = {r["run_id"]: dict(r) for r in claimed}
        return [by_id[i] for i in ids]                         # preserve FIFO order

    def cancel_queued(self, run_id) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE runs SET state='canceled', updated_at=datetime('now') "
                "WHERE run_id=? AND state='queued'", (run_id,))
            return cur.rowcount > 0

    def cancel_batch(self, batch_id) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT run_id FROM runs WHERE batch_id=? AND state='queued'",
                (batch_id,)).fetchall()
            ids = [r["run_id"] for r in rows]
            if ids:
                c.execute("UPDATE runs SET state='canceled', "
                          "updated_at=datetime('now') WHERE batch_id=? AND state='queued'",
                          (batch_id,))
        return ids

    def reclaim_orphans(self) -> list:
        """Restart recovery: batched runs left 'running' when the daemon died are
        reset to 'queued' for re-dispatch. Interactive turns (batch_id NULL) are
        left alone — their env is reconciled separately."""
        with self._conn() as c:
            rows = c.execute("SELECT run_id FROM runs WHERE state='running' "
                             "AND batch_id IS NOT NULL").fetchall()
            ids = [r["run_id"] for r in rows]
            if ids:
                c.execute("UPDATE runs SET state='queued', updated_at=datetime('now') "
                          "WHERE state='running' AND batch_id IS NOT NULL")
        return ids

    def list_runs(self, states=None) -> list:
        q = "SELECT * FROM runs"
        args = ()
        if states:
            q += " WHERE state IN (%s)" % ",".join("?" * len(states))
            args = tuple(states)
        q += " ORDER BY created_at"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]

    def set_plan(self, run_id, plan_json) -> None:
        with self._conn() as c:
            c.execute("UPDATE runs SET plan_json=?, updated_at=datetime('now') "
                      "WHERE run_id=?", (plan_json, run_id))

    def set_auto_draft(self, run_id, on) -> None:
        """Persist whether this run runs autonomously (drafts a PR on execution
        bottom-outs instead of stalling on a checkpoint). Stored as "1"/"" so it
        survives checkpoint resumes — respond_checkpoint re-reads it."""
        with self._conn() as c:
            c.execute("UPDATE runs SET auto_draft=? WHERE run_id=?",
                      ("1" if on else "", run_id))

    def count_checkpoints(self, run_id, ctype) -> int:
        """How many checkpoints of `ctype` this run has EVER had (any status).
        Used to enforce "ask for credentials at most once": a prior needs_input
        means we already asked, so don't ask again — draft the PR instead."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM checkpoints "
                             "WHERE run_id=? AND ctype=?", (run_id, ctype)).fetchone()[0]

    def create_checkpoint(self, run_id, ctype, payload) -> int:
        with self._conn() as c:
            c.execute("UPDATE checkpoints SET status='cancelled' "
                      "WHERE run_id=? AND status='open'", (run_id,))
            cur = c.execute(
                "INSERT INTO checkpoints(run_id, ctype, payload) VALUES (?,?,?)",
                (run_id, ctype, json.dumps(payload)))
            return cur.lastrowid

    def open_checkpoint(self, run_id) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM checkpoints WHERE run_id=? AND status='open' "
                            "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        d["answer"] = json.loads(d["answer"]) if d["answer"] else None
        return d

    def answer_checkpoint(self, checkpoint_id, answer) -> None:
        with self._conn() as c:
            c.execute("UPDATE checkpoints SET status='answered', answer=?, "
                      "answered_at=datetime('now') WHERE id=? AND status='open'",
                      (json.dumps(answer), checkpoint_id))

    def list_sessions(self) -> list:
        q = ("SELECT r.run_id, r.repo, r.title, r.state, r.repo_source, r.pr_url, "
             "r.batch_id, r.model, "
             "e.web_url, e.web_service, e.state AS env_state, "
             "MAX(r.updated_at, COALESCE(e.last_seen_at, r.updated_at)) AS last_active "
             "FROM runs r LEFT JOIN envs e ON e.run_id=r.run_id "
             "ORDER BY last_active DESC")
        with self._conn() as c:
            return [dict(r) for r in c.execute(q).fetchall()]
