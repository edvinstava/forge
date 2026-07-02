from forge.reporesolve import RepoResolver, Resolution, score, parse_aliases

REPOS = [
    {"name": "landing-page", "full_name": "acme/landing-page", "description": "marketing site"},
    {"name": "webapp", "full_name": "acme/webapp", "description": "OS internal tool"},
    {"name": "chap-frontend", "full_name": "acme/chap-frontend", "description": "DHIS2 modeling app"},
    {"name": "landing-zone", "full_name": "acme/landing-zone", "description": "infra"},
]


def _resolver(**kw):
    return RepoResolver(repos_fn=lambda: REPOS, **kw)


def test_parse_aliases():
    txt = "# my shorthands\nOS: acme/webapp\n\nlp : acme/landing-page\n"
    aliases = parse_aliases(txt)
    assert aliases["os"] == "acme/webapp"
    assert aliases["lp"] == "acme/landing-page"


def test_alias_wins_high_confidence():
    r = _resolver(aliases={"os": "acme/webapp"})
    res = r.resolve("deploy OS please")
    assert res.slug == "acme/webapp"
    assert res.confidence == "high"


def test_fuzzy_token_match_high():
    r = _resolver()
    res = r.resolve("fix the landing page repo")
    assert res.slug == "acme/landing-page"
    assert res.confidence == "high"


def test_ambiguous_when_two_close():
    # "landing" matches both landing-page and acme/landing-zone -> ambiguous
    r = _resolver()
    res = r.resolve("the landing thing")
    assert res.confidence == "ambiguous"
    assert set(res.candidates) >= {"acme/landing-page", "acme/landing-zone"}
    assert res.slug is None


def test_llm_tiebreak_resolves_ambiguous():
    r = _resolver(llm=lambda phrase, shortlist: "acme/landing-page")
    res = r.resolve("the landing thing")
    assert res.slug == "acme/landing-page"
    assert res.confidence == "high"


def test_no_match_returns_none():
    r = _resolver()
    res = r.resolve("totally unrelated quantum widget")
    assert res.confidence == "none"
    assert res.slug is None


def test_acronym_match():
    r = _resolver()
    res = r.resolve("the OS internal tool")
    assert res.slug == "acme/webapp"


# --- Task 4: gh enumeration, cache, loaders ---

import json
from forge.reporesolve import gh_push_repos, RepoCache, load_aliases


class _Run:
    def __init__(self, stdout, code=0):
        self.stdout, self.returncode = stdout, code


def test_gh_push_repos_filters_push_permission():
    payload = json.dumps([
        {"name": "a", "full_name": "me/a", "description": "x",
         "permissions": {"push": True}},
        {"name": "b", "full_name": "me/b", "description": None,
         "permissions": {"push": False}},
    ])
    repos = gh_push_repos(run=lambda *a, **k: _Run(payload))
    assert [r["full_name"] for r in repos] == ["me/a"]
    assert repos[0]["description"] == "x"


def test_repo_cache_fetches_then_serves_from_disk(tmp_path):
    calls = []
    def fetch():
        calls.append(1)
        return [{"name": "a", "full_name": "me/a", "description": ""}]
    t = [1000.0]
    cache = RepoCache(tmp_path / "repos.json", ttl_secs=60,
                      fetch=fetch, clock=lambda: t[0])
    assert cache.repos()[0]["full_name"] == "me/a"
    t[0] = 1030.0                       # within ttl
    cache.repos()
    assert len(calls) == 1              # served from disk
    t[0] = 1100.0                       # past ttl
    cache.repos()
    assert len(calls) == 2              # refetched


def test_load_aliases_missing_file(tmp_path):
    assert load_aliases(tmp_path / "nope.yml") == {}


def test_gh_push_repos_includes_pushed_at():
    payload = json.dumps([
        {"name": "a", "full_name": "me/a", "description": "x",
         "pushed_at": "2026-07-01T00:00:00Z", "permissions": {"push": True}},
    ])
    repos = gh_push_repos(run=lambda *a, **k: _Run(payload))
    assert repos[0]["pushed_at"] == "2026-07-01T00:00:00Z"


# --- repo-match quality: stopwords, owner hint, recency, LLM-primary ---
# Root cause of the "horrible" matches: score() counted stopword overlap, the
# workspace/owner hint was ignored, and the LLM only ever saw a garbage top-3.

from forge.reporesolve import _owner_hint, explicit_slug, claude_tiebreak

# The real failing case, distilled: right target has an EMPTY description, the
# noise repos have chatty descriptions and/or live under other owners.
REPOS2 = [
    {"name": "webapp", "full_name": "acme/webapp",
     "description": "", "pushed_at": "2026-07-01T13:26:43Z"},
    {"name": "6PS-infra-docs", "full_name": "acme/6PS-infra-docs",
     "description": "This repository is made to give a description of the "
                    "server setup and procedures in case of any incidents",
     "pushed_at": "2025-02-01T15:52:50Z"},
    {"name": "chap-frontend", "full_name": "forge-dev/chap-frontend",
     "description": "DHIS2 modeling app", "pushed_at": "2026-06-20T00:00:00Z"},
    {"name": "tdt4173-project", "full_name": "frederikfarstad/tdt4173-project",
     "description": "a machine learning project for the course",
     "pushed_at": "2024-01-01T00:00:00Z"},
    {"name": "landing-page", "full_name": "acme/landing-page",
     "description": "marketing site", "pushed_at": "2026-06-09T08:28:01Z"},
    {"name": "fhi-enable-sms", "full_name": "acme/fhi-enable-sms",
     "description": "", "pushed_at": "2026-06-15T08:03:47Z"},
]

MSG2 = ("Hi Forge! I want the OS admin to be able to update and edit the initial "
        "offer. Currently, the admin can't update the initial offer. This would be "
        "nice to have, if the admin needs to change something or refresh an offer. "
        "It's for the webapp project in the acme workspace. The original "
        "offer should then be rejected in some way (redacted by OS is perhaps "
        "better?) and a new offer sent to the end user. To maintain a history of "
        "the events.")


def _resolver2(**kw):
    return RepoResolver(repos_fn=lambda: REPOS2, **kw)


def test_score_ignores_stopwords():
    # a description made only of stopwords must not score against prose
    repo = {"name": "notes", "full_name": "x/notes",
            "description": "this is a set of the notes and info to be read in the"}
    phrase = ("this is a request to update the offer and to change the admin in "
              "the app for the new user")
    assert score(phrase, repo) == 0


def test_score_rewards_content_over_stopword_noise():
    named = {"name": "webapp", "full_name": "acme/webapp",
             "description": ""}
    chatty = {"name": "infra-docs", "full_name": "acme/infra-docs",
              "description": "this is a repo with a description of the server "
                             "and of the setup and of the incidents"}
    phrase = "update the initial offer in the webapp project"
    assert score(phrase, named) > score(phrase, chatty)


def test_owner_hint_from_workspace_word():
    owners = {"acme", "forge-dev", "acme"}
    assert _owner_hint("edit the offer in the acme workspace", owners) == "acme"


def test_owner_hint_none_when_owner_unknown():
    assert _owner_hint("do it in the globex workspace", {"acme"}) is None


def test_owner_hint_none_when_absent():
    assert _owner_hint("just fix the landing page", {"acme"}) is None


def test_explicit_slug_detected():
    full = {"acme/webapp", "acme/landing-page"}
    assert explicit_slug("please work on acme/webapp now", full) \
        == "acme/webapp"


def test_explicit_slug_ignores_non_repo_paths():
    assert explicit_slug("use and/or logic here", {"acme/webapp"}) is None


def test_regression_webapp_picked_deterministically():
    # the horrible case: even with NO llm, the deterministic path must now pick
    # webapp rather than stopword/other-owner noise.
    res = _resolver2().resolve(MSG2)
    assert res.slug == "acme/webapp"
    assert res.confidence == "high"


def test_wrong_owner_repos_never_surface():
    res = _resolver2().resolve(MSG2)
    surfaced = set(res.candidates) | ({res.slug} if res.slug else set())
    assert not any(s.startswith(("forge-dev/", "frederikfarstad/"))
                   for s in surfaced)


def test_recency_leads_the_candidates_on_keyword_tie():
    repos = [
        {"name": "offer-old", "full_name": "acme/offer-old",
         "description": "", "pushed_at": "2020-01-01T00:00:00Z"},
        {"name": "offer-new", "full_name": "acme/offer-new",
         "description": "", "pushed_at": "2026-07-01T00:00:00Z"},
    ]
    res = RepoResolver(repos_fn=lambda: repos).resolve(
        "update the offer in the acme workspace")
    assert res.candidates[0] == "acme/offer-new"


def test_llm_picks_from_recency_menu_when_no_keyword_match():
    # phrase names no repo at all; the LLM must still receive a menu of recent
    # owner-scoped repos to choose from semantically.
    seen = []

    def llm(phrase, menu):
        seen.append([m["full_name"] for m in menu])
        return "acme/fhi-enable-sms"

    res = _resolver2(llm=llm).resolve(
        "we need to handle text-message notifications in the acme workspace")
    assert res.slug == "acme/fhi-enable-sms"
    assert res.confidence == "high"
    assert "acme/fhi-enable-sms" in seen[0]


def test_explicit_slug_resolves_high():
    res = _resolver2().resolve("please continue on acme/landing-page")
    assert res.slug == "acme/landing-page"
    assert res.confidence == "high"


def test_claude_tiebreak_prompt_includes_recency_and_desc():
    captured = {}

    def run(cmd, **kw):
        captured["prompt"] = cmd[-1]
        return _Run("acme/webapp\n")

    shortlist = [{"name": "webapp", "full_name": "acme/webapp",
                  "description": "OS internal tool",
                  "pushed_at": "2026-07-01T13:26:43Z"}]
    pick = claude_tiebreak("the offer thing", shortlist, run=run)
    assert pick == "acme/webapp"
    assert "2026-07-01" in captured["prompt"]
    assert "OS internal tool" in captured["prompt"]
