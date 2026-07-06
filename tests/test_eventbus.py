import threading
import time

from forge.eventbus import EventBus
from forge.events import TurnEvent


def ev(kind="narration", **data):
    return TurnEvent(kind, data)


def test_publish_stamps_monotonic_seq_per_run():
    bus = EventBus()
    bus.publish("r1", ev(text="a"), origin="web")
    bus.publish("r1", ev(text="b"), origin="slack")
    bus.publish("r2", ev(text="c"), origin="web")
    r1 = bus.replay("r1")
    assert [e["seq"] for e in r1] == [1, 2]
    assert r1[0]["origin"] == "web" and r1[1]["origin"] == "slack"
    assert r1[0]["kind"] == "narration" and r1[0]["data"] == {"text": "a"}
    assert [e["seq"] for e in bus.replay("r2")] == [1]


def test_replay_since_filters_older_events():
    bus = EventBus()
    for i in range(5):
        bus.publish("r", ev(text=str(i)), origin="web")
    tail = bus.replay("r", since=3)
    assert [e["seq"] for e in tail] == [4, 5]
    assert bus.replay("r", since=99) == []


def test_ring_buffer_is_bounded():
    bus = EventBus(buffer=3)
    for i in range(10):
        bus.publish("r", ev(text=str(i)), origin="web")
    kept = bus.replay("r")
    assert [e["seq"] for e in kept] == [8, 9, 10]   # seq keeps counting


def test_subscribe_receives_live_events():
    bus = EventBus()
    sub = bus.subscribe("r")
    bus.publish("r", ev(text="hi"), origin="web")
    got = sub.get(timeout=1)
    assert got["seq"] == 1 and got["data"] == {"text": "hi"}
    assert sub.get(timeout=0.01) is None            # empty -> None, no raise
    sub.close()


def test_subscribe_is_per_run():
    bus = EventBus()
    sub = bus.subscribe("r1")
    bus.publish("r2", ev(text="other"), origin="web")
    assert sub.get(timeout=0.01) is None
    sub.close()


def test_closed_subscription_stops_receiving():
    bus = EventBus()
    sub = bus.subscribe("r")
    sub.close()
    assert sub.closed
    bus.publish("r", ev(text="x"), origin="web")
    assert sub.get(timeout=0.01) is None


def test_full_subscriber_queue_drops_instead_of_blocking():
    bus = EventBus(queue_size=2)
    sub = bus.subscribe("r")
    for i in range(5):
        bus.publish("r", ev(text=str(i)), origin="web")   # must not block
    seqs = []
    while (e := sub.get(timeout=0.01)) is not None:
        seqs.append(e["seq"])
    assert seqs == [1, 2]      # rest dropped; client recovers via replay(since)
    sub.close()


def test_tap_receives_all_runs_on_dispatcher_thread():
    bus = EventBus()
    got, main = [], threading.get_ident()
    done = threading.Event()

    def tap(run_id, stamped):
        got.append((run_id, stamped["seq"], threading.get_ident()))
        if len(got) == 2:
            done.set()

    bus.tap(tap)
    bus.publish("a", ev(text="1"), origin="web")
    bus.publish("b", ev(text="2"), origin="slack")
    assert done.wait(2)
    assert {(r, s) for r, s, _ in got} == {("a", 1), ("b", 1)}
    assert all(t != main for _, _, t in got)    # never on the publishing thread


def test_tap_errors_are_swallowed():
    bus = EventBus()
    seen = threading.Event()
    bus.tap(lambda rid, e: (_ for _ in ()).throw(RuntimeError("boom")))
    bus.tap(lambda rid, e: seen.set())
    bus.publish("r", ev(text="x"), origin="web")
    assert seen.wait(2)      # second tap still ran; publisher never raised


def test_close_wakes_a_blocked_get_immediately():
    bus = EventBus()
    sub = bus.subscribe("r")
    woke = threading.Event()

    def wait():
        sub.get(timeout=10)      # nothing published: blocks until woken
        woke.set()

    threading.Thread(target=wait, daemon=True).start()
    time.sleep(0.05)             # let the getter block
    start = time.monotonic()
    sub.close()
    assert woke.wait(2)          # woken by close, not the 10s timeout
    assert time.monotonic() - start < 2


def test_close_all_closes_every_subscription_across_runs():
    bus = EventBus()
    subs = [bus.subscribe("r1"), bus.subscribe("r1"), bus.subscribe("r2")]
    bus.close_all()
    assert all(s.closed for s in subs)
    bus.publish("r1", ev(text="x"), origin="web")   # publish after close is safe
    late = bus.subscribe("r1")                      # bus still usable afterwards
    bus.publish("r1", ev(text="y"), origin="web")
    assert late.get(timeout=1)["data"] == {"text": "y"}
    late.close()


def test_concurrent_publish_keeps_seq_unique():
    bus = EventBus()

    def pump():
        for _ in range(50):
            bus.publish("r", ev(text="x"), origin="web")

    threads = [threading.Thread(target=pump) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    # buffer default (500) holds all 200
    seqs = [e["seq"] for e in bus.replay("r")]
    assert len(seqs) == 200 and len(set(seqs)) == 200 and seqs == sorted(seqs)
