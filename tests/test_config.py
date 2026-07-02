from pathlib import Path
from forge.config import Config, Budget


def test_defaults(tmp_path):
    cfg = Config(runs_dir=tmp_path)
    assert cfg.budget.max_iterations == 20
    assert cfg.budget.max_wall_secs == 1800
    assert cfg.image_tag == "forge-worker"


def test_from_env_reads_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    monkeypatch.setenv("GH_TOKEN", "gh-xyz")
    cfg = Config.from_env(tmp_path)
    assert cfg.oauth_token == "tok-abc"
    assert cfg.gh_token == "gh-xyz"


def test_runs_dir_resolved_to_absolute():
    # The CLI defaults --runs-dir to the relative "runs". Docker Compose rejects a
    # relative bind-mount source (e.g. "runs/<id>/workspace") as an *undefined named
    # volume* ("invalid compose project"), so runs_dir must always be absolute.
    cfg = Config(runs_dir=Path("runs"))
    assert cfg.runs_dir.is_absolute()
    assert cfg.runs_dir == Path("runs").resolve()


def test_from_env_runs_dir_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = Config.from_env(Path("runs"))
    assert cfg.runs_dir.is_absolute()


def test_dormant_ttl_default_and_env(monkeypatch, tmp_path):
    assert Config(runs_dir=tmp_path).dormant_ttl_secs == 259200
    monkeypatch.setenv("FORGE_DORMANT_TTL_SECS", "600")
    assert Config.from_env(tmp_path).dormant_ttl_secs == 600


def test_env_ttl_default_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_ENV_TTL_SECS", raising=False)
    assert Config.from_env(tmp_path).env_ttl_secs == 3600
    monkeypatch.setenv("FORGE_ENV_TTL_SECS", "900")
    assert Config.from_env(tmp_path).env_ttl_secs == 900


def test_compose_up_timeout_default_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("FORGE_COMPOSE_UP_TIMEOUT_SECS", raising=False)
    assert Config.from_env(tmp_path).compose_up_timeout_secs == 1200
    monkeypatch.setenv("FORGE_COMPOSE_UP_TIMEOUT_SECS", "300")
    assert Config.from_env(tmp_path).compose_up_timeout_secs == 300


def test_slack_config_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1")
    monkeypatch.setenv("SLACK_ALLOWED_USER", "U999")
    monkeypatch.setenv("FORGE_REPO_CACHE_TTL", "120")
    cfg = Config.from_env(tmp_path)
    assert cfg.slack_bot_token == "xoxb-1"
    assert cfg.slack_app_token == "xapp-1"
    assert cfg.slack_allowed_user == "U999"
    assert cfg.repo_cache_ttl_secs == 120


def test_slack_config_defaults(tmp_path, monkeypatch):
    # Hermetic: a developer/CI box that runs the bot has SLACK_* exported, so the
    # defaults must be asserted against a cleared environment, not the shell's.
    for var in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USER",
                "FORGE_REPO_CACHE_TTL", "FORGE_REPO_ALIASES"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env(tmp_path)
    assert cfg.slack_bot_token == ""
    assert cfg.repo_cache_ttl_secs == 3600
    assert cfg.repo_aliases_path  # non-empty default path


def test_self_heal_config_defaults(tmp_path, monkeypatch):
    for var in ("FORGE_SELF_HEAL", "FORGE_PROBE_MAX_ITERATIONS",
                "FORGE_KNOWLEDGE_DIR"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env(tmp_path)
    assert cfg.self_heal is True
    assert cfg.probe_max_iterations == 6
    assert str(cfg.knowledge_dir).endswith("/.forge/knowledge")


def test_self_heal_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_SELF_HEAL", "0")
    assert Config.from_env(tmp_path).self_heal is False


def test_review_config_defaults():
    from forge.config import Config
    c = Config(runs_dir="runs")
    assert c.gh_app_id == "" and c.gh_app_private_key_path == ""
    assert c.gh_app_slug == "forge"
    assert c.self_review is True
    assert c.commit_identity == "auto"


def test_web_mem_limits_default(tmp_path, monkeypatch):
    for v in ("FORGE_WEB_MEM_LIMIT", "FORGE_WEB_NODE_MAX_OLD_SPACE_MB"):
        monkeypatch.delenv(v, raising=False)
    cfg = Config.from_env(tmp_path)
    assert cfg.web_mem_limit == "8g"
    assert cfg.web_node_max_old_space_mb == 4096


def test_web_mem_limits_default_on_direct_construct(tmp_path):
    cfg = Config(runs_dir=tmp_path)
    assert cfg.web_mem_limit == "8g"
    assert cfg.web_node_max_old_space_mb == 4096


def test_web_mem_limits_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_WEB_MEM_LIMIT", "12g")
    monkeypatch.setenv("FORGE_WEB_NODE_MAX_OLD_SPACE_MB", "6144")
    cfg = Config.from_env(tmp_path)
    assert cfg.web_mem_limit == "12g"
    assert cfg.web_node_max_old_space_mb == 6144


def test_review_config_from_env(monkeypatch):
    from forge.config import Config
    monkeypatch.setenv("FORGE_GH_APP_ID", "999")
    monkeypatch.setenv("FORGE_GH_APP_KEY", "/tmp/key.pem")
    monkeypatch.setenv("FORGE_GH_APP_SLUG", "forge-dev")
    monkeypatch.setenv("FORGE_SELF_REVIEW", "0")
    monkeypatch.setenv("FORGE_COMMIT_IDENTITY", "user")
    c = Config.from_env("runs")
    assert c.gh_app_id == "999" and c.gh_app_private_key_path == "/tmp/key.pem"
    assert c.gh_app_slug == "forge-dev"
    assert c.self_review is False
    assert c.commit_identity == "user"


def test_budget_has_max_repair_iters_default():
    from forge.config import Budget
    assert Budget().max_repair_iters == 4


def test_config_reads_max_repair_iters_env(monkeypatch, tmp_path):
    from forge.config import Config
    monkeypatch.setenv("FORGE_MAX_REPAIR_ITERS", "7")
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.budget.max_repair_iters == 7


def test_qa_gating_defaults_true(tmp_path):
    from forge.config import Config
    assert Config(runs_dir=tmp_path).qa_gating is True


def test_qa_gating_env_off(monkeypatch, tmp_path):
    from forge.config import Config
    monkeypatch.setenv("FORGE_QA_GATING", "0")
    assert Config.from_env(tmp_path / "runs").qa_gating is False


def test_learn_defaults_true(tmp_path):
    from forge.config import Config
    assert Config(runs_dir=tmp_path).learn is True


def test_learn_env_off(monkeypatch, tmp_path):
    from forge.config import Config
    monkeypatch.setenv("FORGE_LEARN", "0")
    assert Config.from_env(tmp_path / "runs").learn is False


def test_config_file_fills_unset_env(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.env"
    cfgfile.write_text("SLACK_BOT_TOKEN=xoxb-fromfile\n")
    monkeypatch.setenv("FORGE_CONFIG", str(cfgfile))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.slack_bot_token == "xoxb-fromfile"


def test_env_overrides_config_file(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.env"
    cfgfile.write_text("SLACK_BOT_TOKEN=xoxb-fromfile\n")
    monkeypatch.setenv("FORGE_CONFIG", str(cfgfile))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fromenv")
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.slack_bot_token == "xoxb-fromenv"


def test_missing_config_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "does-not-exist.env"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.slack_bot_token == ""


def test_binary_config_file_is_noop(tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.env"
    cfgfile.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    monkeypatch.setenv("FORGE_CONFIG", str(cfgfile))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    cfg = Config.from_env(tmp_path / "runs")  # must not raise
    assert cfg.slack_bot_token == ""


def test_forge_web_url_defaults_empty(tmp_path):
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.forge_web_url == ""


def test_forge_web_url_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_WEB_URL", "http://forge.lan:8099")
    cfg = Config.from_env(tmp_path / "runs")
    assert cfg.forge_web_url == "http://forge.lan:8099"


def test_github_webhook_env_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))  # isolate
    monkeypatch.setenv("FORGE_GH_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("FORGE_PUBLIC_URL", "https://forge.example.com")
    from forge.config import Config
    cfg = Config.from_env(tmp_path)
    assert cfg.gh_webhook_secret == "whsec"
    assert cfg.public_url == "https://forge.example.com"


def test_github_webhook_fields_default_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CONFIG", str(tmp_path / "no-such.env"))
    monkeypatch.delenv("FORGE_GH_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("FORGE_PUBLIC_URL", raising=False)
    from forge.config import Config
    cfg = Config.from_env(tmp_path)
    assert cfg.gh_webhook_secret == ""
    assert cfg.public_url == ""
