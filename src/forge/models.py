"""Model selection for worker turns.

Forge lets a session pick which Claude model runs a turn. The UI offers four
choices (see ``MODEL_CHOICES``): an ``auto`` mode plus the three aliases the
Claude CLI understands. ``auto`` applies a small, transparent heuristic so that
trivial edits run on a fast model and heavy engineering runs on the strong one —
the user keeps full manual override at any time.

We pass the *alias* ("opus" / "sonnet" / "haiku") to ``claude --model`` rather
than a pinned version id, so the CLI always resolves to the current release.
"""

# Advertised to the UI; `auto` is first so it reads as the default.
MODEL_CHOICES = ["auto", "opus", "sonnet", "haiku"]
_ALIASES = {"opus", "sonnet", "haiku"}

# Words that signal substantial engineering → strong model.
_HEAVY = (
    "implement", "refactor", "architect", "design", "redesign", "rewrite",
    "debug", "migrat", "optimi", "performance", "security", "vulnerab",
    "concurren", "race condition", "algorithm", "deadlock", "build a",
)
# Words that signal a trivial, mechanical edit → fast model.
_LIGHT = (
    "typo", "rename", "comment", "docstring", "readme", "spelling", "wording",
    "format", "lint", "whitespace", "changelog", "bump", "copy edit",
)


def auto_model(prompt: str) -> str:
    """Heuristically choose a model for a turn from the prompt text.

    Heavy keywords win first (a "refactor the typo-checker" is still heavy);
    otherwise short prompts that look mechanical go to haiku, and everything
    else lands on the balanced default, sonnet.
    """
    t = (prompt or "").lower()
    if any(k in t for k in _HEAVY):
        return "opus"
    if len(t) <= 140 and any(k in t for k in _LIGHT):
        return "haiku"
    return "sonnet"


def resolve_model(choice: str | None, prompt: str) -> str:
    """Map a UI choice + prompt to a concrete CLI model alias.

    An explicit alias is honored verbatim; ``auto``, empty, or anything
    unrecognized defers to the heuristic so a bad value never aborts a turn.
    """
    if choice in _ALIASES:
        return choice
    return auto_model(prompt)
