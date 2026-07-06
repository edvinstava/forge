import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from forge.configfile import parse_env_file


def config_file_path() -> Path:
    """Path to forge's optional config file: $FORGE_CONFIG, else ~/.forge/config.env."""
    override = os.environ.get("FORGE_CONFIG")
    return Path(override) if override else Path.home() / ".forge" / "config.env"


def load_config_file(path: Path | None = None) -> None:
    """Populate os.environ from the config file (env wins; missing file = no-op)."""
    p = path or config_file_path()
    try:
        text = p.read_text()
    except (OSError, UnicodeError):
        return
    try:
        if p.stat().st_mode & 0o077:
            print(f"forge config: {p} is group/world-readable; "
                  f"consider `chmod 600 {p}`", file=sys.stderr)
    except OSError:
        pass
    for key, value in parse_env_file(text).items():
        os.environ.setdefault(key, value)


def parse_mem_mb(s: str) -> int | None:
    """Parse a docker mem string ('8g','512m') to MB. None if empty/zero/unparseable
    (→ memory budget disabled)."""
    s = (s or "").strip().lower()
    if not s:
        return None
    try:
        if s.endswith("g"):
            return int(float(s[:-1]) * 1024)
        if s.endswith("m"):
            return int(float(s[:-1]))
        val = int(float(s))
    except ValueError:
        return None
    return val or None


@dataclass(frozen=True)
class Budget:
    max_iterations: int = 20
    max_wall_secs: int = 1800
    max_repair_iters: int = 4


@dataclass
class Config:
    runs_dir: Path
    image_tag: str = "forge-worker"
    budget: Budget = field(default_factory=Budget)
    provider: str = "claude"   # which agent CLI runs worker turns: claude | codex
    oauth_token: str = ""
    openai_api_key: str = ""   # codex API-key fallback (plan auth via ~/.codex wins)
    codex_auth: str = "auto"   # auto = prefer ChatGPT-plan login; api = force key
    gh_token: str = ""
    git_author_name: str = ""
    git_author_email: str = ""
    web_port: int = 3000
    health_timeout_secs: int = 90
    health_path: str = "/"
    # Contain a leaky dev server (next dev/vite under heavy HMR) so it can't grow
    # until it eats the whole host: a V8 old-space heap cap (clean JS OOM) plus a
    # hard container backstop. "" / 0 opts out. See recipe.apply_resource_limits.
    web_mem_limit: str = "8g"
    web_node_max_old_space_mb: int = 4096
    proxy_port: int = 8088
    proxy_domain: str = "forge.localhost"
    env_ttl_secs: int = 3600   # idle envs slept after 1h
    dormant_ttl_secs: int = 259200   # asleep envs deleted after 3 days
    compose_up_timeout_secs: int = 1200   # cap `compose up` (image pull/build) at 20m

    workspace_dir: Path = field(default_factory=lambda: Path.home() / "forge-repos")
    max_live_sessions: int = 4
    # Fire-and-forget batch queue (managed parallelism).
    mem_budget_mb: int = 0            # 0 = disabled (cap = max_live_sessions)
    queue_tick_secs: int = 5
    queue_stagger_secs: int = 2
    batch_max_items: int = 50
    supabase_port_stride: int = 100   # gap between per-run Supabase port blocks
    supabase_max_blocks: int = 20     # how many blocks to probe before giving up
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_allowed_user: str = ""
    # Public base URL of the forge web app itself (used to deep-link Slack
    # messages to the session: {forge_web_url}/#s={run_id}). `forge web`
    # defaults it to its own serving address when unset.
    forge_web_url: str = ""
    repo_aliases_path: str = field(
        default_factory=lambda: str(Path.home() / ".forge" / "aliases.yml"))
    repo_cache_ttl_secs: int = 3600
    knowledge_dir: Path = field(
        default_factory=lambda: Path.home() / ".forge" / "knowledge")
    self_heal: bool = True
    # Background self-heal of a Next dev server whose Turbopack cache corrupted
    # (every route 5xx): clear .next + restart the web service. Capped so a
    # genuinely-broken app isn't churned. Gated by self_heal above.
    web_heal_max_attempts: int = 2
    web_heal_cooldown_secs: int = 180
    probe_max_iterations: int = 6
    gh_app_id: str = ""
    gh_app_private_key_path: str = ""
    gh_app_slug: str = "forge"
    gh_webhook_secret: str = ""   # HMAC secret for /api/github/webhook
    public_url: str = ""          # stable public base URL (skips the quick tunnel)
    self_review: bool = True
    # auto = user authors + forge[bot] Co-Authored-By | forge = bot authors
    # outright | user = plain user, no trailer
    commit_identity: str = "auto"
    qa_gating: bool = True   # gate the PR on the plan's acceptance criteria
    learn: bool = True   # run a retrospective after a PR to learn per-repo lessons

    def __post_init__(self):
        # Bind-mount sources in generated compose files derive from runs_dir; Docker
        # Compose rejects a *relative* source (e.g. "runs/<id>/workspace") as an
        # undefined named volume, so keep runs_dir absolute regardless of how it was
        # passed (the CLI defaults --runs-dir to the relative "runs").
        self.runs_dir = Path(self.runs_dir).resolve()

    @classmethod
    def from_env(cls, runs_dir: Path) -> "Config":
        load_config_file()
        return cls(
            runs_dir=Path(runs_dir),
            budget=Budget(
                max_repair_iters=int(os.environ.get("FORGE_MAX_REPAIR_ITERS", "4"))),
            provider=os.environ.get("FORGE_PROVIDER", "claude"),
            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            codex_auth=os.environ.get("FORGE_CODEX_AUTH", "auto"),
            gh_token=os.environ.get("GH_TOKEN", ""),
            git_author_name=os.environ.get("FORGE_GIT_NAME", ""),
            git_author_email=os.environ.get("FORGE_GIT_EMAIL", ""),
            workspace_dir=Path(os.environ.get("FORGE_WORKSPACE_DIR",
                                               str(Path.home() / "forge-repos"))),
            max_live_sessions=int(os.environ.get("FORGE_MAX_SESSIONS", "4")),
            mem_budget_mb=int(os.environ.get("FORGE_MEM_BUDGET_MB", "0")),
            queue_tick_secs=int(os.environ.get("FORGE_QUEUE_TICK_SECS", "5")),
            queue_stagger_secs=int(os.environ.get("FORGE_QUEUE_STAGGER_SECS", "2")),
            batch_max_items=int(os.environ.get("FORGE_BATCH_MAX_ITEMS", "50")),
            web_mem_limit=os.environ.get("FORGE_WEB_MEM_LIMIT", "8g"),
            web_node_max_old_space_mb=int(
                os.environ.get("FORGE_WEB_NODE_MAX_OLD_SPACE_MB", "4096")),
            supabase_port_stride=int(os.environ.get("FORGE_SUPABASE_PORT_STRIDE", "100")),
            supabase_max_blocks=int(os.environ.get("FORGE_SUPABASE_MAX_BLOCKS", "20")),
            env_ttl_secs=int(os.environ.get("FORGE_ENV_TTL_SECS", "3600")),
            dormant_ttl_secs=int(os.environ.get("FORGE_DORMANT_TTL_SECS", "259200")),
            compose_up_timeout_secs=int(
                os.environ.get("FORGE_COMPOSE_UP_TIMEOUT_SECS", "1200")),
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            slack_allowed_user=os.environ.get("SLACK_ALLOWED_USER", ""),
            forge_web_url=os.environ.get("FORGE_WEB_URL", ""),
            repo_aliases_path=os.environ.get(
                "FORGE_REPO_ALIASES", str(Path.home() / ".forge" / "aliases.yml")),
            repo_cache_ttl_secs=int(os.environ.get("FORGE_REPO_CACHE_TTL", "3600")),
            knowledge_dir=Path(os.environ.get(
                "FORGE_KNOWLEDGE_DIR", str(Path.home() / ".forge" / "knowledge"))),
            self_heal=os.environ.get("FORGE_SELF_HEAL", "1") not in ("0", "false", "no"),
            web_heal_max_attempts=int(
                os.environ.get("FORGE_WEB_HEAL_MAX_ATTEMPTS", "2")),
            web_heal_cooldown_secs=int(
                os.environ.get("FORGE_WEB_HEAL_COOLDOWN_SECS", "180")),
            probe_max_iterations=int(os.environ.get("FORGE_PROBE_MAX_ITERATIONS", "6")),
            gh_app_id=os.environ.get("FORGE_GH_APP_ID", ""),
            gh_app_private_key_path=os.environ.get("FORGE_GH_APP_KEY", ""),
            gh_app_slug=os.environ.get("FORGE_GH_APP_SLUG", "forge"),
            gh_webhook_secret=os.environ.get("FORGE_GH_WEBHOOK_SECRET", ""),
            public_url=os.environ.get("FORGE_PUBLIC_URL", ""),
            self_review=os.environ.get("FORGE_SELF_REVIEW", "1")
                not in ("0", "false", "no"),
            commit_identity=os.environ.get("FORGE_COMMIT_IDENTITY", "auto"),
            qa_gating=os.environ.get("FORGE_QA_GATING", "1") not in ("0", "false", "no"),
            learn=os.environ.get("FORGE_LEARN", "1") not in ("0", "false", "no"),
        )
