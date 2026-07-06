from pathlib import Path
from types import SimpleNamespace
from forge import inbox
from forge.slackbot import ForgeSlackBot
from forge.reporesolve import Resolution


def TE(kind, **data):
    return SimpleNamespace(kind=kind, data=data)


def _art(name, kind, caption):
    return SimpleNamespace(path=Path("/runs/r/workspace/.forge/artifacts") / name,
                           kind=kind, caption=caption)


class FakeClient:
    def __init__(self):
        self.posts, self.updates, self.uploads = [], [], []
        self._ts = 1000
        # Slack history the chat path reads for multi-turn context. `_history`
        # is per-channel (DM, newest-first per the API); `_replies` per thread.
        self._history, self._replies = {}, {}
    def chat_postMessage(self, channel, text="", thread_ts=None, blocks=None):
        self._ts += 1
        self.posts.append(SimpleNamespace(channel=channel, text=text,
                                          thread_ts=thread_ts, blocks=blocks,
                                          ts=str(self._ts)))
        return {"ts": str(self._ts)}
    def chat_update(self, channel, ts, text="", blocks=None):
        self.updates.append(SimpleNamespace(channel=channel, ts=ts, text=text,
                                            blocks=blocks))
    def files_upload_v2(self, channel, file=None, title="", thread_ts=None,
                        initial_comment=None, content=None, filename=None):
        self.uploads.append(SimpleNamespace(channel=channel, file=file,
                                            title=title, thread_ts=thread_ts,
                                            content=content, filename=filename))
    def conversations_history(self, channel, limit=20):
        return {"messages": list(self._history.get(channel, []))}
    def conversations_replies(self, channel, ts, limit=20):
        return {"messages": list(self._replies.get((channel, ts), []))}


class FakeManager:
    def artifacts(self, run_id):
        return self._artifacts

    def __init__(self, start_events=None, turn_events=None, can=(True, ""), diff="",
                 artifacts=None):
        self._artifacts = artifacts or []
        self._start = start_events or [TE("phase", name="up", label="Starting stack"),
                                       TE("url", web_url="http://localhost:3001")]
        self._turn = turn_events or [TE("phase", name="agent", label="Agent working"),
                                     TE("done", diff_files=2, verify_ok=True)]
        self._can = can
        self._diff = diff
        self.calls = []
        self.plan_calls = []
        self.respond_calls = []
        self.batch_calls = []
        self.sinks = {}
        self.stopped = set()
        self.forget_calls = []
        self.sleep_reasons = {}
        self.attachments = []
    def save_attachment(self, run_id, filename, data, mimetype=None):
        self.attachments.append((run_id, filename, data, mimetype))
        return f"1-{filename}"
    def enqueue_batch(self, items):
        self.batch_calls.append(list(items))
        ids = [f"b{i}" for i in range(len(items))]
        return "batch-1", ids
    def set_event_sink(self, run_id, fn):
        self.sinks[run_id] = fn
    def can_start(self):
        return self._can
    def start(self, run_id, repo, source, task="", origin="api"):
        self.calls.append(("start", run_id, repo, source))
        yield from self._start
    def turn(self, run_id, prompt, model="auto", origin="api", attachments=None):
        self.calls.append(("turn", run_id, prompt, attachments))
        yield from self._turn
    def plan_task(self, run_id, task, model="auto", policy=None, autonomous=False,
                  auto_draft=None, origin="api", attachments=None):
        self.plan_calls.append(("plan_task", run_id, task, attachments))
        self.last_policy = policy
        self.last_auto_draft = auto_draft
        yield TE("plan", goal=task)
        yield TE("checkpoint", id=1, type="plan_approval", prompt="ok?")
        yield from self._turn
    def diff(self, run_id):
        self.calls.append(("diff", run_id))
        return self._diff
    def sleep(self, run_id):
        self.calls.append(("sleep", run_id))
        return True
    def request_sleep(self, run_id, reason="manual"):
        self.calls.append(("request_sleep", run_id))
        self.sleep_reasons[run_id] = reason
        return getattr(self, "sleep_status", "sleeping")
    def stop(self, run_id):
        self.stopped.add(run_id)
    def forget_credentials(self, slug):
        self.forget_calls.append(slug)
        return getattr(self, "forget_result", True)
    def respond_checkpoint(self, run_id, cid, action, body=None, model="auto",
                           origin="api"):
        self.respond_calls.append(("respond", run_id, action))
        yield TE("done", message=action)


class FakeResolver:
    def __init__(self, res):
        self._res = res
    def resolve(self, phrase):
        return self._res


class FakeTunnel:
    def __init__(self, url="https://x.trycloudflare.com"):
        self._url, self.started = url, []
    def start(self, run_id, target):
        self.started.append((run_id, target))
        return self._url
    def stop(self, run_id):
        pass


def _bot(store, manager=None, resolver=None, tunnel=None, client=None,
         allowed="U1", qa_answer=None, opener=None, bot_user_id="UBOT",
         chat_reply=None, chat_router=None):
    cfg = SimpleNamespace(slack_allowed_user=allowed)
    manager = manager or FakeManager()
    resolver = resolver or FakeResolver(
        Resolution("acme/landing-page", "high", ["acme/landing-page"]))
    return ForgeSlackBot(manager, store, cfg, resolver,
                         tunnel or FakeTunnel(), client or FakeClient(),
                         run_id_factory=lambda: "run-1", qa_answer=qa_answer,
                         opener=opener, bot_user_id=bot_user_id,
                         chat_reply=chat_reply, chat_router=chat_router)


def _make_bot():
    """Return a bot with a fresh in-memory store (no tmp_path needed by callers
    that just call _apply directly and don't need a path-backed store)."""
    from forge.store import Store
    import tempfile, os
    td = tempfile.mkdtemp()
    store = Store(os.path.join(td, "f.db"))
    return _bot(store, manager=FakeManager(), client=FakeClient())


def _make_bot_with_store():
    """Return (bot, store) backed by a fresh in-memory Store for tests that
    need direct store manipulation (e.g. to seed a run + checkpoint)."""
    from forge.store import Store
    import tempfile, os
    td = tempfile.mkdtemp()
    store = Store(os.path.join(td, "f.db"))
    manager = FakeManager()
    bot = _bot(store, manager=manager, client=FakeClient())
    return bot, store


def test_ignores_non_allowed_user(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    bot.handle_message("D1", "U_OTHER", "fix landing page")
    assert client.posts == []


def test_new_message_high_confidence_runs_start_then_turn(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    tunnel = FakeTunnel()
    client = FakeClient()
    bot = _bot(store, manager=manager, tunnel=tunnel, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    kinds = [c[0] for c in manager.calls if c[0] != "diff"]  # diff is a render detail
    assert kinds == ["start"]
    assert ("plan_task", "run-1", "fix the landing page repo", []) in manager.plan_calls
    assert manager.calls[0] == ("start", "run-1", "acme/landing-page", "github")
    assert store.run_for_thread(client.posts[0].ts) == "run-1"   # thread linked
    assert tunnel.started == [("run-1", "http://localhost:3001")]  # url -> tunnel
    # checklist mentions repo + public URL
    assert any("acme/landing-page" in u.text for u in client.updates)
    assert any("trycloudflare.com" in u.text for u in client.updates)


def test_list_message_enqueues_batch_and_threads(tmp_path):
    # A bulleted/numbered list to @forge is a batch: enqueue each line on the
    # resolved repo, post ONE summary, and thread each run with a sink registered.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)   # resolver → acme/landing-page
    bot.handle_message("D1", "U1", "- fix login\n- add logout\n- dark mode")
    # enqueue_batch called once with 3 items on the resolved slug
    assert len(manager.batch_calls) == 1
    items = manager.batch_calls[0]
    assert [it["task"] for it in items] == ["fix login", "add logout", "dark mode"]
    assert all(it["repo"] == "acme/landing-page" for it in items)
    # one summary post + one head post per run
    assert len(client.posts) == 1 + 3
    assert "Queued 3" in client.posts[0].text
    # a sink registered per run, and each head post ts linked to its run
    assert set(manager.sinks) == {"b0", "b1", "b2"}
    linked = {store.run_for_thread(p.ts) for p in client.posts[1:]}
    assert linked == {"b0", "b1", "b2"}


def test_numbered_list_is_detected_despite_chat_intent(tmp_path):
    # A numbered list classifies as "chat" by classify_intent; batch detection
    # must run BEFORE intent routing so it isn't swallowed by the chat path.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    bot = _bot(store, manager=manager, client=FakeClient())
    bot.handle_message("D1", "U1", "1. one\n2. two")
    assert len(manager.batch_calls) == 1
    assert [it["task"] for it in manager.batch_calls[0]] == ["one", "two"]


def test_url_event_surfaces_local_preview_link(tmp_path):
    # When the backend supplies a DNS-free local URL, Slack shows it alongside
    # the public tunnel link so the operator always has a link that opens even
    # if their network can't resolve *.trycloudflare.com.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(start_events=[
        TE("phase", name="up", label="Starting stack"),
        TE("url", web_url="https://demo.trycloudflare.com",
           local_url="http://run-1.forge.localhost:8088"),
    ])
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert any("run-1.forge.localhost:8088" in u.text for u in client.updates)


def test_url_event_surfaces_workspace_link_and_hides_dashboard(tmp_path):
    # With a configured forge_web_url and a live app, the live message shows the
    # 🗔 workspace link (the richer surface) and drops the 🧭 dashboard line.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(start_events=[
        TE("url", web_url="https://demo.trycloudflare.com",
           local_url="http://run-1.forge.localhost:8088"),
    ])
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.cfg.forge_web_url = "https://forge.example.com"
    bot.handle_message("D1", "U1", "fix the landing page repo")
    # Messages are edited in place (same ts): the pre-live "spinning up" edit
    # legitimately shows 🧭 before any app exists, then the SAME message is
    # rewritten to 🗔 once the url event fires. Assert on each message's FINAL
    # text (last edit per ts) — that's what the user actually sees.
    final_by_ts = {}
    for u in client.updates:
        final_by_ts[u.ts] = u.text
    finals = "\n---\n".join(final_by_ts.values())
    assert "🗔 https://forge.example.com/#live=run-1" in finals
    assert "🧭" not in finals  # once live, the workspace link supersedes 🧭


def test_none_confidence_asks_for_repo(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               resolver=FakeResolver(Resolution(None, "none", [])))
    # An explicit build verb but no resolvable repo still asks which repo
    # (the build path is unchanged; only the qa dead-end became conversational).
    bot.handle_message("D1", "U1", "add something")
    assert client.posts and "which repo" in client.posts[0].text.lower()
    assert manager.calls == []


def test_capacity_blocks_start(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(can=(False, "max_live_sessions reached (4)"))
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert ("start", "run-1", "acme/landing-page", "github") not in manager.calls
    assert any("max_live_sessions" in u.text for u in client.updates) or \
           any("max_live_sessions" in p.text for p in client.posts)


def test_follow_up_in_live_thread_runs_turn(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    manager = FakeManager()
    bot = _bot(store, manager=manager)
    bot.handle_message("D1", "U1", "also fix the footer", thread_ts="1001")
    assert ("turn", "run-1", "also fix the footer", []) in manager.calls
    assert ("start", "run-1", "acme/landing-page", "github") not in manager.calls


def test_follow_up_wakes_asleep_session_then_turns(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.mark_asleep("run-1")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    wake_called = []
    class M(FakeManager):
        def wake(self, run_id, origin="api"):
            wake_called.append(run_id)
            yield TE("phase", name="wake", label="Waking")
            yield TE("url", web_url="http://localhost:3001")
    bot = _bot(store, manager=M())
    bot.handle_message("D1", "U1", "continue", thread_ts="1001")
    assert wake_called == ["run-1"]


def test_ambiguous_then_numbered_pick_starts(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    res = Resolution(None, "ambiguous", ["acme/landing-page", "acme/landing-zone"])
    bot = _bot(store, manager=manager, client=client, resolver=FakeResolver(res))
    bot.handle_message("D1", "U1", "fix the landing thing")
    assert any("1." in p.text for p in client.posts)        # candidates posted
    bot.handle_message("D1", "U1", "1")                      # pick first
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls


def test_open_pr_action_posts_link(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    client = FakeClient()
    class M(FakeManager):
        def open_pr(self, run_id):
            return {"ok": True, "pr_url": "https://github.com/x/y/pull/1", "draft": False}
    bot = _bot(store, manager=M(), client=client)
    bot.handle_open_pr("D1", "run-1", user="U1")
    assert any("pull/1" in p.text for p in client.posts)


def test_open_pr_action_rejects_non_allowed_user(tmp_path):
    # The "Open PR" button is visible to the whole channel; a non-allowed user
    # clicking it must NOT push/open a PR under forge's token.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    client = FakeClient()
    opened = {"n": 0}
    class M(FakeManager):
        def open_pr(self, run_id):
            opened["n"] += 1
            return {"ok": True, "pr_url": "https://github.com/x/y/pull/1"}
    bot = _bot(store, manager=M(), client=client, allowed="U1")
    bot.handle_open_pr("D1", "run-1", user="U_OTHER")
    assert opened["n"] == 0                                # no PR was opened
    assert not any("pull/1" in p.text for p in client.posts)


def test_on_lifecycle_posts_to_thread(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    client = FakeClient()
    bot = _bot(store, client=client)
    bot.on_lifecycle("run-1", "asleep")
    assert any("slept" in p.text.lower() and p.thread_ts == "1001"
               for p in client.posts)


def _lifecycle_notice(tmp_path, reason, transition="asleep"):
    """Drive on_lifecycle for a run whose env carries `reason`; return posts."""
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.create_env("run-1", "forge-run-1", None, 3000, "live")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    if transition == "asleep":
        store.mark_asleep("run-1", reason=reason)
    else:
        store.mark_deleted("run-1", reason=reason)
    client = FakeClient()
    bot = _bot(store, client=client)
    bot.on_lifecycle("run-1", transition)
    return client.posts


def test_on_lifecycle_idle_sleep_says_idle(tmp_path):
    posts = _lifecycle_notice(tmp_path, "idle")
    assert any("slept after idle" in p.text for p in posts)


def test_on_lifecycle_web_sleep_says_web(tmp_path):
    posts = _lifecycle_notice(tmp_path, "web")
    assert any("web app" in p.text for p in posts)
    assert not any("after idle" in p.text for p in posts)


def test_on_lifecycle_slack_sleep_is_silent(tmp_path):
    # The sleep ack was already posted in-thread by the intent handler; the
    # lifecycle sweep must not double-announce ("sleeping…" + "slept after idle").
    posts = _lifecycle_notice(tmp_path, "slack")
    assert posts == []


def test_on_lifecycle_web_end_says_ended(tmp_path):
    posts = _lifecycle_notice(tmp_path, "web", transition="deleted")
    assert any("ended" in p.text for p in posts)
    assert not any("dormant" in p.text for p in posts)


def test_on_lifecycle_dormant_delete_keeps_archive_note(tmp_path):
    posts = _lifecycle_notice(tmp_path, "dormant", transition="deleted")
    assert any("archived" in p.text for p in posts)


# --- coworker rendering (Tasks 6-7) ---

def test_greeting_head_replaces_prompt_echo(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert any("👋" in u.text and "acme/landing-page" in u.text
               for u in client.updates)
    assert not any('"fix the landing page repo"' in u.text for u in client.updates)


def test_posts_its_up_reply_once_on_url(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    live = [p for p in client.posts if "It's up" in p.text]
    assert len(live) == 1 and live[0].thread_ts is not None


def test_done_no_diff_posts_plain_answer(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=0, verify_ok=None, message="You're on 2.41.3."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    answer = [p for p in client.posts if "2.41.3" in p.text]
    assert answer and answer[0].blocks is None


def test_done_with_diff_posts_summary_link_and_pr(tmp_path):
    from forge.store import Store
    client = FakeClient()
    diff = ("diff --git a/app/devotta/page.tsx b/app/devotta/page.tsx\n"
            "new file mode 100644\n+++ b/app/devotta/page.tsx\n+x\n")
    manager = FakeManager(diff=diff, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=3, verify_ok=False, message="Added the Devotta page."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    done = [p for p in client.posts if p.blocks]
    assert done and "/devotta" in done[0].text
    assert "Added the Devotta page." in done[0].text


def test_provision_runs_slack_builds_as_auto_draft(tmp_path):
    """A Slack build must run in auto_draft mode: never stall on an execution
    bottom-out, always land a PR — the "entirely from Slack" contract."""
    from forge.store import Store
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=FakeClient())
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert manager.last_auto_draft is True


def test_done_with_pr_url_posts_the_pr_link(tmp_path):
    """When execute auto-opened a PR (pr_url in the done event), Slack shows the
    actual PR link — the finish line — not a dangling 'Open PR' button."""
    from forge.store import Store
    client = FakeClient()
    diff = ("diff --git a/app/devotta/page.tsx b/app/devotta/page.tsx\n"
            "new file mode 100644\n+++ b/app/devotta/page.tsx\n+x\n")
    manager = FakeManager(diff=diff, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=3, verify_ok=True, message="Fixed the table.",
           pr_url="https://github.com/o/r/pull/7", draft=False),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    blob = " ".join(p.text for p in client.posts)
    assert "https://github.com/o/r/pull/7" in blob
    assert "Fixed the table." in blob


def test_done_draft_pr_is_flagged_as_draft(tmp_path):
    """A draft PR (e.g. visual QA couldn't be verified) is labelled 'draft' so
    the reviewer knows to check before merging."""
    from forge.store import Store
    client = FakeClient()
    diff = "diff --git a/x b/x\nnew file mode 100644\n+x\n"
    manager = FakeManager(diff=diff, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=1, verify_ok=True, message="Done.",
           pr_url="https://github.com/o/r/pull/8", draft=True),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    link = [p for p in client.posts if "pull/8" in p.text][0]
    assert "draft" in link.text.lower()


def test_needs_input_checkpoint_shows_work_summary(tmp_path):
    """The one-time credential ask carries the agent's work summary, so Slack
    reflects the actual work (like the web app) instead of a bare prompt."""
    bot = _make_bot()
    state = {"lines": [], "thread_ts": "100.1"}
    bot._apply("r1", "C1", "100.1", state, TE(
        "checkpoint", id=9, type="needs_input",
        prompt="Which login should I use?",
        summary="Fixed the offers table to read the offer price."))
    blob = " ".join(p.text for p in bot.client.posts)
    assert "Fixed the offers table to read the offer price." in blob
    assert "Which login should I use?" in blob


def test_checkpoint_without_summary_posts_no_spurious_done(tmp_path):
    """A plan/ambiguity checkpoint carries no summary — it must NOT emit a bogus
    'Done.' line (clean_summary's empty-string default) before the prompt."""
    bot = _make_bot()
    state = {"lines": []}
    bot._apply("r1", "C1", "100.1", state, TE(
        "checkpoint", id=3, type="ambiguity",
        prompt="Please answer the open questions."))
    assert not any(p.text == "Done." for p in bot.client.posts)


def test_failing_verify_is_honest_not_an_assertion(tmp_path):
    from forge.store import Store
    client = FakeClient()
    diff = "diff --git a/app/x/page.tsx b/app/x/page.tsx\nnew file mode 100644\n+x\n"
    manager = FakeManager(diff=diff, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("verify", ok=False, failed=["playwright"], output="missing libnss3"),
        TE("done", diff_files=2, verify_ok=False, message="TypeScript passes cleanly."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    done = [p for p in client.posts if p.blocks][0]
    assert "tests failing" not in done.text.lower()
    assert "playwright" in done.text
    assert "missing libnss3" in done.text          # one-line reason, auto-included
    assert "ask me why" not in done.text.lower()   # nudge removed
    assert "TypeScript passes cleanly." in done.text


# --- in-thread live progress ---

def test_narration_streams_to_in_thread_live_reply(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("narration", text="Editing the hero section"),
        TE("tool", name="Edit", target="page.tsx"),
        TE("done", diff_files=1, verify_ok=True, message="Done."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    # the "It's up" reply (from the start url) is the live message and gets
    # edited with narration — and it is a thread reply, not the parent.
    its_up = [p for p in client.posts if "It's up" in p.text][0]
    live_edits = [u for u in client.updates if u.ts == its_up.ts]
    assert any("Editing the hero section" in u.text for u in live_edits)
    assert any("page.tsx" in u.text for u in live_edits)


def test_no_web_build_opens_working_reply_for_narration(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(
        start_events=[TE("phase", name="up", label="Starting stack")],  # no url
        turn_events=[
            TE("phase", name="agent", label="Agent working"),
            TE("narration", text="Refactoring the parser"),
            TE("done", diff_files=1, verify_ok=None, message="Done."),
        ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    working = [p for p in client.posts if "working" in p.text.lower()
               and p.thread_ts is not None]
    assert working                                   # a live reply was opened
    assert any("Refactoring the parser" in u.text for u in client.updates)


# --- visual artifacts ---

_VIS_DIFF = ("diff --git a/app/devotta/page.tsx b/app/devotta/page.tsx\n"
             "new file mode 100644\n+++ b/app/devotta/page.tsx\n+x\n")


def _vis_manager(artifacts):
    return FakeManager(diff=_VIS_DIFF, artifacts=artifacts, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=2, verify_ok=True, message="Done."),
    ])


def test_finish_uploads_before_after_to_thread(tmp_path):
    from forge.store import Store
    client = FakeClient()
    arts = [_art("before.png", "before", "Broken footer"),
            _art("after.png", "after", "Fixed footer")]
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager(arts), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert len(client.uploads) == 2
    assert all(u.thread_ts is not None for u in client.uploads)
    assert {u.title for u in client.uploads} == {"Broken footer", "Fixed footer"}
    # a before/after lead-in is posted in the thread
    assert any("before" in p.text.lower() and "after" in p.text.lower()
               for p in client.posts)


def test_finish_video_lead_in(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client,
               manager=_vis_manager([_art("flow.mp4", "video", "Repro → fix")]))
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert len(client.uploads) == 1
    assert any("🎥" in p.text for p in client.posts)


def test_finish_no_artifacts_no_upload(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager([]), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert client.uploads == []


def test_finish_upload_failure_does_not_break_done(tmp_path):
    from forge.store import Store
    class BoomClient(FakeClient):
        def files_upload_v2(self, **kw):
            raise RuntimeError("slack down")
    client = BoomClient()
    arts = [_art("after.png", "after", "Result")]
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager(arts), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    # the done message (with the PR button block) still posted
    assert any(p.blocks and "Done." in p.text for p in client.posts)


def test_finish_upload_failure_posts_visible_note(tmp_path):
    from forge.store import Store
    class BoomClient(FakeClient):
        def files_upload_v2(self, **kw):
            raise RuntimeError("not_in_channel")
    client = BoomClient()
    arts = [_art("after.png", "after", "Result")]
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager(arts), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    # failure is surfaced, not swallowed
    assert any("couldn't attach" in p.text.lower() for p in client.posts)
    # the done message still posted
    assert any(p.blocks and "Done." in p.text for p in client.posts)


def test_finish_upload_missing_scope_note_names_the_scope(tmp_path):
    # A SlackApiError stringifies as a generic prefix with the real reason at
    # the very end, so truncation hides exactly the actionable part. When the
    # bot lacks files:write, the note must name the scope, not a dead generic.
    from forge.store import Store

    class _Resp(dict):
        pass

    class _SlackApiError(Exception):
        def __init__(self):
            self.response = _Resp(ok=False, error="missing_scope",
                                  needed="files:write",
                                  provided="chat:write,im:history")
            super().__init__("The request to the Slack API failed. "
                             "(url: https://slack.com/api/files.getUploadURLExternal)\n"
                             "The server responded with: {'ok': False}")

    class BoomClient(FakeClient):
        def files_upload_v2(self, **kw):
            raise _SlackApiError()

    client = BoomClient()
    arts = [_art("after.png", "after", "Result")]
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager(arts), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    note = next(p.text for p in client.posts
                if "couldn't attach" in p.text.lower())
    assert "files:write" in note
    # the raw "The server responded with:" generic must not be all the user sees
    assert "The server responded with" not in note


def test_finish_upload_other_slack_error_surfaces_code(tmp_path):
    from forge.store import Store

    class _SlackApiError(Exception):
        def __init__(self):
            self.response = {"ok": False, "error": "channel_not_found"}
            super().__init__("The request to the Slack API failed.\n"
                             "The server responded with: {'ok': False}")

    class BoomClient(FakeClient):
        def files_upload_v2(self, **kw):
            raise _SlackApiError()

    client = BoomClient()
    arts = [_art("after.png", "after", "Result")]
    bot = _bot(Store(tmp_path / "f.db"), manager=_vis_manager(arts), client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    note = next(p.text for p in client.posts
                if "couldn't attach" in p.text.lower())
    assert "channel_not_found" in note


def test_finish_skips_artifacts_for_qa_answer(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(artifacts=[_art("after.png", "after", "x")], turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=0, verify_ok=None, message="You're on 2.41.3."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert client.uploads == []      # answer turns never post screenshots


# --- conversational opener ---

def test_build_opener_used_as_head_with_task_and_mode(tmp_path):
    from forge.store import Store
    client = FakeClient()
    seen = []
    def opener(slug, task, mode, fallback):
        seen.append((slug, task, mode))
        return "Hey! On it — pulling up the repo now 👀"
    bot = _bot(Store(tmp_path / "f.db"), client=client, opener=opener)
    bot.handle_message("D1", "U1", "hi! fix the landing page repo")
    assert any("Hey! On it — pulling up the repo now 👀" in u.text
               for u in client.updates)
    assert seen and seen[0][2] == "build"
    assert seen[0][0] == "acme/landing-page"
    assert "fix the landing page repo" in seen[0][1]


def test_build_opener_fallback_is_template_when_not_injected(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)   # no opener
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert any("👋" in u.text and "acme/landing-page" in u.text
               for u in client.updates)


def test_qa_opener_used_as_root_message(tmp_path):
    from forge.store import Store
    client = FakeClient()
    seen = []
    def opener(slug, task, mode, fallback):
        seen.append((slug, task, mode))
        return "Sure thing — let me peek 👀"
    bot = _bot(Store(tmp_path / "f.db"), client=client, opener=opener,
               qa_answer=lambda slug, q: "You're on 2.41.3.")
    bot.handle_message("D1", "U1", "what version is the landing page on?")
    assert any("Sure thing — let me peek 👀" in p.text for p in client.posts)
    assert seen and seen[0][2] == "qa"
    assert "version" in seen[0][1]


# --- intent routing + Q&A fast path (Tasks 8-9) ---

def test_sleep_command_sleeps_and_confirms(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.handle_message("D1", "U1", "you can sleep now", thread_ts="1001")
    assert ("request_sleep", "run-1") in manager.calls   # graceful sleep entrypoint
    assert manager.sleep_reasons["run-1"] == "slack"     # sweep notice stays silent
    assert any("💤" in p.text and p.thread_ts == "1001" for p in client.posts)


def test_create_sleep_timer_is_build_not_sleep(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    manager = FakeManager()
    bot = _bot(store, manager=manager)
    bot.handle_message("D1", "U1", "create a sleep timer page", thread_ts="1001")
    assert ("turn", "run-1", "create a sleep timer page", []) in manager.calls
    assert ("sleep", "run-1") not in manager.calls


def test_chat_intent_falls_back_to_blurb_when_no_reply_injected(tmp_path):
    from forge.store import Store
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "how does forge work?")
    assert any("forge" in p.text.lower() for p in client.posts)
    assert manager.calls == []


def test_chat_intent_posts_llm_reply(tmp_path):
    from forge.store import Store
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               chat_reply=lambda transcript, latest: "I spin repos up in a sandbox 🙂")
    bot.handle_message("D1", "U1", "what can you do?")
    assert any("I spin repos up in a sandbox 🙂" == p.text for p in client.posts)
    assert manager.calls == []                       # no repo resolution, no build


def test_unresolvable_question_falls_to_chat_not_which_repo(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               resolver=FakeResolver(Resolution(None, "none", [])),
               chat_reply=lambda transcript, latest: "Happy to help — which repo?")
    bot.handle_message("D1", "U1", "what's the best way to structure this?")
    assert any("Happy to help" in p.text for p in client.posts)
    assert not any("which repo? tell me a name" in p.text.lower() for p in client.posts)
    assert manager.calls == []


def test_chat_passes_prior_thread_history_as_transcript(tmp_path):
    from forge.store import Store
    client = FakeClient()
    # Seed prior DM messages (newest-first, as the Slack API returns them).
    client._history["D1"] = [{"user": "UBOT", "text": "I'm forge 👋"},
                             {"user": "U1", "text": "hello forge"}]
    seen = {}
    def chat_reply(transcript, latest):
        seen["transcript"], seen["latest"] = transcript, latest
        return "reply"
    bot = _bot(Store(tmp_path / "f.db"), client=client, chat_reply=chat_reply)
    bot.handle_message("D1", "U1", "what can you do?")
    assert "hello forge" in seen["transcript"]
    assert "forge: I'm forge 👋" in seen["transcript"]
    assert seen["latest"] == "what can you do?"


def test_chat_in_channel_owns_thread_and_continues(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager()
    replies = iter(["here's how I work…", "sure, more detail…"])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               chat_reply=lambda transcript, latest: next(replies))
    bot.handle_mention("C1", "U1", "<@UBOT> what can you do?", "5000")
    assert any(p.text == "here's how I work…" and p.thread_ts == "5000"
               for p in client.posts)
    # a follow-up in the same thread (no re-mention) continues the chat
    bot.route_channel_message("C1", "U1", "ok cool thanks", "5000")
    assert any(p.text == "sure, more detail…" for p in client.posts)
    assert manager.calls == []                       # stayed conversational


def test_non_english_build_routes_to_session_via_router(tmp_path):
    # The regex classifier only knows English verbs, so a Norwegian build
    # request ("lag en … side") classifies as chat. The LLM router rescues it:
    # action=build → a real session starts (was: a friendly but empty reply).
    from forge.store import Store
    from forge.slackroute import Route
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client,
               chat_router=lambda transcript, latest: Route("build", None))
    bot.handle_message("D1", "U1", "lag en hello-henrik side på webapp")
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls
    assert store.run_for_thread(client.posts[0].ts) == "run-1"      # thread linked


def test_build_phrased_as_question_routes_to_session(tmp_path):
    # A build request phrased politely as a question ("kan du lage … side?")
    # ends in "?", so the regex calls it qa. The router rescues it to a build
    # instead of answering "yes I can!" and building nothing.
    from forge.store import Store
    from forge.slackroute import Route
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client,
               chat_router=lambda transcript, latest: Route("build", None))
    bot.handle_message("D1", "U1", "kan du lage en hello-henrik side på webapp?")
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls


def test_router_qa_uses_qa_path_not_build(tmp_path):
    from forge.store import Store
    from forge.slackroute import Route
    seen = []
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               qa_answer=lambda slug, q: seen.append((slug, q)) or "On 2.41.3.",
               chat_router=lambda transcript, latest: Route("qa", None))
    bot.handle_message("D1", "U1", "hvilken versjon kjører webapp?")
    assert manager.calls == []                       # no build
    assert seen and seen[0][0] == "acme/landing-page"


def test_router_chat_posts_reply_without_building(tmp_path):
    from forge.store import Store
    from forge.slackroute import Route
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               chat_router=lambda transcript, latest: Route("chat", "hei på deg! 👋"))
    bot.handle_message("D1", "U1", "heisann")
    assert any(p.text == "hei på deg! 👋" for p in client.posts)
    assert manager.calls == []


def test_default_router_preserves_chat_reply(tmp_path):
    # No router injected → falls back to the chat_reply LLM, i.e. behavior is
    # identical to before the router existed.
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client,
               chat_reply=lambda transcript, latest: "I spin repos up 🙂")
    bot.handle_message("D1", "U1", "what can you do?")
    assert any(p.text == "I spin repos up 🙂" for p in client.posts)


def test_dm_thread_reply_continues_in_that_thread(tmp_path):
    # A reply made INSIDE a DM thread (thread_ts set, root_ts not passed by the
    # caller) must be answered in that thread, not at the top level of the DM.
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client,
               chat_reply=lambda transcript, latest: "still here")
    bot.handle_message("D1", "U1", "just checking in", thread_ts="T1")
    replies = [p for p in client.posts if p.text == "still here"]
    assert replies and replies[0].thread_ts == "T1"


def test_dm_thread_build_continues_in_that_thread(tmp_path):
    # A build request typed inside a DM thread should spin up IN that thread.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    client = FakeClient()
    bot = _bot(store, client=client)
    bot.handle_message("D1", "U1", "fix the landing page", thread_ts="T1")
    assert any(p.thread_ts == "T1" for p in client.posts)            # ack in-thread
    assert store.run_for_thread("T1") == "run-1"                     # thread keyed on T1


def test_chat_thread_hands_off_to_build_on_verb(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    client = FakeClient()
    manager = FakeManager()
    bot = _bot(store, manager=manager, client=client,
               chat_reply=lambda transcript, latest: "let's chat")
    bot.handle_mention("C1", "U1", "<@UBOT> hey there", "5000")     # opens a chat thread
    # a build instruction in that same chat thread starts a session
    bot.route_channel_message("C1", "U1", "add a logout button", "5000")
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls
    assert store.run_for_thread("5000") == "run-1"


def test_follow_up_question_with_prior_diff_renders_as_answer(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    client = FakeClient()
    manager = FakeManager(diff="", turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=5, verify_ok=False, message="No tests are failing."),
    ])
    bot = _bot(store, manager=manager, client=client)
    bot.handle_message("D1", "U1", "what tests are failing?", thread_ts="1001")
    answer = [p for p in client.posts if "No tests are failing." in p.text]
    assert answer and answer[0].blocks is None
    assert not any("file(s) changed" in p.text for p in client.posts)


def test_fresh_qa_uses_fast_path_not_manager(tmp_path):
    from forge.store import Store
    seen = []
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client,
               qa_answer=lambda slug, q: seen.append((slug, q)) or "You're on 2.41.3.")
    bot.handle_message("D1", "U1", "what version is the landing page on?")
    assert manager.calls == []
    assert any("2.41.3" in p.text for p in client.posts)
    assert seen and seen[0][0] == "acme/landing-page"


def test_qa_thread_follow_up_reuses_slug(tmp_path):
    from forge.store import Store
    seen = []
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client,
               qa_answer=lambda slug, q: seen.append((slug, q)) or "ok")
    bot.handle_message("D1", "U1", "what version is the landing page on?")
    root_ts = client.posts[0].ts
    bot.handle_message("D1", "U1", "and the node version?", thread_ts=root_ts)
    assert seen[-1] == ("acme/landing-page", "and the node version?")


def test_review_intent_drives_manager_review_and_posts_url(tmp_path):
    from forge.store import Store

    class RevManager(FakeManager):
        def review(self, run_id, pr, model="auto", origin="api"):
            self.calls.append(("review", run_id, pr))
            yield TE("phase", name="clone", label="Checking out PR #3")
            yield TE("review", ok=True, review_url="https://gh/o/r/pull/3#x",
                     comments=2, dropped=1, degraded=False)

    client = FakeClient()
    manager = RevManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "review o/r#3")
    # manager.review gets the normalized ref, not the raw message
    assert any(c[0] == "review" and c[2] == "o/r#3" for c in manager.calls)
    assert any("pull/3" in p.text for p in client.posts)


# --- channel support ---

def test_channel_mention_starts_session_threaded_under_mention(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.handle_mention("C1", "U1", "<@UBOT> fix the landing page repo", "5000")
    # run is linked to the *mention* root, not to forge's own ack message
    assert store.run_for_thread("5000") == "run-1"
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls
    # the ack + replies are posted in-thread under the mention
    assert client.posts and client.posts[0].thread_ts == "5000"


def test_channel_thread_reply_continues_without_mention(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("5000", "C1", "run-1", "5001")  # root=5000
    manager = FakeManager()
    bot = _bot(store, manager=manager)
    bot.route_channel_message("C1", "U1", "also fix the footer", "5000")
    assert ("turn", "run-1", "also fix the footer", []) in manager.calls


def test_channel_ambiguous_pick_resolves_in_thread(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    res = Resolution(None, "ambiguous",
                     ["acme/landing-page", "acme/landing-zone"])
    bot = _bot(store, manager=manager, client=client, resolver=FakeResolver(res))
    bot.handle_mention("C1", "U1", "<@UBOT> fix the landing thing", "5000")
    assert any("1." in p.text for p in client.posts)        # candidates in-thread
    # a bare numeric reply in the same thread (no re-mention) resolves it
    bot.route_channel_message("C1", "U1", "1", "5000")
    assert ("start", "run-1", "acme/landing-page", "github") in manager.calls


def test_channel_ambient_message_ignored(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.route_channel_message("C1", "U1", "just chatting", None)         # top-level
    bot.route_channel_message("C1", "U1", "in another thread", "999")    # unknown
    assert manager.calls == [] and client.posts == []


def test_channel_message_with_mention_is_skipped(tmp_path):
    # A top-level @forge fires BOTH app_mention and message events; the message
    # path must no-op so the work isn't run twice.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.link_slack_thread("5000", "C1", "run-1", "5001")
    manager = FakeManager()
    bot = _bot(store, manager=manager)
    bot.route_channel_message("C1", "U1", "<@UBOT> also fix the footer", "5000")
    assert manager.calls == []


def test_channel_non_allowed_user_gets_notice_once(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    bot.handle_mention("C1", "U_OTHER", "<@UBOT> do my bidding", "5000")
    bot.handle_mention("C1", "U_OTHER", "<@UBOT> please?", "5001")
    notices = [p for p in client.posts if "only take instructions" in p.text.lower()]
    assert len(notices) == 1                # deduped per (channel, user)
    assert "<@U1>" in notices[0].text       # names the allowed user


def test_channel_non_allowed_user_does_not_start_a_run(tmp_path):
    from forge.store import Store
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager)
    bot.handle_mention("C1", "U_OTHER", "<@UBOT> fix the landing page", "5000")
    assert manager.calls == []


# --- per-thread message queue ---

import threading


def test_submit_queues_reentrant_followup(tmp_path):
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    order = []
    def job2():
        order.append("job2")
    def job1():
        order.append("job1-start")
        bot._submit("D1", "T", job2)        # a message arrives mid-turn
        order.append("job1-end")            # job2 must NOT run yet
    bot._submit("D1", "T", job1)
    assert order == ["job1-start", "job1-end", "job2"]   # FIFO, after job1
    assert any("right after" in p.text for p in client.posts)   # queued ack


def test_submit_one_job_failing_does_not_strand_queue(tmp_path):
    from forge.store import Store
    bot = _bot(Store(tmp_path / "f.db"))
    ran = []
    def job2():
        ran.append("job2")
    def job1():
        bot._submit("D1", "T", job2)        # queued while job1 is running
        raise RuntimeError("boom")
    bot._submit("D1", "T", job1)            # job1 raises; job2 still drains
    assert ran == ["job2"]


def test_submit_concurrent_no_double_run(tmp_path):
    from forge.store import Store
    bot = _bot(Store(tmp_path / "f.db"))
    started, release, runs = threading.Event(), threading.Event(), []
    def job1():
        runs.append("j1"); started.set(); release.wait(2)
    def job2():
        runs.append("j2")
    t = threading.Thread(target=lambda: bot._submit("D1", "T", job1))
    t.start()
    assert started.wait(2)                  # j1 owns the thread
    bot._submit("D1", "T", job2)            # main thread: must enqueue, not run
    assert "j2" not in runs
    release.set(); t.join(2)
    assert runs == ["j1", "j2"]             # j2 ran once, after j1


def test_followup_while_running_is_queued_then_runs(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("1001", "D1", "run-1", "1001")
    client = FakeClient()
    # turn that submits a second follow-up to the SAME thread while "in flight"
    class M(FakeManager):
        def turn(self, run_id, prompt, model="auto", origin="api", attachments=None):
            self.calls.append(("turn", run_id, prompt, attachments))
            if prompt == "first":
                bot._submit("D1", "1001",
                            lambda: bot._follow_up("D1", "1001", "second", mode="build"))
            yield from self._turn
    bot = _bot(store, manager=M(), client=client)
    bot.handle_message("D1", "U1", "first", thread_ts="1001")
    prompts = [c[2] for c in bot.manager.calls if c[0] == "turn"]
    assert prompts == ["first", "second"]   # second ran after first, in order


# --- regression: Slack client errors must not silently kill a turn ---

class _UpdateRaisesClient(FakeClient):
    """A client whose chat_update always fails — models Slack rate-limiting
    (429) of rapid live-edits. Posts (incl. the final result) still succeed."""
    def chat_update(self, channel, ts, text="", blocks=None):
        super().chat_update(channel, ts, text=text, blocks=blocks)
        raise RuntimeError("ratelimited")


def test_render_error_midturn_does_not_abort_turn(tmp_path):
    # The live-feedback work multiplied chat_update calls (per-narration +
    # per-tool live edits). If any edit raises, the turn must still finish and
    # post its result — a dropped progress edit is cosmetic, not fatal.
    from forge.store import Store
    client = _UpdateRaisesClient()
    diff = "diff --git a/app/x/page.tsx b/app/x/page.tsx\nnew file mode 100644\n+x\n"
    manager = FakeManager(diff=diff, turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("narration", text="Editing the hero section"),
        TE("tool", target="app/x/page.tsx"),
        TE("done", diff_files=1, verify_ok=True, message="Added the page."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert any(c[0] == "plan_task" for c in manager.plan_calls)  # plan_task actually ran
    done = [p for p in client.posts if p.blocks]
    assert done and "Added the page." in done[0].text            # result still posted


def test_submit_job_failure_posts_visible_notice(tmp_path):
    # A turn-driver that dies (e.g. the engine raised) must not vanish silently;
    # the thread gets a short failure notice so the user isn't left hanging.
    from forge.store import Store
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client)
    def job():
        raise RuntimeError("worker crashed")
    bot._submit("D1", "1001", job)
    assert any(p.thread_ts == "1001" and "⚠️" in p.text for p in client.posts)


def test_install_rate_limit_retry_adds_handler_idempotently():
    # 429s on rapid chat.update must be retried, not raised mid-turn. The default
    # WebClient only retries connection errors; we add the rate-limit handler.
    pytest = __import__("pytest")
    pytest.importorskip("slack_sdk")
    from slack_sdk import WebClient
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    from forge.slackbot import install_rate_limit_retry

    client = WebClient(token="x")
    assert not any(isinstance(h, RateLimitErrorRetryHandler)
                   for h in client.retry_handlers)
    install_rate_limit_retry(client)
    install_rate_limit_retry(client)            # idempotent — no duplicate
    n = sum(isinstance(h, RateLimitErrorRetryHandler)
            for h in client.retry_handlers)
    assert n == 1


# --- Task 8: plan/checkpoint rendering + checkpoint reply routing ---

def test_apply_renders_plan_and_checkpoint(tmp_path):
    # _apply should post the plan and a checkpoint prompt without raising
    bot = _make_bot()
    state = {"lines": []}
    bot._apply("r1", "C1", "100.1", state, TE("plan", goal="Add logout",
                                              steps=[], acceptance=["works"],
                                              open_questions=[], risk="low"))
    bot._apply("r1", "C1", "100.1", state, TE("checkpoint", id=7,
                                              type="plan_approval",
                                              prompt="Approve this plan?"))
    blob = " ".join(p.text for p in bot.client.posts)
    assert "Add logout" in blob
    assert "Approve" in blob
    assert state.get("checkpoint_id") == 7


def test_plan_posts_short_line_with_collapsed_snippet(tmp_path):
    """A real plan (several sentence-long steps) → one glanceable line in the
    thread; the full plan is attached as a snippet (collapsed in Slack),
    never a wall of text."""
    bot = _make_bot()
    state = {"lines": []}
    steps = [{"intent": f"step number {i} — read the component, add the field, "
                        "wire the change handler through, and verify the total "
                        "updates in the card footer"} for i in range(1, 9)]
    bot._apply("r1", "C1", "100.1", state,
               TE("plan", goal="Add emission fields to the offer form",
                  steps=steps, acceptance=["emissions editable in the form"],
                  open_questions=[], risk="medium"))
    post = bot.client.posts[-1]
    assert "Add emission fields to the offer form" in post.text
    assert "8 steps" in post.text
    assert "step number 3" not in post.text        # body lives in the snippet
    up = bot.client.uploads[-1]
    assert up.filename == "plan.md"
    assert up.thread_ts == "100.1"
    assert "step number 3" in up.content
    assert "emissions editable in the form" in up.content


def test_short_plan_posts_inline_without_snippet(tmp_path):
    """A plan that fits in a glance stays inline — no gratuitous snippet."""
    bot = _make_bot()
    state = {"lines": []}
    bot._apply("r1", "C1", "100.1", state,
               TE("plan", goal="Add logout", steps=[{"intent": "add the button"}],
                  acceptance=[], open_questions=[], risk="low"))
    assert "Add logout" in bot.client.posts[-1].text
    assert bot.client.uploads == []


def test_thread_reply_approves_open_checkpoint(tmp_path):
    bot, store = _make_bot_with_store()
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.link_slack_thread("100.1", "C1", "r1", "100.1")
    store.create_checkpoint("r1", "plan_approval", {"plan": {"goal": "x"}})
    bot.manager.respond_calls = []
    bot.handle_message("C1", bot.cfg.slack_allowed_user, "approve",
                       thread_ts="100.1", root_ts="100.0")
    assert ("respond", "r1", "approve") in bot.manager.respond_calls
    # The turn must not have died in _submit (which swallows exceptions and posts
    # a generic "hit an error" note) — a bare state dict used to KeyError here.
    assert not any("hit an error and stopped" in p.text for p in bot.client.posts)


def test_cred_reply_drafting_a_pr_posts_the_link_in_thread(tmp_path):
    """Entirely-from-Slack contract: replying with creds to a login-wall pause
    that still can't be crossed drafts a PR — and the PR link lands in the thread
    (the finish line), flagged as a draft."""
    bot, store = _make_bot_with_store()
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.link_slack_thread("100.1", "C1", "r1", "100.1")
    store.create_checkpoint("r1", "needs_input",
                            {"blocked": {"kind": "needs_credentials"}})

    class M(FakeManager):
        def respond_checkpoint(self, run_id, cid, action, body=None, model="auto",
                           origin="api"):
            self.respond_calls.append(("respond", run_id, action))
            yield TE("phase", name="agent", label="Agent working")
            yield TE("done", message="Fixed the offers table.", diff_files=2,
                     verify_ok=True, pr_url="https://github.com/o/r/pull/9",
                     draft=True)
    bot.manager = M()

    bot.handle_message("C1", bot.cfg.slack_allowed_user, "admin@x :: secret",
                       thread_ts="100.1", root_ts="100.0")

    blob = " ".join(p.text for p in bot.client.posts)
    assert "https://github.com/o/r/pull/9" in blob
    assert "draft" in blob.lower()
    assert not any("hit an error and stopped" in p.text for p in bot.client.posts)


def test_checkpoint_reply_streams_execution_without_crashing(tmp_path):
    # Regression: answering a checkpoint built state={"lines": []}, so resuming
    # execution KeyError'd on state["thread_ts"] the moment the `agent` phase
    # opened its in-thread live reply (and again on state["head"] in _render).
    # That crash was swallowed by _submit, leaving the user with only a generic
    # error note after they replied "approve".
    bot, store = _make_bot_with_store()
    store.create_run("r1", "o/r", "Add logout", "forge/x")
    store.link_slack_thread("100.1", "C1", "r1", "100.1")
    store.create_checkpoint("r1", "ambiguity", {"plan": {"goal": "x"}})

    class M(FakeManager):
        def respond_checkpoint(self, run_id, cid, action, body=None, model="auto",
                           origin="api"):
            self.respond_calls.append(("respond", run_id, action))
            yield TE("phase", name="agent", label="Agent working")
            yield TE("done", message="all set", diff_files=0, verify_ok=True)
    bot.manager = M()

    bot.handle_message("C1", bot.cfg.slack_allowed_user, "approve",
                       thread_ts="100.1", root_ts="100.0")

    assert ("respond", "r1", "approve") in bot.manager.respond_calls
    assert not any("hit an error and stopped" in p.text for p in bot.client.posts)
    # Live progress + the final summary both land in the conversation thread.
    assert any("working" in p.text.lower() and p.thread_ts == "100.1"
               for p in bot.client.posts)
    assert any(p.text == "all set" and p.thread_ts == "100.1"
               for p in bot.client.posts)


def test_slack_build_is_autonomous_no_plan_approval_gate(tmp_path):
    # "Just figure it out": the Slack build path hands plan_task a policy that
    # skips plan approval but still asks when the plan is unsure (ambiguity).
    from forge import flow
    bot, store = _make_bot_with_store()
    bot._provision_and_fix("r1", "C1", "100.1", "o/r", "add logout", "100.1")
    pol = bot.manager.last_policy
    assert pol is not None
    assert not pol.gates(flow.PLAN_APPROVAL)
    assert pol.gates(flow.AMBIGUITY)


def test_new_build_routes_to_plan_task(tmp_path):
    bot, store = _make_bot_with_store()
    bot.manager.plan_calls = []
    # FakeManager.start yields its start events; FakeManager.plan_task records + yields plan/checkpoint
    bot._provision_and_fix("r1", "C1", "100.1", "o/r", "add logout", "100.1")
    assert ("plan_task", "r1", "add logout", []) in bot.manager.plan_calls
    assert ("turn", "r1", "add logout", []) not in getattr(bot.manager, "calls", [])


# --- interrupt / creds thread commands ---

def test_stop_command_calls_manager_stop_immediately(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(); client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    store.create_run("run-1", "o/r", "t", "forge/x")
    bot._thread_command_or_turn("D1", "1001", "run-1", "stop")
    assert "run-1" in manager.stopped
    assert any("🛑" in p.text for p in client.posts)


def test_sleep_midturn_says_will_pause(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(); manager.sleep_status = "deferred"
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    store.create_run("run-1", "o/r", "t", "forge/x")
    bot._thread_command_or_turn("D1", "1001", "run-1", "sleep")
    assert any("will pause after this step" in p.text for p in client.posts)


def test_forget_creds_command_calls_manager(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(); client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    store.create_run("run-1", "o/r", "t", "forge/x")
    bot._thread_command_or_turn("D1", "1001", "run-1", "forget creds")
    assert manager.forget_calls == ["o/r"]
    assert any("forgot" in p.text.lower() for p in client.posts)


def test_giant_narration_never_sends_oversized_updates(tmp_path):
    # Slack rejects long chat.update text with msg_too_long, and the failure
    # repeats on every subsequent edit of the same message. The bot must cap
    # everything outbound instead (seen live: a huge agent narration killed the
    # progress message for the rest of the run).
    from forge.store import Store
    from forge.slackmsg import SLACK_TEXT_LIMIT
    wall = "word " * 20000            # ~100k chars
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("narration", text=wall),
        TE("done", diff_files=1, verify_ok=True, message=wall)])
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert client.updates and client.posts
    assert all(len(u.text) <= SLACK_TEXT_LIMIT + 2 for u in client.updates)
    assert all(len(p.text) <= SLACK_TEXT_LIMIT + 2 for p in client.posts)


def test_live_narration_is_a_single_glanceable_line(tmp_path):
    from forge.store import Store
    manager = FakeManager(turn_events=[
        TE("url", web_url="http://localhost:3001"),
        TE("phase", name="agent", label="Agent working"),
        TE("narration", text="I looked at the table.\nNow I will " + "x" * 900),
        TE("done", diff_files=1, verify_ok=True, message="done")])
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    live = [u.text for u in client.updates if "🛠️" in u.text]
    assert live
    narr_lines = [ln for u in live for ln in u.splitlines() if ln.startswith("🛠️")]
    assert all(len(ln) < 260 and "\n" not in ln for ln in narr_lines)


def test_batch_sink_posts_artifacts_with_the_pr_link(tmp_path):
    # Fire-and-forget batch runs finish inside the scheduler sink, not _finish —
    # screenshots must still reach the thread there.
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager(artifacts=[_art("after.png", "after", "Fixed table")])
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    bot.handle_message("D1", "U1", "- fix login\n- add logout")
    sink = manager.sinks["b0"]
    sink(TE("done", pr_url="https://github.com/o/r/pull/9", draft=True))
    assert any("pull/9" in p.text for p in client.posts)
    assert len(client.uploads) == 1 and client.uploads[0].title == "Fixed table"


def test_pr_button_blocks_respect_section_limit(tmp_path):
    from forge.store import Store
    bot = _bot(Store(tmp_path / "f.db"), client=FakeClient())
    blocks = bot._pr_button("r1", "y" * 5000)
    assert len(blocks[0]["text"]["text"]) <= 3000


def test_remember_in_session_thread_saves_lesson_for_repo(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    manager.lessons = []
    manager.remember_lesson = lambda slug, text: (
        manager.lessons.append((slug, text)) or True)
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)
    store.create_run("run-1", "o/r", "t", "forge/x")
    bot._thread_command_or_turn("D1", "1001", "run-1",
                                "remember: always run bun install first")
    assert manager.lessons == [("o/r", "always run bun install first")]
    assert any("🧠" in p.text for p in client.posts)


def test_remember_top_level_resolves_repo_or_asks(tmp_path):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    manager = FakeManager()
    manager.lessons = []
    manager.remember_lesson = lambda slug, text: (
        manager.lessons.append((slug, text)) or True)
    client = FakeClient()
    bot = _bot(store, manager=manager, client=client)   # resolver → high confidence
    bot.handle_message("D1", "U1", "remember for landing-page: deploy needs `bun run gen`")
    assert manager.lessons == [("acme/landing-page", "deploy needs `bun run gen`")]
    # Unresolvable repo → ask, never guess.
    from forge.reporesolve import Resolution
    bot2 = _bot(Store(tmp_path / "f2.db"), manager=manager, client=client,
                resolver=FakeResolver(Resolution(None, "none", [])))
    n = len(manager.lessons)
    bot2.handle_message("D1", "U1", "remember: something vague")
    assert len(manager.lessons) == n
    assert any("Which repo" in p.text for p in client.posts)


def test_retrospective_event_posts_learned_note(tmp_path):
    from forge.store import Store
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("retrospective", added=2),
        TE("done", diff_files=1, verify_ok=True, message="done")])
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert any("learned 2 things" in p.text for p in client.posts)


def test_stage_lines_complete_as_next_stage_starts(tmp_path):
    """Each stage's ⏳ flips to ✅ the moment the NEXT stage begins — the
    progress message tracks reality live instead of staying all-hourglass
    until the whole session finishes."""
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(start_events=[
        TE("phase", name="clone", label="Cloning"),
        TE("phase", name="recipe", label="Recipe: next"),
        TE("phase", name="up", label="Stack up"),
        TE("url", web_url="http://localhost:3001"),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    mid = [u.text for u in client.updates if "⏳ Stack up" in u.text]
    assert mid, "expected an update while Stack up was still active"
    assert all("✅ Cloned" in t and "✅ Recipe" in t for t in mid)
    assert "⏳" not in client.updates[-1].text     # done flips the last stage


def test_error_marks_active_stage_failed(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(start_events=[
        TE("phase", name="up", label="Stack up"),
        SimpleNamespace(kind="error",
                        data={"kind": "env", "detail": "compose failed"}),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    text = client.updates[-1].text
    assert "❌ Stack up" in text
    assert "compose failed" in text
    assert "⏳" not in text


def test_long_answer_posts_digest_with_full_snippet(tmp_path):
    """A long final answer goes out as a short digest plus the full text as a
    snippet — a wall of text is a worse teammate than an attachment."""
    from forge.store import Store
    long = "\n\n".join(f"Detail paragraph {i}: " + "y" * 300 for i in range(6))
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=0, verify_ok=None, message=long),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    snips = [u for u in client.uploads if u.content]
    assert len(snips) == 1 and snips[0].content == long
    digest = [p for p in client.posts if "Detail paragraph 0" in p.text]
    assert digest and len(digest[-1].text) < len(long)


def test_short_answer_posts_plain_with_no_snippet(tmp_path):
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=0, verify_ok=None, message="You're on 2.41.3."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    assert not [u for u in client.uploads if u.content]


def test_long_build_summary_digests_and_keeps_result_lines(tmp_path):
    """The build finish line stays glanceable: digest the summary but keep the
    link / files-changed / verify tail, and attach the full notes."""
    from forge.store import Store
    long = "\n\n".join(f"Change note {i}: " + "z" * 300 for i in range(6))
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("done", diff_files=2, verify_ok=True, message=long),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    done = [p for p in client.posts if "file(s) changed" in p.text]
    assert done and len(done[0].text) < len(long)
    assert any(u.content == long for u in client.uploads if u.content)


def test_qa_fast_path_long_answer_attaches_snippet(tmp_path):
    from forge.store import Store
    long = "\n\n".join(f"Answer part {i}: " + "w" * 300 for i in range(6))
    client = FakeClient()
    bot = _bot(Store(tmp_path / "f.db"), client=client,
               qa_answer=lambda slug, q: long)
    bot.handle_message("D1", "U1", "what node version does the landing repo use?")
    snips = [u for u in client.uploads if u.content]
    assert len(snips) == 1 and snips[0].content == long
    assert all(len(p.text) < len(long) for p in client.posts)


def test_live_ticker_finalizes_on_done(tmp_path):
    """The in-thread live ticker ("⏳ Agent working — …") must not end the turn
    frozen on an hourglass; done rewrites it with a completion mark."""
    from forge.store import Store
    client = FakeClient()
    manager = FakeManager(turn_events=[
        TE("phase", name="agent", label="Agent working"),
        TE("tool", target="src/app/page.tsx"),
        TE("done", diff_files=1, verify_ok=True, message="Done."),
    ])
    bot = _bot(Store(tmp_path / "f.db"), manager=manager, client=client)
    bot.handle_message("D1", "U1", "fix the landing page repo")
    live_ts = next(p.ts for p in client.posts if "It's up" in p.text)
    live_updates = [u for u in client.updates if u.ts == live_ts]
    assert live_updates, "expected the live ticker to be edited"
    assert "⏳" not in live_updates[-1].text
    assert "✅" in live_updates[-1].text


# --- Task 7: Slack image ingress ---

def _img(name="bug.png", size=100, mimetype="image/png"):
    return {"name": name, "mimetype": mimetype, "size": size,
            "url_private_download": f"https://files.slack/{name}"}


def _running_thread_bot(tmp_path, manager=None, client=None):
    """A bot with a run already linked to thread '111' and marked running, so
    a message with thread_ts='111' flows through _thread_command_or_turn ->
    _follow_up (the follow-up-turn attachment path)."""
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    store.create_run("run-1", "acme/landing-page", "", "forge/x")
    store.set_state("run-1", "running")
    store.link_slack_thread("111", "D1", "run-1", "111")
    manager = manager or FakeManager()
    return _bot(store, manager=manager, client=client or FakeClient()), manager


def test_followup_with_image_reaches_turn_attachments(tmp_path):
    bot, mgr = _running_thread_bot(tmp_path)
    bot._download = lambda url: ("image/png", b"\x89PNG")
    bot.handle_message("D1", "U1", "fix this", thread_ts="111", files=[_img()])
    turn = next(c for c in mgr.calls if c[0] == "turn")
    assert turn[3] == ["1-bug.png"]


def test_new_session_with_image_reaches_plan_task(tmp_path):
    from forge.store import Store
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager)
    bot._download = lambda url: ("image/png", b"\x89PNG")
    bot.handle_message("D1", "U1", "build the header like this",
                       files=[_img("design.png")])
    plan = manager.plan_calls[0]
    assert plan[3] == ["1-design.png"]


def test_non_image_files_skipped_with_note(tmp_path):
    client = FakeClient()
    bot, mgr = _running_thread_bot(tmp_path, client=client)
    bot.handle_message("D1", "U1", "fix this", thread_ts="111",
                       files=[_img("notes.txt", mimetype="text/plain")])
    turn = next(c for c in mgr.calls if c[0] == "turn")
    assert turn[3] == []
    assert any("non-image" in p.text for p in client.posts)


def test_oversize_image_skipped_with_note(tmp_path):
    client = FakeClient()
    bot, mgr = _running_thread_bot(tmp_path, client=client)
    bot.handle_message("D1", "U1", "fix", thread_ts="111",
                       files=[_img(size=inbox.MAX_BYTES + 1)])
    assert any("10 MB" in p.text for p in client.posts)


def test_missing_scope_html_response_notes_files_read(tmp_path):
    client = FakeClient()
    bot, mgr = _running_thread_bot(tmp_path, client=client)
    bot._download = lambda url: ("text/html; charset=utf-8", b"<html>login</html>")
    bot.handle_message("D1", "U1", "fix", thread_ts="111", files=[_img()])
    assert any("files:read" in p.text for p in client.posts)


def test_download_failure_proceeds_text_only(tmp_path):
    bot, mgr = _running_thread_bot(tmp_path)
    def boom(url):
        raise OSError("net down")
    bot._download = boom
    bot.handle_message("D1", "U1", "fix", thread_ts="111", files=[_img()])
    turn = next(c for c in mgr.calls if c[0] == "turn")   # turn still ran
    assert turn[3] == []


def test_max_five_images(tmp_path):
    bot, mgr = _running_thread_bot(tmp_path)
    bot._download = lambda url: ("image/png", b"\x89PNG")
    bot.handle_message("D1", "U1", "fix", thread_ts="111",
                       files=[_img(f"s{i}.png") for i in range(7)])
    turn = next(c for c in mgr.calls if c[0] == "turn")
    assert len(turn[3]) == 5


def test_channel_file_share_with_mention_not_double_handled(tmp_path):
    # A channel `@forge …` + image fires BOTH app_mention AND message
    # (subtype=file_share). The file_share copy carries the mention text, so
    # route_channel_message's mention check must drop it — only app_mention
    # drives a turn. (bot_user_id defaults to "UBOT" in the _bot fixture.)
    from forge.store import Store
    manager = FakeManager()
    bot = _bot(Store(tmp_path / "f.db"), manager=manager)
    bot.route_channel_message("C1", "U1", "<@UBOT> fix it", "111", files=[_img()])
    assert manager.calls == [] and manager.plan_calls == []
