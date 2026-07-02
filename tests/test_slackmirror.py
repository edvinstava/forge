"""The Slack mirror: turns driven from the web app (or CLI) render live into
the run's linked Slack thread via the event bus tap, and checkpoint asks link
to the web session — the cross-surface half of the interop design."""
from pathlib import Path
from types import SimpleNamespace

from forge.reporesolve import Resolution
from forge.slackbot import ForgeSlackBot

from test_slackbot import FakeClient, FakeManager, FakeResolver, FakeTunnel


def _bot(tmp_path, web_url="http://127.0.0.1:8099", manager=None):
    from forge.store import Store
    store = Store(tmp_path / "f.db")
    cfg = SimpleNamespace(slack_allowed_user="U1", forge_web_url=web_url)
    client = FakeClient()
    manager = manager or FakeManager()
    bot = ForgeSlackBot(manager, store, cfg,
                        FakeResolver(Resolution("o/r", "high", ["o/r"])),
                        FakeTunnel(), client, run_id_factory=lambda: "run-1",
                        bot_user_id="UBOT")
    return bot, store, client


def _link(store, run_id="run-1", thread_ts="100", channel="C1"):
    store.link_slack_thread(thread_ts, channel, run_id, anchor_ts="101")


def stamped(kind, data=None, origin="web", seq=1):
    return {"seq": seq, "origin": origin, "kind": kind, "data": data or {}}


# --- foreign-turn mirroring ---

def test_foreign_turn_opens_anchor_and_streams(tmp_path):
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped("phase", {"name": "agent",
                                                 "label": "Agent working"}))
    bot._mirror_event("run-1", stamped("tool", {"name": "Bash",
                                                "target": "npm test"}, seq=2))
    texts = [p.text for p in client.posts]
    assert any("driving from the web app" in t for t in texts)
    # the agent phase opened the in-thread live reply; the tool event edits it
    assert any("Agent working — npm test" in u.text for u in client.updates)
    # everything lands in the linked thread
    assert all(p.thread_ts == "100" for p in client.posts if p.thread_ts)


def test_slack_and_queue_origins_are_not_mirrored(tmp_path):
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped("phase", {"name": "agent"}, origin="slack"))
    bot._mirror_event("run-1", stamped("phase", {"name": "agent"}, origin="queue"))
    assert client.posts == [] and client.updates == []


def test_runs_without_linked_thread_are_ignored(tmp_path):
    bot, store, client = _bot(tmp_path)
    bot._mirror_event("run-1", stamped("phase", {"name": "agent"}))
    assert client.posts == []


def test_preflight_busy_error_does_not_anchor(tmp_path):
    # A web user clicking Send during a Slack-driven turn triggers a `busy`
    # rejection — noise, not a turn; the thread must stay quiet.
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped("error", {"kind": "busy",
                                                 "detail": "a turn is in flight"}))
    assert client.posts == []


def test_checkpoint_answered_posts_note_without_anchor(tmp_path):
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped(
        "checkpoint_answered", {"id": 3, "action": "approve", "body": None}))
    assert len(client.posts) == 1
    assert "answered from the web app: approve" in client.posts[0].text
    assert "run-1" not in bot._mirror_state


def test_mirrored_checkpoint_ask_links_web_session(tmp_path):
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped("phase", {"name": "planning",
                                                 "label": "Planning"}))
    bot._mirror_event("run-1", stamped(
        "checkpoint", {"id": 1, "type": "plan_approval",
                       "prompt": "Approve this plan to proceed."}, seq=2))
    ask = [p.text for p in client.posts if "Approve this plan" in p.text]
    assert ask and "http://127.0.0.1:8099/#s=run-1" in ask[0]
    # gate reached -> turn is over; the next foreign turn re-anchors
    assert "run-1" not in bot._mirror_state


def test_done_finishes_and_evicts_state(tmp_path):
    bot, store, client = _bot(tmp_path)
    store.create_run("run-1", "o/r", "t", "b")
    _link(store)
    bot._mirror_event("run-1", stamped("phase", {"name": "agent",
                                                 "label": "Agent working"}))
    bot._mirror_event("run-1", stamped(
        "done", {"message": "All done.", "diff_files": 2, "verify_ok": True}, seq=2))
    assert "run-1" not in bot._mirror_state
    finish = [p.text for p in client.posts if "file(s) changed" in p.text]
    assert finish and "tests pass" in finish[0]


def test_mirror_render_failure_never_raises(tmp_path):
    bot, store, client = _bot(tmp_path)
    _link(store)

    def boom(**kwargs):
        raise RuntimeError("slack down")
    client.chat_postMessage = boom
    bot._on_bus_event("run-1", stamped("phase", {"name": "agent"}))   # must not raise


# --- web-session links on Slack-driven turns ---

def test_checkpoint_ask_includes_web_link(tmp_path):
    bot, store, client = _bot(tmp_path)
    state = {"head": "h", "lines": [], "done": False, "summary": None,
             "announce_live": False, "mode": "build", "thread_ts": "100"}
    from types import SimpleNamespace as NS
    bot._apply("run-1", "C1", "101", state,
               NS(kind="checkpoint",
                  data={"id": 1, "type": "plan_approval", "prompt": "Approve?"}))
    ask = client.posts[-1].text
    assert "🧭 or answer on the web: http://127.0.0.1:8099/#s=run-1" in ask


def test_checkpoint_ask_without_web_url_has_no_link(tmp_path):
    bot, store, client = _bot(tmp_path, web_url="")
    state = {"head": "h", "lines": [], "done": False, "summary": None,
             "announce_live": False, "mode": "build", "thread_ts": "100"}
    from types import SimpleNamespace as NS
    bot._apply("run-1", "C1", "101", state,
               NS(kind="checkpoint",
                  data={"id": 1, "type": "plan_approval", "prompt": "Approve?"}))
    assert "🧭" not in client.posts[-1].text


def test_anchor_url_lines_include_forge_session_link(tmp_path):
    bot, _, _ = _bot(tmp_path)
    lines = bot._url_lines({"public_url": "https://x.trycloudflare.com",
                            "forge_url": "http://127.0.0.1:8099/#s=run-1"})
    assert lines == ["🌐 https://x.trycloudflare.com",
                     "🧭 http://127.0.0.1:8099/#s=run-1 (session in forge web)"]


def test_new_session_anchor_renders_forge_link(tmp_path):
    bot, store, client = _bot(tmp_path)
    bot.handle_message("D1", "U1", "fix the login bug in o/r")
    anchor_edits = [u.text for u in client.updates]
    assert any("🧭 http://127.0.0.1:8099/#s=run-1" in t for t in anchor_edits)


def test_web_session_link_helper():
    from forge.slackmsg import web_session_link
    assert web_session_link("http://h:1/", "abc") == "http://h:1/#s=abc"
    assert web_session_link("", "abc") == ""


def test_attach_bus_mirrors_published_events_end_to_end(tmp_path):
    """Full path: a web-origin flow publishes to a real EventBus; the tap
    dispatches on the bus thread; the linked thread receives the render."""
    import time
    from forge.eventbus import EventBus
    from forge.events import TurnEvent

    bot, store, client = _bot(tmp_path)
    _link(store)
    bus = EventBus()
    bot.attach_bus(bus)
    bus.publish("run-1", TurnEvent("phase", {"name": "agent",
                                             "label": "Agent working"}),
                origin="web")
    bus.publish("run-1", TurnEvent("narration", {"text": "streaming"}),
                origin="web")
    deadline = time.time() + 2
    while time.time() < deadline:
        if any("driving from the web app" in p.text for p in client.posts):
            break
        time.sleep(0.02)
    assert any("driving from the web app" in p.text for p in client.posts)


def test_stream_end_evicts_mirror_state_without_posting(tmp_path):
    # wake ends at `url` (no done/checkpoint) — the bus-only stream_end must
    # release the mirror state silently so the next turn re-anchors cleanly.
    bot, store, client = _bot(tmp_path)
    _link(store)
    bot._mirror_event("run-1", stamped("phase", {"name": "wake",
                                                 "label": "Waking"}))
    posts_before = len(client.posts)
    bot._mirror_event("run-1", stamped("stream_end", {}, seq=2))
    assert "run-1" not in bot._mirror_state
    assert len(client.posts) == posts_before
