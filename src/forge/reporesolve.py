"""Free-text phrase -> owner/repo. Ladder: alias map -> fuzzy rank -> LLM
tiebreak. Pure logic; gh enumeration + caching (repos_fn) and the LLM call
are injected so tests stay hermetic."""
import re
from dataclasses import dataclass


@dataclass
class Resolution:
    slug: str | None
    confidence: str          # "high" | "ambiguous" | "none"
    candidates: list


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokens(s):
    return set(_norm(s).split()) - {""}


# Function words + generic dev/request filler. These carry no signal about
# *which* repo a request means, yet a chatty repo description full of them used
# to out-score the repo the user literally named. Stripped from both sides
# before overlap scoring. Deliberately conservative: words that can actually
# distinguish a repo (frontend, page, site, tool, offer, admin, ...) stay in.
STOPWORDS = {
    # articles / determiners / quantifiers
    "a", "an", "the", "this", "that", "these", "those", "some", "any", "all",
    "each", "every", "no", "both", "few", "more", "most", "other", "such",
    # pronouns
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "our", "their", "its", "his", "who", "what", "which",
    # conjunctions / prepositions / adverbs
    "and", "or", "but", "so", "if", "then", "than", "as", "of", "to", "in",
    "on", "at", "by", "for", "with", "from", "into", "about", "over", "under",
    "between", "within", "without", "via", "per", "up", "down", "out", "off",
    "again", "also", "here", "there", "when", "where", "while", "because",
    # auxiliaries / modals
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does",
    "did", "have", "has", "had", "having", "will", "would", "shall", "should",
    "can", "could", "may", "might", "must", "need", "needs", "want", "wants",
    # generic request verbs / adjectives
    "make", "add", "adds", "added", "adding", "update", "updates", "updated",
    "edit", "edits", "change", "changes", "changed", "fix", "fixes", "new",
    "please", "let", "get", "use", "using", "like", "nice", "able", "currently",
    "perhaps", "better", "way", "just", "now", "still", "maintain", "keep",
    # filler nouns
    "thing", "things", "stuff", "something", "someone", "somewhere",
    # generic dev nouns that rarely disambiguate a repo
    "app", "apps", "repo", "repos", "repository", "project", "projects",
    "code", "codebase",
    # greetings / the bot's own name
    "hi", "hey", "hello", "thanks", "forge",
    # contraction fragments left after _norm splits "can't", "it's", "we're"
    "s", "t", "re", "ll", "ve", "m", "d",
}

# owner/repo written out explicitly, e.g. "acme/webapp"
_SLUG_RE = re.compile(r"(?<![\w./])([A-Za-z0-9][\w.-]*/[\w.-]+)")

# a GitHub owner named alongside "workspace"/"org"/"account", e.g.
# "in the acme workspace" -> acme
_OWNER_RE = re.compile(
    r"\b([A-Za-z0-9][\w.-]*)\s+"
    r"(?:workspace|workspaces|org|orgs|organi[sz]ation|account)\b",
    re.IGNORECASE,
)


def _keywords(s):
    """Content tokens: whole-word tokens with stopwords removed."""
    return _tokens(s) - STOPWORDS


def _owner_of(full_name):
    return (full_name or "").split("/", 1)[0].lower()


def _recency_key(repo):
    # ISO-8601 timestamps sort lexicographically; missing -> oldest.
    return repo.get("pushed_at") or ""


def _owner_hint(phrase, known_owners):
    """Extract an owner/workspace hint from a phrase, but only honour it when it
    names an owner we actually have repos for (so a stray word can't wipe out
    the whole candidate set)."""
    known = {o.lower() for o in known_owners}
    for m in _OWNER_RE.finditer(phrase or ""):
        cand = m.group(1).lower()
        if cand in known:
            return cand
    return None


def explicit_slug(phrase, known_full_names):
    """Return an owner/repo slug written verbatim in the phrase, if it matches a
    real repo. Ignores incidental slashes like 'and/or'."""
    known = set(known_full_names)
    for m in _SLUG_RE.finditer(phrase or ""):
        if m.group(1) in known:
            return m.group(1)
    return None


def parse_aliases(text):
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        if k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


def score(phrase, repo):
    pt = _keywords(phrase)
    if not pt:
        return 0
    name_t = _keywords(repo["name"])
    desc_t = _keywords(repo.get("description") or "")
    overlap_name = len(pt & name_t)
    overlap_desc = len(pt & desc_t)
    name_norm = _norm(repo["name"])
    sub = 1 if (name_norm and name_norm in _norm(phrase)) else 0
    acro = "".join(w[0] for w in name_norm.split() if w)
    acr = 1 if (acro and acro in pt) else 0
    return overlap_name * 3 + overlap_desc * 1 + sub * 3 + acr * 2


class RepoResolver:
    MARGIN = 2      # top must beat 2nd by > MARGIN to be a clear lexical winner
    MENU_N = 10     # how many candidates the LLM gets to choose among

    def __init__(self, repos_fn, aliases=None, llm=None, top_n=3, menu_n=None):
        self._repos_fn = repos_fn
        self._aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self._llm = llm
        self._top_n = top_n
        self._menu_n = menu_n or self.MENU_N

    def resolve(self, phrase):
        repos = self._repos_fn()

        # 1. explicit "owner/repo" written verbatim -> unambiguous.
        slug = explicit_slug(phrase, {r["full_name"] for r in repos})
        if slug:
            return Resolution(slug, "high", [slug])

        # 2. alias map (exact token hit).
        pt = _tokens(phrase)
        for alias, aslug in self._aliases.items():
            if alias in pt or alias == _norm(phrase):
                return Resolution(aslug, "high", [aslug])

        # 3. owner/workspace hint restricts the candidate pool (ignored unless
        #    it names an owner we actually have repos for).
        owner = _owner_hint(phrase, {_owner_of(r["full_name"]) for r in repos})
        pool = [r for r in repos if _owner_of(r["full_name"]) == owner] \
            if owner else repos

        # 4. rank by keyword score, breaking ties toward recently-pushed repos.
        ranked = sorted(pool, key=lambda r: (score(phrase, r), _recency_key(r)),
                        reverse=True)
        hits = [r for r in ranked if score(phrase, r) > 0]

        # 5. a dominant lexical match is trustworthy on its own — skip the LLM.
        if hits:
            top = score(phrase, hits[0])
            second = score(phrase, hits[1]) if len(hits) > 1 else 0
            if top - second > self.MARGIN:
                return Resolution(hits[0]["full_name"], "high",
                                  [hits[0]["full_name"]])

        # 6. otherwise let the LLM pick from a generous menu. When nothing
        #    matched lexically but we're scoped to an owner, hand it the most
        #    recent repos in that owner so it can still match semantically
        #    (e.g. a feature described without naming the repo).
        if hits:
            menu = hits[:self._menu_n]
        elif owner:
            menu = ranked[:self._menu_n]
        else:
            menu = []
        if self._llm is not None and menu:
            pick = self._llm(phrase, menu)
            if pick in {r["full_name"] for r in menu}:
                return Resolution(pick, "high", [pick])

        # 7. deterministic fallback: no signal -> none; else present a short,
        #    recency-ordered "did you mean" list.
        if not hits:
            return Resolution(None, "none", [])
        return Resolution(None, "ambiguous",
                          [r["full_name"] for r in hits[:self._top_n]])


# --- gh enumeration, disk cache, default LLM + alias loaders ---

import json
import subprocess
import time
from pathlib import Path


def gh_push_repos(run=subprocess.run):
    r = run(["gh", "api", "--paginate",
             "user/repos?per_page=100&affiliation=owner,collaborator,organization_member"],
            capture_output=True, text=True)
    if getattr(r, "returncode", 1) != 0 or not r.stdout.strip():
        return []
    out = []
    for item in json.loads(r.stdout):
        if (item.get("permissions") or {}).get("push"):
            out.append({"name": item["name"], "full_name": item["full_name"],
                        "description": item.get("description") or "",
                        "pushed_at": item.get("pushed_at") or ""})
    return out


class RepoCache:
    def __init__(self, path, ttl_secs, fetch, clock=time.time):
        self._path = Path(path)
        self._ttl = ttl_secs
        self._fetch = fetch
        self._clock = clock

    def repos(self):
        now = self._clock()
        if self._path.is_file():
            try:
                blob = json.loads(self._path.read_text())
                if now - blob.get("ts", 0) <= self._ttl:
                    return blob["repos"]
            except Exception:
                pass
        return self.refresh()

    def refresh(self):
        repos = self._fetch()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"ts": self._clock(), "repos": repos}))
        return repos


def claude_tiebreak(phrase, shortlist, run=subprocess.run):
    def _line(r):
        parts = [f"- {r['full_name']}"]
        if r.get("description"):
            parts.append(f": {r['description']}")
        if r.get("pushed_at"):
            parts.append(f"  (last updated {r['pushed_at'][:10]})")
        return "".join(parts)

    lines = "\n".join(_line(r) for r in shortlist)
    prompt = (
        "You are matching a user's request to the repository they mean. "
        "Consider what the request is about, not just shared words — the repo "
        "name may not appear in the request at all. A more recently updated "
        "repo is more likely to be the one they're actively working on. "
        "Reply with ONLY the full_name (owner/repo) of the single best match, "
        "or NONE if none clearly fits.\n\n"
        f"Request: {phrase}\n\nRepositories:\n{lines}\n")
    try:
        r = run(["claude", "-p", prompt], capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    pick = (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout else ""
    valid = {x["full_name"] for x in shortlist}
    return pick if pick in valid else None


def load_aliases(path):
    p = Path(path)
    if not p.is_file():
        return {}
    return parse_aliases(p.read_text())


def build_resolver(cfg):
    cache = RepoCache(Path(cfg.runs_dir) / "cache" / "repos.json",
                      cfg.repo_cache_ttl_secs, gh_push_repos)
    return RepoResolver(repos_fn=cache.repos,
                        aliases=load_aliases(cfg.repo_aliases_path),
                        llm=claude_tiebreak)
