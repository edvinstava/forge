import re
from dataclasses import dataclass

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class RunSpec:
    repo: str
    task: str
    run_id: str
    branch: str


# Words that carry no meaning for a branch name: greetings, politeness,
# pronouns, and glue. Prompts arrive as chat ("hi, can you do this issue…"),
# so without this the filler crowds out the task within the length cap.
_FILLER = frozenset("""
    hi hey hello yo thanks thank thx please pls kindly
    can could would should will you u we i me my our us your
    it its this that these those there here what who
    a an the of on in at to for with and or as is are be been was were
    if so then just also some any very really
    do does did done doing go going make making
    need needs needed want wants wanted like help let lets
""".split())

_ISSUE_KEY_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{1,9}-\d{1,6})\b")
_URL_RE = re.compile(r"https?://\S+|\bwww\.\S+")
_NAME_MAX = 32


def _slug(task: str) -> str:
    """A fitting branch name from a free-form prompt: issue keys first (even
    when only inside a pasted URL), URLs and filler words dropped, truncated
    at a word boundary."""
    keys = [k.lower() for k in _ISSUE_KEY_RE.findall(task)]
    s = _ISSUE_KEY_RE.sub(" ", _URL_RE.sub(" ", task)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _FILLER]
    name = ""
    for w in dict.fromkeys(keys + words):    # dedupe, keep order
        cand = f"{name}-{w}" if name else w[:_NAME_MAX]
        if len(cand) > _NAME_MAX:
            break
        name = cand
    return name or "task"


def normalize_github_repo(value: str) -> str:
    """Reduce a user-supplied GitHub reference to a bare ``owner/name`` slug.

    Accepts a bare slug, an http(s) URL, an scp-style git URL
    (``git@host:owner/name``), or a ``host/owner/name`` string, each with an
    optional ``.git`` suffix, surrounding whitespace, or trailing path/slash.
    Raises ``ValueError`` if the result is not a valid ``owner/name``.
    """
    s = value.strip()
    s = re.sub(r"^[\w.+-]+@[^:/]+:", "", s)        # scp-style git@host:
    s = re.sub(r"^[a-zA-Z][\w+.-]*://", "", s)     # scheme://
    parts = [p for p in s.split("/") if p]
    if parts and "." in parts[0]:                  # leading host (github.com)
        parts = parts[1:]
    if len(parts) >= 2:
        owner, name = parts[0], re.sub(r"\.git$", "", parts[1])
        slug = f"{owner}/{name}"
        if _REPO_RE.match(slug):
            return slug
    raise ValueError(f"repo must be 'owner/name', got: {value!r}")


def make_runspec(repo: str, task: str, run_id: str) -> RunSpec:
    if not _REPO_RE.match(repo):
        raise ValueError(f"repo must be 'owner/name', got: {repo!r}")
    branch = f"forge/{run_id[:8]}/{_slug(task)}"
    return RunSpec(repo=repo, task=task, run_id=run_id, branch=branch)
