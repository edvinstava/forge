"""Slack Socket Mode ingress. Translates DM events into SessionManager calls
and TurnEvents into one live-edited progress message. Holds no engine logic.
The Socket Mode wiring (run()) is added in a later task; this class is the
testable core, driven with an injected Slack client + manager."""
import logging
import threading
import uuid
from urllib.parse import urlparse

from forge.events import TurnEvent
from forge.slackmsg import (greeting_head, qa_head, clean_summary, deep_link,
                            classify_intent, help_blurb, strip_mentions,
                            unwrap_links, concise_verify_reason, digest_for_slack,
                            format_transcript, narration_line, remember_text,
                            truncate_for_slack, web_session_link,
                            web_workspace_link,
                            SLACK_BLOCK_TEXT_LIMIT, SLACK_DIGEST_LIMIT)
from forge.slackroute import Route
from forge.slackbatch import parse_batch_lines
from forge import flow
from forge import inbox

logger = logging.getLogger(__name__)

# How many recent thread/DM messages to feed the chat model for context.
_CHAT_HISTORY_MAX = 12

_PHASE_EMOJI = {"clone": "Cloned", "recipe": "Recipe", "up": "Stack up",
                "agent": "Agent working", "wake": "Waking",
                "noweb": "No web service"}


def _localhost_target(web_url):
    """http://localhost:3001 -> same (pass through to cloudflared --url)."""
    p = urlparse(web_url)
    return f"http://localhost:{p.port}" if p.port else web_url


class ForgeSlackBot:
    def __init__(self, manager, store, cfg, resolver, tunnel, client,
                 run_id_factory=None, qa_answer=None, opener=None,
                 bot_user_id=None, chat_reply=None, chat_router=None):
        self.manager, self.store, self.cfg = manager, store, cfg
        self.resolver, self.tunnel, self.client = resolver, tunnel, client
        self.bot_user_id = bot_user_id  # for mention detection in channels
        self._new_run_id = run_id_factory or (lambda: uuid.uuid4().hex)
        self._qa_answer = qa_answer or (lambda slug, q: "")
        # (slug, task, mode, fallback) -> opening line. Default: just the
        # template fallback; the real LLM opener is wired in build_app.
        self._opener = opener or (lambda slug, task, mode, fallback: fallback)
        # (transcript, latest) -> conversational reply. Default: the canned
        # blurb; the real LLM chat reply is wired in build_app.
        self._chat_reply = chat_reply or (lambda transcript, latest: help_blurb())
        # (transcript, latest) -> Route. Disambiguates the regex "chat" bucket:
        # a non-English build request lands here and is routed to a real build
        # instead of an empty promise. Default keeps today's behavior (always
        # chat); the real LLM router is wired in build_app.
        self._chat_router = chat_router or (lambda transcript, latest: Route("chat", None))
        # Pending picks / QA threads are keyed by a *conversation key*: the
        # channel in a DM, the thread root in a channel (see handle_message).
        self._pending: dict = {}        # conv-key -> [candidate slugs]
        self._pending_text: dict = {}   # conv-key -> original phrase
        self._qa_threads: dict = {}     # thread_ts -> slug (clone-only Q&A)
        self._chat_threads: set = set()  # channel roots forge is chatting in
        self._notified_gate: set = set()  # (channel, user) already told "not you"
        # Per-thread turn serialization: the engine refuses concurrent turns, so
        # the bot queues mid-turn follow-ups instead of dropping them as "busy".
        self._mu = threading.Lock()
        self._running: set = set()      # thread_key currently being driven
        self._queues: dict = {}         # thread_key -> [job, ...] (FIFO)
        # Mirror of turns driven from OTHER surfaces (web/CLI) into linked
        # threads: run_id -> {channel, anchor_ts, state} (see attach_bus).
        self._mirror_state: dict = {}

    # --- channel entry points ---

    def handle_mention(self, channel, user, text, root_ts, files=None):
        """An `@forge …` in a channel. The mention root (an existing thread root
        or the mention's own ts) becomes the conversation root; delegate to the
        shared brain with channel threading."""
        self.handle_message(channel, user, text, thread_ts=root_ts, root_ts=root_ts,
                            files=files)

    def route_channel_message(self, channel, user, text, thread_ts, files=None):
        """A non-DM `message` event. Mentions are handled by handle_mention
        (dedup — a top-level @forge fires both events), and only forge-owned
        threads continue here. Ambient channel chatter is ignored."""
        t = text or ""
        if self.bot_user_id and (f"<@{self.bot_user_id}>" in t
                                 or f"<@{self.bot_user_id}|" in t):
            return
        if not thread_ts or not self._owns_thread(thread_ts):
            return
        self.handle_message(channel, user, text,
                            thread_ts=thread_ts, root_ts=thread_ts, files=files)

    def _owns_thread(self, thread_ts) -> bool:
        return bool(self.store.run_for_thread(thread_ts)
                    or self._qa_threads.get(thread_ts)
                    or thread_ts in self._pending
                    or thread_ts in self._chat_threads)

    def _gate_notice(self, channel, user):
        """Tell a non-allowed user, once per (channel, user), that forge only
        takes instructions from the allowed user. Visible (the whole channel
        learns forge is gated) and best-effort."""
        key = (channel, user)
        if key in self._notified_gate:
            return
        self._notified_gate.add(key)
        allowed = self.cfg.slack_allowed_user
        try:
            self.client.chat_postMessage(
                channel=channel,
                text=f"👋 I only take instructions from <@{allowed}> right now — "
                     f"ask them to drive me!")
        except Exception:
            pass

    # --- entry point ---

    def handle_message(self, channel, user, text, thread_ts=None, root_ts=None,
                       files=None):
        # root_ts is None in a DM (forge's own first message becomes the thread
        # root) and the mention/thread root in a channel.
        if user != self.cfg.slack_allowed_user:
            if root_ts is not None:        # channel: tell them (deduped)
                self._gate_notice(channel, user)
            return                          # DM from others stays silent
        text = unwrap_links(strip_mentions(text))
        if thread_ts:
            run_id = self.store.run_for_thread(thread_ts)
            if run_id:
                cp = self.store.open_checkpoint(run_id)
                if cp:
                    return self._submit(channel, thread_ts,
                                        lambda: self._answer_checkpoint(channel, thread_ts,
                                                                        run_id, cp["id"], text))
                return self._thread_command_or_turn(channel, thread_ts, run_id, text,
                                                    files=files)
            slug = self._qa_threads.get(thread_ts)
            if slug:
                return self._qa(channel, thread_ts, slug, text)
        # A reply made inside a thread continues in that thread — even in a DM,
        # where the event carries thread_ts but the caller left root_ts unset.
        # (A brand-new top-level message has neither, so this is a no-op there.)
        root_ts = root_ts or thread_ts
        key = root_ts or channel
        picked = self._resolve_pending_pick(key, text)
        if picked:
            return self._start_resolved(
                channel, picked, self._pending_text.pop(key, text), root_ts)
        # A bulleted/numbered list is a fire-and-forget batch. Detect it BEFORE
        # intent classification (a numbered list otherwise classifies as "chat").
        batch = parse_batch_lines(text)
        if batch:
            return self._start_batch(channel, text, batch, root_ts)
        intent = classify_intent(text)
        if intent == "remember":
            return self._remember_fresh(channel, text, root_ts)
        if intent == "chat":
            return self._chat_or_route(channel, text, root_ts)
        if intent in ("sleep", "wake", "status"):
            self.client.chat_postMessage(
                channel=channel, thread_ts=root_ts,
                text="No session going here yet — name a repo + task and I'll start one.")
            return
        if intent == "review":
            return self._run_review(channel, text)
        if intent == "qa":
            return self._qa_or_route(channel, text, root_ts)
        self._new_session(channel, text, root_ts, files=files)

    def _thread_command_or_turn(self, channel, thread_ts, run_id, text, files=None):
        intent = classify_intent(text)
        repo = (self.store.get_run(run_id) or {}).get("repo", "it")
        if intent == "stop":
            # Emergency brake: cancel the in-flight turn NOW (not queued).
            self.manager.stop(run_id)
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                         text=f"🛑 stopped `{repo}`.")
            return
        if intent == "remember":
            ok = self.manager.remember_lesson(repo, remember_text(text))
            txt = (f"🧠 noted — I'll remember that for `{repo}`." if ok
                   else "I couldn't save that — try `remember: <the lesson>`.")
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=txt)
            return
        if intent == "forget_creds":
            ok = self.manager.forget_credentials(repo)
            txt = (f"🗑️ forgot the saved login for `{repo}`." if ok
                   else f"No saved login for `{repo}` to forget.")
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=txt)
            return
        if intent == "sleep":
            status = self.manager.request_sleep(run_id, reason="slack")
            txt = ("💤 will pause after this step." if status == "deferred"
                   else f"💤 sleeping `{repo}` — reply here to wake it.")
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=txt)
            return
        if intent == "status":
            return self._post_status(channel, thread_ts, run_id)
        if intent == "wake":
            return self._follow_up(channel, thread_ts, text, run_turn=False)
        # A question/chat reply is an answer turn; anything else is a build turn.
        mode = "qa" if intent in ("qa", "chat") else "build"
        return self._submit(channel, thread_ts,
                            lambda: self._follow_up(channel, thread_ts, text, mode=mode,
                                                    files=files))

    def _answer_checkpoint(self, channel, thread_ts, run_id, cid, text):
        low = text.strip().lower()
        if low in ("approve", "yes", "lgtm", "go", "ship it"):
            action, body = "approve", None
        elif low in ("reject", "no", "cancel", "stop"):
            action, body = "reject", None
        else:
            action, body = "edit", text
        # Resume in a fresh in-thread reply (like _follow_up): the original anchor
        # keeps the plan it displayed; replan/execution progress streams here. The
        # state must be fully shaped — _apply/_render/_finish read
        # head/lines/thread_ts/mode, so a bare {"lines": []} KeyError'd on the
        # first `agent` phase (thread_ts) and again in _render (head).
        ack = self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                           text="🔧 on it…")
        anchor_ts = ack["ts"]
        state = {"head": "🔧 on it…", "lines": [], "done": False,
                 "summary": None, "announce_live": False, "mode": "build",
                 "thread_ts": thread_ts, "forge_url": self._session_link(run_id)}
        self._drive(run_id, channel, anchor_ts, state,
                    self.manager.respond_checkpoint(run_id, cid, action, body,
                                                    origin="slack"))

    def _post_status(self, channel, thread_ts, run_id):
        run = self.store.get_run(run_id) or {}
        env = self.store.get_env(run_id) or {}
        url = env.get("web_url") or "—"
        self.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"`{run.get('repo', '?')}` · {run.get('state', '?')} · {url}")

    def _qa_fresh(self, channel, text, root_ts=None):
        res = self.resolver.resolve(text)
        if res.confidence != "high":
            # Can't pin it to a repo — it may be about forge itself or just
            # chatter, so converse (forge answers or asks) instead of a terse
            # "Which repo?". The build path keeps the deterministic ask.
            return self._chat(channel, text, root_ts)
        head = self._opener(res.slug, text, "qa", qa_head(res.slug))
        root = self.client.chat_postMessage(channel=channel, text=head,
                                            thread_ts=root_ts)
        qa_key = root_ts or root["ts"]
        self._qa_threads[qa_key] = res.slug
        self._post_digest(channel, qa_key, self._qa_answer(res.slug, text))

    def _qa(self, channel, thread_ts, slug, text):
        self._post_digest(channel, thread_ts, self._qa_answer(slug, text))

    def _remember_fresh(self, channel, text, root_ts=None):
        """A top-level `remember …: <lesson>` (outside any session thread).
        The repo must be inferable from the message itself ('remember for
        webapp: use bun'); otherwise ask for it rather than guessing —
        a lesson filed on the wrong repo silently misleads future runs."""
        res = self.resolver.resolve(text)
        if res.confidence != "high":
            self.client.chat_postMessage(
                channel=channel, thread_ts=root_ts,
                text="Which repo is that for? Say e.g. "
                     "`remember for owner/repo: <the lesson>`.")
            return
        ok = self.manager.remember_lesson(res.slug, remember_text(text))
        txt = (f"🧠 noted for `{res.slug}` — I'll apply it on future runs."
               if ok else "I couldn't save that — try `remember: <the lesson>`.")
        self.client.chat_postMessage(channel=channel, thread_ts=root_ts, text=txt)

    def _chat_or_route(self, channel, text, root_ts=None):
        """The regex classifier fell through to "chat". Ask the conversational
        brain what the teammate actually wants: a non-English build request
        ("lag en … side") routes to a real session instead of getting a friendly
        but empty reply; a repo question goes to the QA path; genuine chat posts
        the brain's reply. On router failure it degrades to a plain chat reply."""
        transcript = self._chat_history(channel, root_ts, text)
        route = self._chat_router(transcript, text)
        if route.action == "build":
            return self._new_session(channel, text, root_ts)
        if route.action == "qa":
            return self._qa_fresh(channel, text, root_ts)
        reply = route.reply or self._chat_reply(transcript, text)
        self.client.chat_postMessage(channel=channel, thread_ts=root_ts, text=reply)
        if root_ts is not None:
            self._chat_threads.add(root_ts)

    def _qa_or_route(self, channel, text, root_ts=None):
        """The regex saw a question, but a build request phrased politely as one
        ("kan du lage en side?") ends in "?" too. Let the brain decide: a build
        starts a session; otherwise we answer it as a question (which itself
        falls back to plain chat when no repo can be pinned). The default router
        keeps today's behavior — straight to the QA fast path."""
        route = self._chat_router(self._chat_history(channel, root_ts, text), text)
        if route.action == "build":
            return self._new_session(channel, text, root_ts)
        return self._qa_fresh(channel, text, root_ts)

    def _chat(self, channel, text, root_ts=None):
        """Reply as forge itself (vs. about a repo), via the LLM, with prior
        thread/DM context. Used when we've already decided this is plain chat
        (e.g. a question we couldn't pin to a repo) so it never re-routes. In a
        channel we record the thread root so follow-up replies (which re-route
        through handle_message) keep the conversation — a build-verb reply there
        naturally hands off to a session."""
        transcript = self._chat_history(channel, root_ts, text)
        reply = self._chat_reply(transcript, text)
        self.client.chat_postMessage(channel=channel, thread_ts=root_ts, text=reply)
        if root_ts is not None:
            self._chat_threads.add(root_ts)

    def _chat_history(self, channel, root_ts, latest):
        """Recent messages -> a `User:`/`forge:` transcript for context. Reads
        the thread in a channel, the whole DM otherwise. Best-effort: any failure
        (missing history scope, API error) yields '' and chat stays single-turn."""
        try:
            if root_ts:
                resp = self.client.conversations_replies(
                    channel=channel, ts=root_ts, limit=_CHAT_HISTORY_MAX)
                msgs = list((resp or {}).get("messages") or [])
            else:
                resp = self.client.conversations_history(
                    channel=channel, limit=_CHAT_HISTORY_MAX)
                msgs = list(reversed((resp or {}).get("messages") or []))
            transcript = format_transcript(msgs[-_CHAT_HISTORY_MAX:], self.bot_user_id)
            # The triggering message is usually already in history; drop it from
            # the tail since we hand `latest` to the prompt separately.
            tail = f"User: {strip_mentions(latest)}"
            if transcript.endswith(tail):
                transcript = transcript[: -len(tail)].rstrip("\n")
            return transcript
        except Exception:
            return ""

    def _resolve_pending_pick(self, key, text):
        cands = self._pending.get(key)
        if not cands:
            return None
        t = text.strip()
        if t.isdigit() and 1 <= int(t) <= len(cands):
            slug = cands[int(t) - 1]
            self._pending.pop(key, None)
            return slug
        return None

    def _start_resolved(self, channel, slug, task, root_ts=None):
        ack = self.client.chat_postMessage(channel=channel, text="🔧 on it…",
                                           thread_ts=root_ts)
        anchor_ts = ack["ts"]
        run_id = self._new_run_id()
        thread_key = root_ts or anchor_ts
        self.store.link_slack_thread(thread_key, channel, run_id, anchor_ts)
        self._submit(channel, thread_key, lambda: self._provision_and_fix(
            run_id, channel, anchor_ts, slug, task, thread_key))

    # --- fire-and-forget batch ---

    def _start_batch(self, channel, text, tasks, root_ts=None):
        """Enqueue a list of tasks on the resolved repo, post one summary, and
        thread each run. The scheduler drains them respecting admission; each
        run's progress renders into its thread via a registered sink."""
        res = self.resolver.resolve(text)
        if res.confidence == "none":
            self.client.chat_postMessage(
                channel=channel, thread_ts=root_ts,
                text="Which repo is this batch for? Name a repo, e.g. `owner/repo`.")
            return
        if res.confidence == "ambiguous":
            return self._ask_pick(channel, text, res, root_ts)
        slug = res.slug
        items = [{"repo": slug, "task": t, "source": "github"} for t in tasks]
        _batch_id, run_ids = self.manager.enqueue_batch(items)
        self.client.chat_postMessage(
            channel=channel, thread_ts=root_ts,
            text=f"📥 Queued {len(run_ids)} task{'s' if len(run_ids) != 1 else ''} "
                 f"on `{slug}` — I'll thread each as it runs.")
        for run_id, task in zip(run_ids, tasks):
            head = self.client.chat_postMessage(
                channel=channel, thread_ts=root_ts, text=f"• `{slug}`: {task}")
            anchor_ts = head["ts"]
            self.store.link_slack_thread(anchor_ts, channel, run_id, anchor_ts)
            self._register_batch_sink(channel, anchor_ts, slug, task, run_id)

    def _register_batch_sink(self, channel, anchor_ts, slug, task, run_id):
        """Register a best-effort thread renderer the scheduler passes to
        run_autonomous when this run is dispatched. Streams live progress via
        _apply; posts the PR link on completion and the reason on failure. Never
        raises into the run (best-effort render)."""
        state = {"head": f"• `{slug}`: {task}", "lines": [], "done": False,
                 "summary": None, "announce_live": False, "mode": "build",
                 "thread_ts": anchor_ts, "forge_url": self._session_link(run_id)}

        def sink(ev):
            try:
                if ev.kind == "done":
                    pr = ev.data.get("pr_url")
                    draft = " (draft)" if ev.data.get("draft") else ""
                    txt = f"📬 PR opened{draft}: {pr}" if pr else "✅ done."
                    self._safe_post(channel=channel, thread_ts=anchor_ts, text=txt)
                    self._post_artifacts(run_id, channel, anchor_ts)
                elif ev.kind == "error":
                    self._safe_post(
                        channel=channel, thread_ts=anchor_ts,
                        text=f"⚠️ {ev.data.get('kind')}: {ev.data.get('detail', '')}")
                else:
                    self._apply(run_id, channel, anchor_ts, state, ev)
            except Exception:
                logger.exception("forge batch render failed (run %s)", run_id)

        self.manager.set_event_sink(run_id, sink)

    # --- new session ---

    def _new_session(self, channel, text, root_ts=None, files=None):
        res = self.resolver.resolve(text)
        if res.confidence == "none":
            self.client.chat_postMessage(
                channel=channel, thread_ts=root_ts,
                text="Which repo? Tell me a name/slug (e.g. `owner/repo`).")
            return
        if res.confidence == "ambiguous":
            return self._ask_pick(channel, text, res, root_ts)
        # high confidence -> ack + run
        ack = self.client.chat_postMessage(channel=channel, text="🔧 on it…",
                                           thread_ts=root_ts)
        anchor_ts = ack["ts"]
        run_id = self._new_run_id()
        thread_key = root_ts or anchor_ts
        self.store.link_slack_thread(thread_key, channel, run_id, anchor_ts)
        self._submit(channel, thread_key, lambda: self._provision_and_fix(
            run_id, channel, anchor_ts, res.slug, text, thread_key, files=files))

    def _provision_and_fix(self, run_id, channel, anchor_ts, slug, task, thread_key,
                           files=None):
        ok, msg = self.manager.can_start()
        if not ok:
            self.client.chat_update(channel=channel, ts=anchor_ts,
                                    text=f"⚠️ {msg}")
            return
        head = self._opener(slug, task, "build", greeting_head(slug))
        state = {"head": head, "lines": [], "done": False,
                 "summary": None, "announce_live": True, "mode": "build",
                 "thread_ts": thread_key, "forge_url": self._session_link(run_id)}
        self._render(channel, anchor_ts, state)
        self._drive(run_id, channel, anchor_ts, state,
                    self.manager.start(run_id, slug, "github", task=task,
                                       origin="slack"))
        if state.get("failed"):
            return
        names, notes = self._fetch_attachments(run_id, files)
        self._post_attachment_notes(channel, thread_key, notes)
        # Autonomous by default: plan and proceed without a plan-approval gate;
        # plan_task still checkpoints if the plan has open questions (ambiguity).
        # auto_draft=True makes execution bottom-outs (CI/QA/login wall) land a
        # DRAFT PR instead of stalling — a build can run end-to-end from Slack.
        self._drive(run_id, channel, anchor_ts, state,
                    self.manager.plan_task(
                        run_id, task, policy=flow.CheckpointPolicy.for_slack(),
                        auto_draft=True, attachments=names, origin="slack"))

    # --- per-thread turn queue ---

    def _submit(self, channel, thread_key, job):
        """Run `job` (a no-arg turn-driver) if this thread is idle, then drain
        any follow-ups queued while it ran. If a turn is already in flight for
        this thread, enqueue the job and ack it instead of dropping it.

        Invariant: a thread clears `_running` only while holding `_mu` AND seeing
        an empty queue; producers check/set `_running` under the same lock — so
        no job is lost and no two turns for one thread overlap."""
        with self._mu:
            if thread_key in self._running:
                self._queues.setdefault(thread_key, []).append(job)
                queued = True
            else:
                self._running.add(thread_key)
                queued = False
        if queued:
            self.client.chat_postMessage(
                channel=channel, thread_ts=thread_key,
                text="📥 got it — I'll handle this right after the current step.")
            return
        cur = job
        while cur is not None:
            try:
                cur()
            except Exception:
                # Progress edits are already swallowed in-render, so reaching here
                # means the turn itself died (engine raised, bug). Don't vanish —
                # tell the thread, or the user is left staring at a dead opener.
                logger.exception("forge slack job failed (thread %s)", thread_key)
                self._safe_post(
                    channel=channel, thread_ts=thread_key,
                    text="⚠️ that step hit an error and stopped — try again, "
                         "or tell me what to do next.")
            with self._mu:
                q = self._queues.get(thread_key)
                if q:
                    cur = q.pop(0)
                else:
                    self._running.discard(thread_key)
                    cur = None

    def _run_review(self, channel, text):
        from forge.prref import find_pr_ref
        ref = find_pr_ref(text)
        if ref is None:
            self.client.chat_postMessage(
                channel=channel,
                text="Point me at a PR like `owner/repo#123` or a PR URL.")
            return
        ok, msg = self.manager.can_start()
        if not ok:
            self.client.chat_postMessage(channel=channel, text=f"⚠️ {msg}")
            return
        ack = self.client.chat_postMessage(
            channel=channel, text=f"🔍 reviewing `{ref.slug}#{ref.number}`…")
        anchor_ts = ack["ts"]
        run_id = self._new_run_id()
        self.store.link_slack_thread(anchor_ts, channel, run_id, anchor_ts)
        result = None
        # Pass the normalized ref, not the raw message (manager.review parses strictly).
        for ev in self.manager.review(run_id, f"{ref.slug}#{ref.number}",
                                      origin="slack"):
            if ev.kind == "review":
                result = ev.data
            elif ev.kind == "error":
                self.client.chat_postMessage(
                    channel=channel, thread_ts=anchor_ts,
                    text=f"⚠️ {ev.data.get('kind')}: {ev.data.get('detail', '')}")
        if result and result.get("ok"):
            tag = " _(under your account — set up the Forge GitHub App for the " \
                  "forge[bot] avatar)_" if result.get("degraded") else ""
            self.client.chat_postMessage(
                channel=channel, thread_ts=anchor_ts,
                text=f"📝 review posted{tag}: {result['review_url']} "
                     f"({result['comments']} inline)")
            # Surface any screenshots the review worker captured (best-effort;
            # helper is thread-safe and no-ops when there are none).
            self._post_artifacts(run_id, channel, anchor_ts)
        elif result:
            self.client.chat_postMessage(
                channel=channel, thread_ts=anchor_ts,
                text=f"⚠️ couldn't post review: {result.get('reason', 'unknown')}")

    # --- TurnEvent rendering ---

    def _drive(self, run_id, channel, anchor_ts, state, events):
        # Each _apply branch renders its own target: setup/coarse milestones go
        # to the parent (anchor_ts); turn narration + tool actions stream to the
        # in-thread live reply (state["live_ts"]).
        for ev in events:
            self._apply(run_id, channel, anchor_ts, state, ev)

    @staticmethod
    def _complete_active(state, mark="✅"):
        """Flip the currently-active stage line's ⏳ to `mark`. Called when the
        next stage starts (or the turn errors) so the progress message tracks
        reality live instead of staying all-hourglass until the session ends."""
        i = state.pop("_active", None)
        if i is not None and 0 <= i < len(state["lines"]):
            state["lines"][i] = state["lines"][i].replace("⏳", mark, 1)

    def _apply(self, run_id, channel, anchor_ts, state, ev):
        d = ev.data
        if ev.kind == "phase":
            self._complete_active(state)
            name = d.get("name")
            label = _PHASE_EMOJI.get(name, d.get("label", ""))
            state["lines"].append(f"⏳ {label}")
            state["_active"] = len(state["lines"]) - 1
            if name == "agent" and not state.get("live_ts"):
                # No public URL was announced (e.g. no web service) — open a
                # dedicated in-thread reply to stream the turn into.
                r = self._safe_post(
                    channel=channel, thread_ts=state["thread_ts"], text="🛠️ working…")
                if r:
                    state["live_ts"] = r["ts"]
            self._render(channel, anchor_ts, state)
        elif ev.kind == "narration":
            text = (d.get("text") or "").strip()
            if text:
                state.setdefault("narration", []).append(text)
                self._render_live(channel, state)
        elif ev.kind == "tool":
            tgt = (" — " + d["target"]) if d.get("target") else ""
            state["action"] = f"Agent working{tgt}"
            self._render_live(channel, state)
        elif ev.kind == "url":
            url = self.tunnel.start(run_id, _localhost_target(d["web_url"]))
            state["public_url"] = url or d["web_url"]
            state["local_url"] = d.get("local_url")
            state["workspace_url"] = self._workspace_link(run_id)
            if state.get("announce_live") and not state.get("_announced"):
                state["_announced"] = True
                r = self._safe_post(
                    channel=channel, thread_ts=state["thread_ts"],
                    text=f"It's up — {state['public_url']}. Making the change now…")
                if r:
                    state["live_ts"] = r["ts"]      # reuse it as the live message
            self._render(channel, anchor_ts, state)
        elif ev.kind == "verify":
            state["verify"] = d.get("ok")
            state["verify_failed"] = d.get("failed") or []
            state["verify_output"] = d.get("output") or ""
        elif ev.kind == "plan":
            from forge.plan import Plan
            p = Plan(goal=d.get("goal", ""), steps=tuple(d.get("steps") or ()),
                     acceptance=tuple(d.get("acceptance") or ()),
                     assumptions=tuple(d.get("assumptions") or ()),
                     open_questions=tuple(d.get("open_questions") or ()),
                     risk=d.get("risk", "unknown"))
            # A short plan is glanceable inline; a real one (the planner emits
            # several steps + acceptance) goes out as one line with the full
            # plan attached as a snippet (collapsed by Slack) instead of
            # walling the channel. Same threshold as digest_for_slack.
            md = p.to_markdown()
            if len(md) <= SLACK_DIGEST_LIMIT:
                self._safe_post(channel=channel, thread_ts=anchor_ts, text=md)
            else:
                n = len(p.steps)
                self._safe_post(
                    channel=channel, thread_ts=anchor_ts,
                    text=(f"📋 Finished planning — {narration_line(p.goal)} "
                          f"({n} step{'s' if n != 1 else ''} · risk {p.risk}). "
                          "Full plan attached."))
                self._attach_snippet(channel, anchor_ts, md,
                                     filename="plan.md", title="Plan")
        elif ev.kind == "checkpoint":
            state["checkpoint_id"] = d.get("id")
            # Show what the agent actually did (matches the web transcript) before
            # the ask, so a pause reflects real work instead of a bare prompt. Only
            # when a summary is actually present — clean_summary() defaults empties
            # to "Done.", which would be a bogus line before a plan/ambiguity ask.
            if d.get("summary"):
                self._post_digest(channel, anchor_ts,
                                  clean_summary(d.get("summary"), limit=12000))
            # Answering works on either surface (checkpoints live in the store);
            # link the web session so the ask can be handled there too.
            link = self._session_link(run_id)
            web_hint = f"\n🧭 or answer on the web: {link}" if link else ""
            if d.get("type") == flow.NEEDS_INPUT:
                self._safe_post(channel=channel, thread_ts=anchor_ts,
                                text=f"🔐 {d.get('prompt', 'I need credentials to continue.')} "
                                     "(or reply *stop*)" + web_hint)
            else:
                self._safe_post(channel=channel, thread_ts=anchor_ts,
                                text=f"🟡 {d.get('prompt', 'Approve to proceed?')} "
                                     "(reply *approve*, or describe changes)" + web_hint)
        elif ev.kind == "creds_saved":
            self._safe_post(channel=channel, thread_ts=state["thread_ts"],
                            text=f"🔐 saved login for `{d.get('repo')}` — say "
                                 "'forget creds' to remove.")
        elif ev.kind == "retrospective":
            n = d.get("added") or 0
            if n:
                self._safe_post(
                    channel=channel, thread_ts=state["thread_ts"],
                    text=f"🧠 learned {n} thing{'s' if n != 1 else ''} about "
                         "this repo for next time.")
        elif ev.kind == "slept":
            state["done"] = True
            self._safe_post(channel=channel, thread_ts=state["thread_ts"],
                            text=d.get("message", "💤 Paused — reply here to wake."))
        elif ev.kind == "error":
            state["failed"] = True
            self._complete_active(state, "❌")
            state["lines"].append(f"❌ {d.get('kind')}: {d.get('detail', '')}")
            self._render(channel, anchor_ts, state)
        elif ev.kind == "done":
            state["done"] = True
            state["diff_files"] = d.get("diff_files")
            state["verify_ok"] = d.get("verify_ok")
            state["summary"] = d.get("message")
            state["pr_url"] = d.get("pr_url")      # set iff execute auto-opened a PR
            state["draft"] = d.get("draft")
            state["pr_updated"] = d.get("updated")  # a follow-up build that pushed to an existing PR
            self._render(channel, anchor_ts, state)
            self._render_live(channel, state)      # retire the ⏳ action line
            self._finish(run_id, channel, anchor_ts, state)

    # Progress UI is best-effort: a dropped/throttled edit (Slack 429s rapid
    # chat.update of one message) must never abort the turn. The engine reports
    # real failures as `error` events; a *transport* error on a cosmetic edit is
    # logged (one line, not a stack trace — these repeat) and swallowed here so
    # the turn always runs to its result. Text is length-guarded on the way out
    # so an oversized message degrades to a truncated one instead of a
    # msg_too_long rejection on every subsequent edit.
    def _safe_update(self, channel, ts, text="", blocks=None):
        try:
            self.client.chat_update(channel=channel, ts=ts,
                                    text=truncate_for_slack(text), blocks=blocks)
        except Exception as e:
            logger.warning("forge slack progress edit failed (ts %s): %r", ts, e)

    def _safe_post(self, **kwargs):
        """Best-effort chat_postMessage for progress milestones. Returns the
        response (so callers can capture `ts`) or None on failure."""
        if "text" in kwargs:
            kwargs["text"] = truncate_for_slack(kwargs["text"])
        try:
            return self.client.chat_postMessage(**kwargs)
        except Exception as e:
            logger.warning("forge slack progress post failed: %r", e)
            return None

    def _post_digest(self, channel, thread_ts, text,
                     filename="forge-notes.md", title="Full notes"):
        """Post `text`, keeping the channel glanceable: a long message goes out
        as a short digest with the full text attached as a snippet. Best-effort
        like every progress post — a failed snippet upload leaves the digest
        standing with a note, never a broken turn."""
        short, full = digest_for_slack(text)
        if not full:
            return self._safe_post(channel=channel, thread_ts=thread_ts, text=text)
        r = self._safe_post(channel=channel, thread_ts=thread_ts,
                            text=short + "\n📄 full notes attached.")
        self._attach_snippet(channel, thread_ts, full, filename, title)
        return r

    def _attach_snippet(self, channel, thread_ts, content,
                        filename="forge-notes.md", title="Full notes"):
        try:
            self.client.files_upload_v2(channel=channel, thread_ts=thread_ts,
                                        content=content, filename=filename,
                                        title=title)
        except Exception as e:
            logger.warning("forge snippet upload failed: %r", e)
            self._safe_post(channel=channel, thread_ts=thread_ts,
                            text="⚠️ couldn't attach the full notes — ask me "
                                 "for the details.")

    def _session_link(self, run_id) -> str:
        """Deep link to this session in the forge web app ('' when the daemon
        has no FORGE_WEB_URL — e.g. unit tests with a bare cfg namespace)."""
        return web_session_link(getattr(self.cfg, "forge_web_url", ""), run_id)

    def _workspace_link(self, run_id) -> str:
        """Deep link to the live workspace (app + chat side by side) in the
        forge web app ('' when the daemon has no FORGE_WEB_URL)."""
        return web_workspace_link(getattr(self.cfg, "forge_web_url", ""), run_id)

    @staticmethod
    def _url_lines(state):
        """The public tunnel link (share it) plus, when present, the local
        *.forge.localhost link that opens on the forge host with no external DNS
        — a working fallback when the public hostname can't be resolved there.
        When a live app has a workspace link, 🗔 (running app + agent chat side
        by side) is the richer surface and supersedes the 🧭 dashboard-session
        line; 🧭 remains for messages with no live app (e.g. answers)."""
        lines = []
        if state.get("public_url"):
            lines.append(f"🌐 {state['public_url']}")
        if state.get("local_url"):
            lines.append(f"🏠 {state['local_url']} (local, no DNS)")
        if state.get("workspace_url"):
            lines.append(f"🗔 {state['workspace_url']} (app + chat in forge web)")
        elif state.get("forge_url"):
            lines.append(f"🧭 {state['forge_url']} (session in forge web)")
        return lines

    def _render(self, channel, anchor_ts, state):
        body = [state["head"]]
        for ln in state["lines"]:
            body.append(ln.replace("⏳", "✅") if state.get("done") else ln)
        body += self._url_lines(state)
        text = "\n".join(body)
        if state.get("_last_parent") == text:    # skip redundant edits
            return
        state["_last_parent"] = text
        self._safe_update(channel, anchor_ts, text=text)

    def _render_live(self, channel, state):
        ts = state.get("live_ts")
        if not ts:
            return
        parts = self._url_lines(state)
        narr = state.get("narration") or []
        if narr:
            parts.append("🛠️ " + narration_line(narr[-1]))
        # A finished turn must not leave the ticker frozen mid-action on ⏳.
        if state.get("done"):
            parts.append("✅ done")
        elif state.get("action"):
            parts.append(f"⏳ {state['action']}")
        text = "\n".join(parts) or "🛠️ working…"
        if state.get("_last_live") == text:
            return
        state["_last_live"] = text
        self._safe_update(channel, ts, text=text)

    def _finish(self, run_id, channel, anchor_ts, state):
        # Clean with a high cap — the digest keeps the *message* short, and the
        # attached snippet carries the long form, so nothing is lost to the cap.
        summary = clean_summary(state.get("summary"), limit=12000)
        n = state.get("diff_files") or 0
        thread_ts = state["thread_ts"]
        # A question turn (or a build that changed nothing) is an answer, not a
        # build report — after a build, a follow-up question still shows the prior
        # files in `git diff`, so mode is the reliable signal, n==0 a backstop.
        # A build turn that pushed (pr_url set) is always a report, even if this
        # turn's diff was empty (e.g. it only pushed a prior turn's stranded commit).
        if state.get("mode") == "qa" or (n == 0 and not state.get("pr_url")):
            self._post_digest(channel, thread_ts, summary)
            return
        base = state.get("public_url")
        link = deep_link(base, self.manager.diff(run_id)) if base else None
        short, full = digest_for_slack(summary)
        parts = [short + ("\n📄 full notes attached." if full else "")]
        if link:
            parts.append(f"🎉 {link}")
        parts.append(f"{n} file(s) changed · "
                     + self._verify_line(state.get("verify_ok"),
                                         state.get("verify_failed"),
                                         state.get("verify_output")))
        # If execute already opened the PR (autonomous build), show the link — the
        # finish line for a build run entirely from Slack. Only offer the "Open PR"
        # button when nothing was opened yet (a follow-up turn awaiting the push).
        pr_url = state.get("pr_url")
        if pr_url:
            if state.get("pr_updated"):
                parts.append(f"📬 PR updated: {pr_url}")
            else:
                draft = " (draft)" if state.get("draft") else ""
                parts.append(f"📬 PR opened{draft}: {pr_url}")
            self._safe_post(channel=channel, thread_ts=thread_ts,
                            text="\n".join(parts))
        else:
            text = "\n".join(parts)
            self._safe_post(channel=channel, thread_ts=thread_ts,
                            text=text, blocks=self._pr_button(run_id, text))
        if full:
            self._attach_snippet(channel, thread_ts, full)
        self._post_artifacts(run_id, channel, thread_ts)

    def _download(self, url):
        """GET a Slack private file with the bot token. Isolated for tests."""
        import urllib.request
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.cfg.slack_bot_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (resp.headers.get("Content-Type", ""),
                    resp.read(inbox.MAX_BYTES + 1))

    def _fetch_attachments(self, run_id, files):
        """Download a message's image attachments into the run's inbox. Returns
        (stored_names, notes). Best-effort by the render rule: every failure is a
        note in the thread, never an aborted turn."""
        names, notes = [], []
        files = files or []
        imgs = [f for f in files if (f.get("mimetype") or "").startswith("image/")]
        if len(files) - len(imgs):
            notes.append(f"skipped {len(files) - len(imgs)} non-image file(s)")
        if len(imgs) > inbox.MAX_FILES:
            notes.append(f"taking only the first {inbox.MAX_FILES} images")
            imgs = imgs[:inbox.MAX_FILES]
        max_mb = inbox.MAX_BYTES // (1024 * 1024)
        for f in imgs:
            label = f.get("name") or "image"
            if (f.get("size") or 0) > inbox.MAX_BYTES:
                notes.append(f"skipped `{label}` (over {max_mb} MB)")
                continue
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                notes.append(f"couldn't fetch `{label}` (no download URL)")
                continue
            try:
                ctype, data = self._download(url)
            except Exception:
                logger.exception("forge slack file download failed (%s)", label)
                notes.append(f"couldn't fetch `{label}` (download failed)")
                continue
            if ctype.startswith("text/html"):
                # Slack serves its login page instead of the bytes when the app
                # lacks the files:read scope — name the fix, don't fail silently.
                notes.append(f"couldn't fetch `{label}` — add the `files:read` "
                             "bot scope and reinstall the app")
                continue
            if len(data) > inbox.MAX_BYTES:
                notes.append(f"skipped `{label}` (over {max_mb} MB)")
                continue
            try:
                names.append(self.manager.save_attachment(
                    run_id, label, data, mimetype=f.get("mimetype")))
            except Exception as e:
                notes.append(f"skipped `{label}` ({e})")
        return names, notes

    def _post_attachment_notes(self, channel, thread_ts, notes):
        if notes:
            self._safe_post(channel=channel, thread_ts=thread_ts,
                            text="📎 " + "; ".join(notes))

    @staticmethod
    def _artifact_lead(arts) -> str:
        kinds = {a.kind for a in arts}
        if "before" in kinds and "after" in kinds:
            return "📸 before / after:"
        if "video" in kinds:
            return "🎥 here's the flow:"
        return "📸 here's how it looks:"

    def _post_artifacts(self, run_id, channel, thread_ts):
        """Upload whatever screenshots/video the agent captured. Best-effort but
        NOT silent: each upload is isolated, failures are logged and surfaced as
        a short note so a broken upload never reads as "nothing to show". The
        lead-in is posted lazily, just before the first upload attempt, so it is
        never left bare."""
        try:
            arts = self.manager.artifacts(run_id)
        except Exception:
            logger.exception("artifact collection failed for run %s", run_id)
            return
        if not arts:
            return
        lead, posted_lead = self._artifact_lead(arts), False
        for a in arts:
            try:
                if not posted_lead:
                    self.client.chat_postMessage(channel=channel,
                                                 thread_ts=thread_ts, text=lead)
                    posted_lead = True
                self.client.files_upload_v2(channel=channel, thread_ts=thread_ts,
                                            file=str(a.path), title=a.caption)
            except Exception as e:
                logger.exception("artifact upload failed: %s", a.path)
                self.client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=self._upload_error_note(a.path.name, e))

    @staticmethod
    def _upload_error_note(name, exc):
        """Turn an upload exception into a short, actionable note. A SlackApiError
        stringifies as a generic prefix with the real reason ({'error': ...}) at
        the very end, so truncating its str() drops exactly the useful part. Read
        the code off the response instead, and call out the one that bites here in
        practice — a bot missing files:write, which fails files_upload_v2 at its
        first hop (files.getUploadURLExternal)."""
        code = needed = ""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                code, needed = resp["error"] or "", resp.get("needed") or ""
            except (KeyError, TypeError, AttributeError):
                code = needed = ""
        if code == "missing_scope":
            scope = needed or "files:write"
            return (f"⚠️ couldn't attach `{name}` — the bot is missing the "
                    f"`{scope}` OAuth scope. Add it under the Slack app's "
                    f"OAuth & Permissions, reinstall the app, and try again.")
        if code:
            return f"⚠️ couldn't attach `{name}` — Slack rejected it: `{code}`"
        return (f"⚠️ couldn't attach `{name}` — "
                f"{type(exc).__name__}: {str(exc)[:160]}")

    @staticmethod
    def _verify_line(ok, failed, output=""):
        # Never assert "tests failing": checks often fail for environmental
        # reasons (missing browser libs, no creds) while the code is fine.
        if ok is True:
            return "tests pass ✅"
        if ok is None:
            return "no automated checks"
        names = ", ".join(f"`{f}`" for f in (failed or [])) or "some checks"
        return f"⚠️ {names} didn't pass cleanly — {concise_verify_reason(output)}"

    def _pr_button(self, run_id, text):
        # Block Kit section text has its own (3000-char) cap, tighter than the
        # message-text limit the plain path is guarded by.
        text = truncate_for_slack(text, SLACK_BLOCK_TEXT_LIMIT)
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": "open_pr",
                 "text": {"type": "plain_text", "text": "Open PR"},
                 "value": run_id}]}]

    # --- ambiguous pick / follow-up / actions / lifecycle ---

    def _ask_pick(self, channel, text, res, root_ts=None):
        key = root_ts or channel
        self._pending[key] = res.candidates
        self._pending_text[key] = text
        lines = ["I found a few — reply with a number:"]
        for i, slug in enumerate(res.candidates, 1):
            lines.append(f"{i}. `{slug}`")
        self.client.chat_postMessage(channel=channel, thread_ts=root_ts,
                                     text="\n".join(lines))

    def _follow_up(self, channel, thread_ts, text, run_turn=True, mode="build",
                   files=None):
        run_id = self.store.run_for_thread(thread_ts)
        state = {"head": "↪️ on it…", "lines": [], "done": False,
                 "summary": None, "mode": mode, "thread_ts": thread_ts,
                 "forge_url": self._session_link(run_id)}
        ack = self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                           text="↪️ on it…")
        anchor_ts = ack["ts"]
        if self.store.get_run(run_id).get("state") == "asleep":
            self._drive(run_id, channel, anchor_ts, state,
                        self.manager.wake(run_id, origin="slack"))
            if state.get("failed"):
                return
        if not run_turn:
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                         text="☀️ awake — what next?")
            return
        names, notes = self._fetch_attachments(run_id, files)
        self._post_attachment_notes(channel, thread_ts, notes)
        # A build follow-up must finish like the first task — commit, push, and
        # open-or-update the PR. build_turn does that; turn() is the read-only
        # chat/QA reply that never pushes (its changes would otherwise pile up
        # uncommitted in the container, unreachable to the credential-less agent).
        driver = (self.manager.build_turn(run_id, text, attachments=names, origin="slack")
                  if mode == "build"
                  else self.manager.turn(run_id, text, attachments=names, origin="slack"))
        self._drive(run_id, channel, anchor_ts, state, driver)

    def handle_open_pr(self, channel, run_id, user=None):
        # The "Open PR" button is visible to everyone who can see the message,
        # but acting on it pushes commits and opens a PR under forge's GH token.
        # Gate it to the allowed user, exactly like every message entry point.
        # `user=None` is for internal/test callers; the Slack action always
        # passes the clicker's id, so the live surface is always gated.
        if user is not None and user != self.cfg.slack_allowed_user:
            self._gate_notice(channel, user)
            return
        res = self.manager.open_pr(run_id)
        row = self.store.slack_thread_for_run(run_id) or {}
        thread_ts = row.get("thread_ts") or row.get("anchor_ts")
        if res.get("ok"):
            tag = " (draft)" if res.get("draft") else ""
            txt = f"📬 PR opened{tag}: {res['pr_url']}"
        else:
            txt = f"⚠️ couldn't open PR: {res.get('reason', 'unknown')}"
        self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=txt)

    def on_lifecycle(self, run_id, transition):
        row = self.store.slack_thread_for_run(run_id)
        if not row:
            return
        reason = (self.store.get_env(run_id) or {}).get("state_reason")
        txt = self._lifecycle_text(run_id, transition, reason)
        if not txt:
            return
        self.client.chat_postMessage(channel=row["channel"],
                                     thread_ts=row.get("thread_ts") or row["anchor_ts"],
                                     text=txt)

    def _lifecycle_text(self, run_id, transition, reason):
        """Cause-accurate lifecycle notice, or None to stay silent. A sleep
        initiated FROM this Slack thread already got an ack from the intent
        handler ('💤 sleeping …' / '💤 Paused …') — announcing it again from
        the sweep produced the duplicate 'slept after idle' message."""
        if transition == "asleep":
            if reason == "slack":
                return None
            if reason == "idle":
                return "💤 slept after idle — reply here to wake it."
            if reason == "web":
                return "💤 slept from the web app — reply here to wake it."
            return "💤 slept — reply here to wake it."
        if transition == "deleted":
            if reason == "dormant":
                branch = (self.store.get_run(run_id) or {}).get("branch", "?")
                return ("🗑️ removed after the dormant window "
                        f"(code archived to `{branch}`).")
            if reason == "web":
                return "🗑️ ended from the web app."
            if reason == "gone":   # wake already told the user the workspace is gone
                return None
            return "🗑️ session ended."
        return None

    # --- mirror: turns driven from other surfaces (web/CLI) into the thread ---

    # Pre-flight rejections a foreign surface triggers (e.g. clicking Send on
    # the web while a Slack turn runs). Not a real turn — never open an anchor.
    _PREFLIGHT_ERRORS = ("busy", "not_provisioned", "checkpoint")

    def attach_bus(self, bus) -> None:
        """Subscribe to the engine's event bus so turns this bot did NOT drive
        (web app, CLI attach) render into the run's linked thread — both
        surfaces update simultaneously. Slack-driven turns (origin 'slack') and
        queued batch runs (origin 'queue', rendered by their registered sink)
        are filtered out, so nothing renders twice."""
        bus.tap(self._on_bus_event)

    def _on_bus_event(self, run_id, stamped) -> None:
        try:
            self._mirror_event(run_id, stamped)
        except Exception:
            # Mirroring is best-effort like every progress render: a Slack
            # hiccup must never disturb the surface actually driving the turn.
            logger.exception("forge slack mirror failed (run %s)", run_id)

    def _mirror_event(self, run_id, stamped) -> None:
        if stamped.get("origin") in ("slack", "queue"):
            return
        row = self.store.slack_thread_for_run(run_id)
        if not row:
            return                       # run was never surfaced in Slack
        channel = row["channel"]
        thread_ts = row.get("thread_ts") or row["anchor_ts"]
        kind, data = stamped["kind"], stamped["data"]
        if kind == "checkpoint_answered":
            detail = data.get("body") or data.get("action") or "answered"
            self._safe_post(channel=channel, thread_ts=thread_ts,
                            text=f"✅ answered from the web app: {detail} — "
                                 "continuing; progress streams here too.")
            return
        if kind == "stream_end":         # flow over — release render state
            self._mirror_state.pop(run_id, None)
            return
        m = self._mirror_state.get(run_id)
        if m is None:
            if kind == "error" and data.get("kind") in self._PREFLIGHT_ERRORS:
                return
            if kind == "done":           # tail of a turn we never anchored
                return
            ack = self._safe_post(channel=channel, thread_ts=thread_ts,
                                  text="🖥️ driving from the web app…")
            if not ack:
                return
            m = {"channel": channel, "anchor_ts": ack["ts"],
                 "state": {"head": "🖥️ driving from the web app…", "lines": [],
                           "done": False, "summary": None,
                           "announce_live": False, "mode": "build",
                           "thread_ts": thread_ts,
                           "forge_url": self._session_link(run_id)}}
            self._mirror_state[run_id] = m
        self._apply(run_id, m["channel"], m["anchor_ts"], m["state"],
                    TurnEvent(kind, data))
        if kind in ("done", "error", "checkpoint", "slept"):
            self._mirror_state.pop(run_id, None)   # turn over; next one re-anchors


def install_rate_limit_retry(client, max_retry_count=3):
    """Add slack_sdk's RateLimitErrorRetryHandler to a WebClient so 429s are
    retried with backoff (honoring Retry-After) instead of raising. The default
    client carries only a ConnectionErrorRetryHandler; the live-feedback work
    edits the progress message often enough to draw chat.update rate limits, and
    an un-retried 429 would otherwise surface as a SlackApiError mid-turn.
    Idempotent — won't stack a second handler if one is already registered."""
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler
    if any(isinstance(h, RateLimitErrorRetryHandler) for h in client.retry_handlers):
        return client
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=max_retry_count))
    return client


def build_app(manager, store, cfg, resolver, tunnel):
    """Build the slack_bolt Socket Mode app + a wired ForgeSlackBot, WITHOUT
    blocking. Returns (bot, socket_handler); the caller starts the handler
    (handler.start() blocks). slack_bolt is imported lazily so it stays an
    optional dependency."""
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    from forge import slackchat, slackopener, slackqa, slackroute

    app = App(token=cfg.slack_bot_token)
    install_rate_limit_retry(app.client)
    bot_user_id = app.client.auth_test().get("user_id")
    bot = ForgeSlackBot(
        manager, store, cfg, resolver, tunnel, app.client,
        bot_user_id=bot_user_id,
        qa_answer=lambda slug, q: slackqa.answer_question(cfg, slug, q),
        opener=lambda slug, task, mode, fallback: slackopener.generate_opener(
            cfg, slug, task, mode, fallback),
        chat_reply=lambda transcript, latest: slackchat.generate_reply(
            cfg, transcript, latest, help_blurb()),
        chat_router=lambda transcript, latest: slackroute.route_chat(
            cfg, transcript, latest, help_blurb()))
    # Cross-surface mirror: web/CLI-driven turns render into linked threads.
    bot.attach_bus(manager.bus)

    @app.event("app_mention")
    def _on_mention(event, logger):
        root = event.get("thread_ts") or event.get("ts")
        bot.handle_mention(event["channel"], event.get("user"),
                           event.get("text", ""), root, files=event.get("files"))

    @app.event("message")
    def _on_message(event, logger):
        # file_share is the ONE subtype allowed through: an uploaded image arrives
        # as message/file_share, and dropping it made attachments invisible.
        sub = event.get("subtype")
        if (sub and sub != "file_share") or event.get("bot_id"):
            return
        files = event.get("files")
        if event.get("channel_type") == "im":      # DM: every message is for forge
            bot.handle_message(event["channel"], event.get("user"),
                               event.get("text", ""), event.get("thread_ts"),
                               files=files)
        else:                                       # channel: only forge threads
            bot.route_channel_message(event["channel"], event.get("user"),
                                      event.get("text", ""), event.get("thread_ts"),
                                      files=files)

    @app.action("open_pr")
    def _on_open_pr(ack, body):
        ack()
        run_id = body["actions"][0]["value"]
        bot.handle_open_pr(body["channel"]["id"], run_id,
                           user=body.get("user", {}).get("id"))

    return bot, SocketModeHandler(app, cfg.slack_app_token)


def run(manager, store, cfg, resolver, tunnel):
    """Build and block serving Slack events. Returns nothing (blocks until the
    socket closes). Use build_app() when you need the bot instance first."""
    _bot, handler = build_app(manager, store, cfg, resolver, tunnel)
    handler.start()
