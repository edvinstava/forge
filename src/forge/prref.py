"""Parse a user-supplied PR reference into owner/repo/number. Pure logic."""
import re
from dataclasses import dataclass

# Owner/repo must start with an alphanumeric/underscore so a dash-leading slug
# can never be smuggled as a CLI flag downstream (e.g. `gh repo clone -foo`).
_SEG = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
_HASH = re.compile(rf"^({_SEG})/({_SEG}?)(?:\.git)?#(\d+)$")
_URL = re.compile(rf"github\.com/({_SEG})/({_SEG}?)(?:\.git)?/pull/(\d+)")


@dataclass(frozen=True)
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_pr_ref(text: str) -> PRRef:
    s = (text or "").strip()
    m = _HASH.match(s) or _URL.search(s)
    if not m:
        raise ValueError(f"expected owner/repo#N or a PR URL, got: {text!r}")
    return PRRef(m.group(1), m.group(2), int(m.group(3)))


# Search form: find a PR ref embedded in free text (e.g. a Slack message).
_FIND = re.compile(rf"\b{_SEG}/{_SEG}#\d+|github\.com/{_SEG}/{_SEG}/pull/\d+")


def find_pr_ref(text: str):
    """Return the first PRRef embedded anywhere in `text`, or None."""
    m = _FIND.search(text or "")
    return parse_pr_ref(m.group(0)) if m else None
