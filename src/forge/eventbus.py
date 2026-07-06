"""In-process per-run event bus: the cross-surface live-update spine.

Every SessionManager flow publishes each TurnEvent here (stamped with a
per-run monotonic `seq` and the driving surface's `origin`) regardless of
which surface consumes the generator. Subscribers:

  - the web app's GET /api/sessions/{id}/events SSE feed (subscribe/replay),
  - the Slack mirror (tap), which re-renders foreign-origin turns into the
    run's linked thread.

One process only — `forge web --slack` shares a single SessionManager, so a
single bus reaches both surfaces. Publishing never blocks and never raises
into the engine: subscriber queues drop when full (clients recover via
`replay(since=...)`), and taps run on a dedicated dispatcher thread.
"""
import functools
import logging
import queue
import threading
from collections import deque

logger = logging.getLogger(__name__)


def published(fn):
    """Wrap a SessionManager generator flow (first arg run_id) so every yielded
    TurnEvent is also published to self.bus. Adds an `origin` kwarg the driving
    surface sets ("web" | "slack" | "queue"; default "api"): the bus stamps it
    so the mirror/feed can tell foreign turns from their own. Publish failures
    are swallowed — rendering must never abort an engine turn.

    When the flow finishes (return, error, or abandoned generator) a synthetic
    `stream_end` event goes to the BUS ONLY (never yielded to the driving
    surface): several flows end without a terminal `done` (wake stops at `url`,
    plan_task at `checkpoint`), and a passive follower needs a deterministic
    signal that the stream is over to release its live UI state."""
    from forge.events import TurnEvent

    @functools.wraps(fn)
    def wrapper(self, run_id, *args, origin="api", **kwargs):
        try:
            for ev in fn(self, run_id, *args, **kwargs):
                try:
                    self.bus.publish(run_id, ev, origin=origin)
                except Exception:
                    logger.exception("eventbus publish failed (run %s)", run_id)
                yield ev
        finally:
            try:
                self.bus.publish(run_id, TurnEvent("stream_end", {}), origin=origin)
            except Exception:
                logger.exception("eventbus publish failed (run %s)", run_id)
    return wrapper

_DEFAULT_BUFFER = 500
_DEFAULT_QUEUE = 1000


class Subscription:
    """A per-run live feed. `get()` returns the next stamped event dict or
    None on timeout; `close()` detaches it from the bus."""

    def __init__(self, bus, run_id, queue_size):
        self._bus = bus
        self.run_id = run_id
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self.closed = False

    def get(self, timeout=None):
        try:
            return self._q.get(timeout=timeout) if timeout else self._q.get_nowait()
        except queue.Empty:
            return None

    def _offer(self, stamped) -> None:
        try:
            self._q.put_nowait(stamped)
        except queue.Full:
            # A stalled client loses live events but recovers on reconnect via
            # replay(since=last_seen_seq); blocking the publisher is never ok.
            pass

    def close(self) -> None:
        self.closed = True
        self._bus._unsubscribe(self)
        # Wake a get() blocked on an empty queue so its consumer re-checks
        # `closed` now instead of after the full heartbeat timeout.
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass                 # a full queue means the getter isn't blocked


class EventBus:
    def __init__(self, buffer=_DEFAULT_BUFFER, queue_size=_DEFAULT_QUEUE):
        self._buffer = buffer
        self._queue_size = queue_size
        self._mu = threading.Lock()
        self._events: dict = {}          # run_id -> deque of stamped dicts
        self._seq: dict = {}             # run_id -> last seq
        self._subs: dict = {}            # run_id -> [Subscription, ...]
        self._taps: list = []
        # Taps (Slack mirror) do network I/O; dispatch them off-thread so a
        # slow chat.update never stalls the engine thread that published.
        self._tap_q: queue.Queue = queue.Queue()
        self._dispatcher = None

    # --- publish side ---

    def publish(self, run_id, ev, origin="api") -> dict:
        """Stamp and fan out one TurnEvent. Returns the stamped dict."""
        with self._mu:
            seq = self._seq.get(run_id, 0) + 1
            self._seq[run_id] = seq
            stamped = {"seq": seq, "origin": origin,
                       "kind": ev.kind, "data": ev.data}
            buf = self._events.setdefault(run_id, deque(maxlen=self._buffer))
            buf.append(stamped)
            subs = list(self._subs.get(run_id, ()))
            has_taps = bool(self._taps)
        for sub in subs:
            if not sub.closed:
                sub._offer(stamped)
        if has_taps:
            self._tap_q.put((run_id, stamped))
            self._ensure_dispatcher()
        return stamped

    # --- consume side ---

    def replay(self, run_id, since=0) -> list:
        with self._mu:
            return [e for e in self._events.get(run_id, ())
                    if e["seq"] > since]

    def last_seq(self, run_id) -> int:
        with self._mu:
            return self._seq.get(run_id, 0)

    def subscribe(self, run_id) -> Subscription:
        sub = Subscription(self, run_id, self._queue_size)
        with self._mu:
            self._subs.setdefault(run_id, []).append(sub)
        return sub

    def close_all(self) -> None:
        """Close every live subscription (server shutdown): wakes each blocked
        SSE tail so its stream ends and uvicorn's connection wait can finish.
        The bus stays usable — publish/subscribe still work afterwards."""
        with self._mu:
            subs = [s for lst in self._subs.values() for s in lst]
        for sub in subs:
            sub.close()

    def _unsubscribe(self, sub) -> None:
        with self._mu:
            subs = self._subs.get(sub.run_id, [])
            if sub in subs:
                subs.remove(sub)
            if not subs:
                self._subs.pop(sub.run_id, None)

    def tap(self, fn) -> None:
        """Register a global observer fn(run_id, stamped) for every run's
        events, called on the dispatcher thread. Errors are logged, never
        raised into publish()."""
        with self._mu:
            self._taps.append(fn)

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        with self._mu:
            if self._dispatcher and self._dispatcher.is_alive():
                return
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop, name="forge-eventbus", daemon=True)
            self._dispatcher.start()

    def _dispatch_loop(self) -> None:
        while True:
            run_id, stamped = self._tap_q.get()
            with self._mu:
                taps = list(self._taps)
            for fn in taps:
                try:
                    fn(run_id, stamped)
                except Exception:
                    logger.exception("eventbus tap failed (run %s)", run_id)
