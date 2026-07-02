# Forge — Conversational Slack Chat — Design Spec

- **Date:** 2026-06-25
- **Status:** Approved (brainstorm complete) — implementing
- **Author:** dev@example.com (with Claude)
- **Builds on:** the Slack coworker bot (`slackbot.py`, `slackmsg.py`), the conversational opener
  (`slackopener.py`) and clone-only repo Q&A (`slackqa.py`).

---

## 1. Summary

Today every Slack message forge receives is funneled through `classify_intent()` — a set of regexes that
bucket the message into `build` / `qa` / `help` / `sleep` / `wake` / `status` / `review`, **defaulting to
`build` when nothing matches**. The `help` bucket posts a single hardcoded blurb. Two consequences make
forge feel rigid rather than like an LLM:

- "tell me what you can do" either matched nothing → defaulted to **build** → ran the repo resolver →
  "I found a few — reply with a number"; or hit `help` → a canned static blurb.
- Meta questions ("how do you work? where are you running?") classify as `qa` → handed to the **repo**
  resolver → "Which repo?", because the router assumes every question is about a repo.

forge has no path that answers **as forge, like an LLM**. This spec adds one: a `chat` route that
generates a real conversational reply via a one-shot host `claude -p` call (the same pattern
`slackopener`/`slackqa` already use), grounded in a forge persona, with multi-turn context pulled from the
Slack thread, and `help_blurb()` as the deterministic fallback so the worst case is never worse than today.

## 2. Locked decisions (from brainstorm)

| Question | Decision |
|---|---|
| Scope | Meta/identity answers **and** general multi-turn chat (back-and-forth in a thread). |
| Routing boundary | **Chat is the safe default.** Explicit `build` verbs, `sleep`/`wake`/`status`, `review` (PR ref), and repo-questions that resolve to a real repo stay deterministic. Everything else → `chat`. |
| Build path | **Unchanged.** A build verb is a strong action signal; `none`→"Which repo?", `ambiguous`→picker, `high`→build all stay. Only `chat`/`qa` get the conversational treatment. |
| Model | **Default (Sonnet) tier** (same as repo Q&A), not haiku — better for "think through an idea". |
| Multi-turn context | **Fetched from Slack** each turn (`conversations.replies` in a channel, `conversations.history` in a DM), not kept in memory — durable across forge's frequent restarts. Degrades to single-turn if the fetch fails. |

## 3. Routing changes (`slackmsg.classify_intent`)

Two precise edits; the `review` and `build`-verb branches are untouched:

- The `help` intent is **renamed `chat`** (identity/meta phrases: "what can you do", "are you a bot",
  "what is forge?", and `is_question and forge` mentions).
- The **default fallthrough** changes from `build` → **`chat`**.

`build` is still returned for messages containing a build verb, so explicit tasks are unaffected.

```
def classify_intent(text):
    if find_pr_ref(t): return "review"
    if _BUILD_VERBS.search(t): return "build"
    if _SLEEP/_WAKE/_STATUS: return that
    if help/identity/meta: return "chat"     # was "help"
    if is_question: return "qa"
    return "chat"                            # was "build"
```

## 4. The `qa` dead-end becomes conversational

`_qa_fresh`: when the resolver returns **non-high** confidence (today → "Which repo? …"), route to the
**chat path** instead. forge answers if the question is about itself, or asks which repo naturally.
`build` with no repo is **unchanged**.

## 5. New module `slackchat.py` (mirrors `slackopener.py`)

```
generate_reply(cfg, transcript, latest, fallback, run=subprocess.run) -> str
```
One-shot `claude -p` on the **default model** (`worker_cmd(prompt, None)`), in a neutral empty dir
(`cache/chat`), with a ~30s timeout. Any exception / non-zero exit / auth-error / empty result →
returns `fallback`. On success → `clean_summary(result_text)`.

## 6. Persona prompt + transcript (`slackmsg`, pure + unit-tested)

- `chat_prompt(transcript, latest) -> str` — embeds forge's **real** capabilities (spin up a repo in a
  sandbox → live preview link → run checks / before-after screenshots → open a PR; answer questions about
  a specific repo; `sleep`/`wake`/`status`; reply in-thread to continue). Tone: warm, concise colleague,
  plain Slack text (no markdown headings / essays). Explicit rules: don't invent capabilities; if they
  clearly want a change, ask for the repo + task; don't describe yourself unprompted. Includes the prior
  `transcript` (or a "start of conversation" placeholder) and the `latest` message to reply to.
- `format_transcript(messages, bot_user_id) -> str` — maps Slack messages (chronological) to
  `User:` / `forge:` lines (`forge:` when `bot_id` is set or `user == bot_user_id`), strips mention
  tokens, drops empties, truncates each to a bounded length.

## 7. Multi-turn context, thread ownership & build handoff (`slackbot.py`)

- New injected `chat_reply(transcript, latest)` callable (default → `help_blurb()`; wired in `build_app`
  to `slackchat.generate_reply`). Mirrors how `qa_answer` / `opener` are injected.
- `_chat_history(channel, root_ts, latest)` — fetches recent messages (`conversations_replies(ts=root_ts)`
  in a channel; `conversations_history` reversed in a DM), formats via `format_transcript`, caps to the
  last ~12, and drops the trailing line if it is the triggering `latest` message. Wrapped in try/except →
  `""` on any failure (single-turn fallback).
- `_chat(channel, text, root_ts=None)` — builds the transcript, gets the reply, posts it
  (`thread_ts=root_ts`), and in a **channel** (`root_ts is not None`) records `root_ts` in a new
  `_chat_threads` set so follow-up replies are picked up.
- `_owns_thread` also returns true for `thread_ts in _chat_threads`.
- **Build handoff comes for free:** continuation re-runs the classifier. A chat-thread reply with a build
  verb classifies `build` → `_new_session` anchored at the thread root → the thread becomes a run thread
  (the `run_for_thread` check precedes the chat path on later turns).
- `_thread_command_or_turn`: the in-session mode map `("qa", "help")` → `("qa", "chat")` (a meta reply
  inside an active build session stays an in-repo answer turn — unchanged behavior, renamed intent).

## 8. Surfaces

DMs (every message handled) and channel threads forge started or was `@forge`'d into — matching the
existing bot. Chat is gated to the allowed user like every other instruction.

## 9. Dependency / scope note

`conversations.history` / `conversations.replies` need the `*:history` read scopes. Message-event ingress
already requires `im:history` / `channels:history`, so these are almost certainly granted. If a scope is
missing the fetch fails → history is `""` → chat still replies single-turn (graceful, no crash).

## 10. Out of scope (YAGNI)

- Task carry-over when a build can't resolve a repo (a pre-existing limitation, untouched).
- A "typing…"/thinking ack before the reply (replies are short; can add later if latency bites).
- Filtering forge's own build-progress messages out of DM history (model tolerates them).

## 11. Testing

- `slackmsg`: `classify_intent` — "how do you work"/"what can you do"/greeting/"thanks" → `chat`; build
  verb → `build`; PR ref → `review`; repo question → `qa`. `chat_prompt` contains persona facts +
  transcript + latest. `format_transcript` role mapping, mention stripping, truncation.
- `slackchat`: happy path (fake `run` returns worker JSON) → reply; timeout / non-zero / auth-error /
  empty → `fallback`.
- `slackbot`: `chat` intent posts the reply; `qa` non-high → chat (not "Which repo?"); channel chat
  thread ownership + continuation; build handoff from a chat thread; gate still blocks non-allowed users.

## 12. Files

- **New:** `src/forge/slackchat.py`, `tests/test_slackchat.py`.
- **Edit:** `src/forge/slackmsg.py` (+`tests/test_slackmsg.py`), `src/forge/slackbot.py`
  (+`tests/test_slackbot.py`).
