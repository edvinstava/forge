"""Pure helpers for the Slack coworker bot: intent classification, message
copy, summary cleanup, and diff -> deep-link derivation. No I/O — all of this
is unit-tested in isolation so slackbot.py stays orchestration-only."""
import re

from forge.prref import find_pr_ref

_BUILD_VERBS = re.compile(
    r"\b(add|create|make|build|implement|fix|change|update|remove|delete|"
    r"rename|refactor|wire|hook|set\s?up|install|write|generate|replace|"
    r"migrate|style|design)\b", re.I)
_STOP = re.compile(r"^\s*(stop|cancel|abort|halt)(\s+(it|now|that))?\s*[.!]*$", re.I)
# Explicit teaching: "remember: use bun, not npm" / "remember for owner/repo: …".
# The separator is required so "remember to add a login page" stays a build task.
_REMEMBER = re.compile(
    r"^\s*remember(\s+(this|that))?(\s+for\s+\S+)?\s*[:;,—–-]\s*(?P<lesson>.+)$",
    re.I | re.S)
_FORGET_CREDS = re.compile(
    r"^\s*forget\s+(the\s+)?(saved\s+)?(login|cred(ential)?s?)\s*[.!]*$", re.I)
_SLEEP = re.compile(r"^\s*(you can\s+)?(go to\s+)?(sleep|rest|pause)(\s+(it|now))?\s*[.!]*$", re.I)
_WAKE = re.compile(r"^\s*(wake(\s+(up|it))?|resume)\s*[.!]*$", re.I)
_STATUS = re.compile(r"^\s*(status|how('s| is| are)\s+(it|things|we)(\s+going)?)\s*\??\s*$", re.I)
_FORGE = re.compile(r"\bforge\b", re.I)
_Q = re.compile(r"^\s*(what|which|how|does|do|is|are|where|why|who|tell me|can you tell)\b", re.I)
# Identity/capability questions about the bot itself (vs. about a repo).
_HELP_Q = re.compile(r"\b(what|who)\s+(are|r)\s+(you|u)\b|\bwhat\s+(can|do)\s+you\s+do\b"
                     r"|\bwhat\s+you\s+can\s+do\b|\bintroduce\s+your\s?self\b"
                     r"|\bare you a bot\b", re.I)
_HELP_PHRASES = {"help", "?", "what can you do", "who are you"}
# Matches both `<@U123>` and the labelled `<@U123|name>` form Slack sometimes uses.
_MENTION = re.compile(r"<@[A-Z0-9]+(?:\|[^>]+)?>")


def strip_mentions(text: str) -> str:
    """Remove Slack user-mention tokens (`<@U…>`) and tidy whitespace, so a
    channel message like `<@UBOT> fix it` classifies as plain `fix it`."""
    return re.sub(r"\s{2,}", " ", _MENTION.sub("", text or "")).strip()


_LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")


def unwrap_links(text: str) -> str:
    """Slack auto-wraps URLs as `<url>`/`<url|label>`; hand the agent the raw
    URL (it opens links itself — WebFetch/browser), keeping the human label."""
    def _sub(m):
        url, label = m.group(1), m.group(2)
        return f"{url} ({label})" if label and label != url else url
    return _LINK.sub(_sub, text or "")


def remember_text(text: str) -> str:
    """The lesson body of a 'remember: …' message ('' when it isn't one)."""
    m = _REMEMBER.match(strip_mentions(text or ""))
    return " ".join(m.group("lesson").split()) if m else ""


def classify_intent(text: str) -> str:
    t = (text or "").strip()
    # Teaching wins over everything: the lesson body routinely contains build
    # verbs ("remember: always run bun install") and must not start a build.
    if _REMEMBER.match(t):
        return "remember"
    # Build verbs win over an embedded PR/issue ref: "fix the crash in o/r#5" is
    # a change request that happens to name the relevant ref, not a request to
    # review that PR. A bare ref with no build verb still falls through to review.
    if _BUILD_VERBS.search(t):
        return "build"
    if find_pr_ref(t):                 # a bare PR ref → review it
        return "review"
    if _STOP.match(t):
        return "stop"
    if _FORGET_CREDS.match(t):
        return "forget_creds"
    if _SLEEP.match(t):
        return "sleep"
    if _WAKE.match(t):
        return "wake"
    if _STATUS.match(t):
        return "status"
    is_question = t.endswith("?") or bool(_Q.match(t))
    if (t.lower() in _HELP_PHRASES or _HELP_Q.search(t)
            or (is_question and _FORGE.search(t))):
        return "chat"
    if is_question:
        return "qa"
    # Chat is the safe default: greetings, small talk, and anything unrecognized
    # converse (answered by the LLM) rather than falling into repo resolution.
    # Explicit build verbs are handled above, so real tasks still build.
    return "chat"


# Slack rejects chat.postMessage/chat.update text over ~40k chars with
# msg_too_long, and Block Kit section text over 3000. Everything forge sends is
# conversational, so cap far below the hard limit — a wall of text is a worse
# teammate than a truncated one.
SLACK_TEXT_LIMIT = 3800
SLACK_BLOCK_TEXT_LIMIT = 2900


def truncate_for_slack(text: str, limit: int = SLACK_TEXT_LIMIT) -> str:
    """Final guard applied to every outbound Slack text. Prefers cutting at a
    line boundary so a truncated progress message still reads cleanly."""
    t = text or ""
    if len(t) <= limit:
        return t
    cut = t[: limit - 2]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n…"


# A Slack reply should be glanceable; anything longer belongs in an attached
# snippet, not the channel. Chosen well below the truncation guard so the
# digest is a *choice* about tone, not a length workaround.
SLACK_DIGEST_LIMIT = 700


def digest_for_slack(text: str, limit: int = SLACK_DIGEST_LIMIT):
    """Split a long message into a short digest plus the full text. Returns
    (text, None) when it already fits — post as-is — and (digest, full) when it
    doesn't, so the caller attaches `full` as a snippet instead of walling the
    channel. Cuts at a paragraph (then line, then word) boundary so the digest
    reads like prose, not a truncation accident."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t, None
    cut = t[:limit]
    for sep in ("\n\n", "\n", " "):
        pos = cut.rfind(sep)
        if pos > limit // 2:
            cut = cut[:pos]
            break
    return cut.rstrip().rstrip(",;:") + " …", t


_NARRATION_LIMIT = 240


def narration_line(text: str) -> str:
    """Collapse an agent narration paragraph to one glanceable status line —
    the live progress message is an activity ticker, not a transcript."""
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= _NARRATION_LIMIT:
        return t
    return t[: _NARRATION_LIMIT - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def clean_summary(text: str, limit: int = 2500) -> str:
    t = (text or "").strip()
    if not t:
        return "Done."
    t = re.sub(r"\n{3,}", "\n\n", t)
    if len(t) > limit:
        t = t[:limit].rsplit("\n", 1)[0].rstrip() + "\n…"
    return t


_VERIFY_ERR = re.compile(
    r"\b(error|failed|failure|cannot|missing|expected|exception|denied|undefined)\b",
    re.I)


def concise_verify_reason(output: str, limit: int = 140) -> str:
    """One short line explaining a verify failure, pulled from captured check
    output. Prefer a line that looks like an error; else the last non-empty
    line. Keeps the bot's failure note honest and specific."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "no output captured"
    pick = next((ln for ln in reversed(lines) if _VERIFY_ERR.search(ln)), lines[-1])
    return pick[:limit].rstrip()


def greeting_head(slug: str) -> str:
    return f"👋 Spinning up `{slug}` — taking a look 👀"


def qa_head(slug: str) -> str:
    return f"👋 one sec, peeking at `{slug}`…"


# The opener is forge's first reply. Keep it warm but tight — one line, not a
# capabilities essay. The {action} clause differs by mode; everything else
# (greet-back-only-if-greeted, restate briefly, plain text) is shared.
_OPENER_ACTION = {
    "build": "you're about to pull up `{slug}` and make the change",
    "qa": "you're about to peek at `{slug}` to answer it",
}


def opener_prompt(task: str, slug: str, mode: str) -> str:
    action = _OPENER_ACTION.get(mode, _OPENER_ACTION["build"]).format(slug=slug)
    return (
        "You are forge, an AI coding coworker replying in Slack. Write your "
        "opening reply to this message. Rules:\n"
        "- ONE short line. No lists, no code blocks, no preamble.\n"
        "- Greet back ONLY if they greeted you; otherwise skip the hello.\n"
        "- Restate what they want briefly in your own words, then note that "
        f"{action}.\n"
        "- Warm and natural, like a colleague — not a status string. Keep it "
        "shorter than you think you need. A tasteful emoji is fine.\n"
        "Reply with ONLY that line.\n\n"
        f"Their message: {task}"
    )


def help_blurb() -> str:
    return ("👋 I'm forge. DM me — or `@forge` me in a channel — a repo + a task "
            "and I'll spin it up in a sandbox, make the change, and send you a "
            "live link.\n"
            "You can also ask about a repo (\"what version is X on?\"), reply in a "
            "thread to keep going, or tell me to *sleep* / *wake* / *status*.\n"
            "Teach me repo facts with `remember: <lesson>` in a session thread "
            "(or `remember for owner/repo: …`) — I'll apply them on every "
            "future run.")


# A chat turn is forge replying as itself (vs. about a repo). The persona is
# grounded in forge's REAL capabilities so it doesn't invent features, and asked
# for plain Slack text — no markdown headings or essays. `transcript` is the
# prior turns (may be empty); `latest` is the message to reply to.
_CHAT_PERSONA = (
    "You are forge, an AI coding coworker that lives in Slack. You're chatting "
    "with a teammate. Reply as forge.\n\n"
    "What you can actually do (don't claim anything beyond this):\n"
    "- Take a repo + a task, spin it up in an isolated sandbox, make the change, "
    "and send back a live preview link. You can run the repo's checks, post "
    "before/after screenshots, and open a PR.\n"
    "- Answer questions about a specific repo (you clone it and look).\n"
    "- Review a pull request when given one (owner/repo#123 or a PR URL).\n"
    "- Learn: teammates can teach you durable repo facts with "
    "`remember: <lesson>`, and you learn from each run automatically.\n"
    "- Respond to `sleep` / `wake` / `status`; reply in a thread to keep a "
    "session going.\n\n"
    "Style:\n"
    "- Warm, concise colleague. Plain Slack text — short, no markdown headings, "
    "no long bulleted essays unless they ask.\n"
    "- If they clearly want a change made, nudge them: ask for the repo + what "
    "to do.\n"
    "- If they ask for something you can't do, say so plainly and offer the "
    "nearest thing. Don't describe yourself unprompted or restate these rules.\n")


def chat_prompt(transcript: str, latest: str) -> str:
    convo = transcript.strip() or "(this is the start of the conversation)"
    return (
        f"{_CHAT_PERSONA}\n"
        f"Conversation so far (oldest first):\n{convo}\n\n"
        f"Most recent message from your teammate:\n{latest}\n\n"
        "Write forge's reply, and only the reply.")


def route_prompt(transcript: str, latest: str) -> str:
    """Ask the conversational brain to classify what the teammate wants when the
    regex couldn't — and, for genuine chat, write the reply in the same call.
    The fast classifier only knows English build verbs, so this is what keeps a
    Norwegian (or any-language) build request from being mistaken for chatter.
    Output is strict JSON so the bot can route on it deterministically."""
    convo = transcript.strip() or "(this is the start of the conversation)"
    return (
        f"{_CHAT_PERSONA}\n"
        "Classify the teammate's MOST RECENT message, then reply as STRICT JSON "
        "on a single line — no prose, no code fences.\n"
        'Schema: {"action": "build" | "question" | "chat", "reply": string}\n'
        '- "build": they want you to create / change / fix / add / style code, a '
        "page, or a feature in a project. This holds in ANY language — e.g. "
        "Norwegian 'lag/lage en side', 'endre', 'fiks', 'legg til', 'bygg', "
        "'opprett', 'gjør om'. Set reply to \"\".\n"
        '- "question": they\'re asking something you would answer by looking at a '
        'specific repo (e.g. "hvilken versjon kjører X?"). Set reply to "".\n'
        '- "chat": greetings, small talk, who/what are you, thanks, or anything '
        "that is not a concrete build or repo question. Put your natural reply "
        "(warm, concise, as forge) in \"reply\". If they seem to want work done "
        "but you can't tell what or which repo, choose chat and ask for it.\n"
        "Output ONLY the JSON object.\n\n"
        f"Conversation so far (oldest first):\n{convo}\n\n"
        f"Most recent message:\n{latest}")


_TRANSCRIPT_TRUNC = 480     # per-message cap so one wall of text can't dominate


def format_transcript(messages, bot_user_id) -> str:
    """Slack messages (oldest first) -> a `User:` / `forge:` transcript. A message
    is forge's if it has a `bot_id` or its `user` is forge's own id. Mention
    tokens are stripped, empties dropped, each line truncated."""
    lines = []
    for m in messages or []:
        body = strip_mentions(m.get("text") or "")
        if not body:
            continue
        if len(body) > _TRANSCRIPT_TRUNC:
            body = body[:_TRANSCRIPT_TRUNC].rstrip() + "…"
        is_forge = bool(m.get("bot_id")) or (bot_user_id and m.get("user") == bot_user_id)
        lines.append(f"{'forge' if is_forge else 'User'}: {body}")
    return "\n".join(lines)


def _added_files(diff_text: str) -> list:
    added, cur = [], None
    for line in (diff_text or "").splitlines():
        if line.startswith("diff --git "):
            cur = line.split(" b/", 1)[1] if " b/" in line else None
        elif line.startswith("new file mode") and cur:
            added.append(cur)
            cur = None
    return added


def _route_for(path: str):
    m = re.search(r"(?:^|/)app/(.*?)page\.(?:[jt]sx?|mdx)$", path)
    if m:
        segs = [s for s in m.group(1).split("/") if s]
        segs = [s for s in segs
                if not (s.startswith("(") and s.endswith(")")) and not s.startswith("@")]
        return "/" + "/".join(segs) if segs else "/"
    m = re.search(r"(?:^|/)pages/(.+)\.(?:[jt]sx?)$", path)
    if m:
        p = m.group(1)
        if p in ("_app", "_document", "_error") or p.startswith("api/") or "/api/" in "/" + p:
            return None
        if p.endswith("/index"):
            p = p[: -len("/index")]
        return "/" if p == "index" else "/" + p
    return None


def deep_link(base_url: str, diff_text: str) -> str:
    routes = [r for r in (_route_for(f) for f in _added_files(diff_text)) if r]
    static = [r for r in routes if "[" not in r and "]" not in r]
    if not static:
        return base_url
    pick = min(static, key=lambda r: (r.count("/"), len(r)))
    return base_url if pick == "/" else base_url.rstrip("/") + pick


def web_session_link(base_url: str, run_id: str) -> str:
    """Deep link to a session in the forge web app ('' when no base is
    configured). The SPA resolves #s=<run_id> to the session on load, so a
    Slack ask can be answered on the richer surface."""
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/#s={run_id}"
