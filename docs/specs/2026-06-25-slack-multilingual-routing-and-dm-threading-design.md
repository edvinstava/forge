# Slack: multilingual intent routing + DM thread continuity

**Date:** 2026-06-25
**Status:** approved (just-fix-it path)

## Problem

A user sent forge a build request in Norwegian:

> *"Hei, kan du **lage** en hello-henrik side på webapp appen…"*

forge replied conversationally (*"tar fatt på det nå… Poster lenken her så snart
den er oppe"*) and then went silent. No run was created, nothing appeared at
`127.0.0.1:8099`, and `forge.db` showed no write after the request.

### Root cause #1 — English-only intent routing (the "holdup")

`slackmsg.classify_intent()` decides build-vs-chat with an English-only verb
regex (`add|create|make|build|fix|…`). Norwegian verbs (*lage, lag, endre, fiks,
gjør, legg til*) don't match, and with no trailing `?` the message falls through
to the safe default `chat`. The `_chat` path calls the conversational LLM and
posts a reply but **never starts a build, never creates a run, never writes the
DB**. The LLM, understanding the request, promised work that nothing triggered.

This is two brains disagreeing: a brittle regex router and a fluent LLM, where
only the router can start a build.

Reproduced:

```
'chat'  <- Hei, kan du lage en hello-henrik side på webapp...
'build' <- can you make a hello-henrik page on the webapp...   (same, English)
```

### Root cause #2 — DM replies escape their thread

In a DM, the message handler receives `thread_ts` but the reply paths
(`_chat`, `_qa_fresh`, new-session ack) thread on `root_ts`, which is left
`None`. So a reply made *inside* a DM thread is answered at the **top level** of
the DM — "it sends a new message in the chat instead of continuing the thread."

## Design

### Fix 1 — LLM disambiguates the `chat` bucket

Keep the fast regex for the clear cases (English build verbs, `sleep`/`wake`/
`status`, PR refs, repo questions). Only when the regex would fall through to
`chat`, ask one LLM call to decide what the teammate actually wants:

```
regex classify_intent(text)
   └─ "chat"?  ──► route_chat(transcript, latest)  →  Route(action, reply)
                      action == "build" → _new_session(text)   (real build, real opener)
                      action == "qa"    → _qa_fresh(text)
                      action == "chat"  → post reply            (no false promises)
```

- New module `slackroute.py` (mirrors `slackchat.py`): `route_chat(cfg,
  transcript, latest, fallback, run=subprocess.run) -> Route`. One host
  `claude -p` call returning strict JSON `{"action": "build"|"question"|"chat",
  "reply": str}`. Parsing is tolerant; on **any** failure it returns
  `Route("chat", fallback)` — never worse than today's canned blurb.
- New pure prompt `slackmsg.route_prompt(transcript, latest)`, grounded in the
  existing `_CHAT_PERSONA`, with explicit multilingual build examples.
- `Route` dataclass: `action` ∈ {`build`, `qa`, `chat`}, optional `reply`.
- `ForgeSlackBot` gains an injected `chat_router` (default returns
  `Route("chat", None)` → behavior identical to today without the LLM). Wired in
  `build_app` to `slackroute.route_chat`. The `handle_message` `chat` branch
  calls a new `_chat_or_route`; the existing `_chat` (genuine reply via
  `chat_reply`) stays for the `_qa_fresh` low-confidence fallback so it never
  re-routes into a loop.

Because the brain that routes is also the brain that talks, the over-promising
failure mode disappears: a build hands off to the real build flow (whose opener
honestly says "pulling up `repo` and making the change"); genuine chat just
chats.

### Fix 2 — DM thread continuity

In `handle_message`, after the live-run / qa-thread lookups, derive
`root_ts = root_ts or thread_ts`. A reply made inside any thread (DM included)
then continues in that thread; a brand-new top-level DM (no `thread_ts`) is
unchanged. Channels already pass `root_ts` explicitly, so they're unaffected.

## Out of scope (noted, not bundled)

`store._conn()` uses `with self._conn() as c:`, which commits the transaction
but doesn't close the connection. Worth a follow-up, but not the cause here.

## Testing

- `test_slackmsg`: `route_prompt` carries the JSON schema + a Norwegian build
  example + the latest message.
- `test_slackroute`: `parse_route` (build / question→qa / chat / garbage→chat),
  and `route_chat` fallback on exception / nonzero / auth-error.
- `test_slackbot`:
  - regex-`chat` + router→`build` starts a session and links the thread;
  - router→`qa` uses the QA path, no build;
  - router→`chat` posts the reply, no build;
  - default router (no injection) preserves all existing chat behavior;
  - a DM reply with `thread_ts` set is answered **in that thread**.
