from forge.recipe import Probe, detect_pkg_manager

_CHAP_APP_ID = "a29851f9"
_REPO_COMPOSE = ("docker-compose.yml", "docker-compose.yaml",
                 "compose.yml", "compose.yaml")


def build_probe(host, ws: str) -> Probe:
    d2 = host.read(ws, "d2.config.js") or ""
    pyproject = host.read(ws, "pyproject.toml") or ""
    repo_compose = next((f for f in _REPO_COMPOSE if host.exists(ws, f)), None)
    return Probe(
        package_json=host.read(ws, "package.json"),
        repo_yml=host.read(ws, ".forge/repo.yml"),
        env_yml=host.read(ws, ".forge/env.yml"),
        has_supabase_config=host.exists(ws, "supabase/config.toml"),
        has_d2_config=bool(d2),
        is_chap_frontend=_CHAP_APP_ID in d2,
        is_chap_core=("chap-core" in pyproject or "chap_core" in pyproject
                      or host.exists(ws, "chap_core")),
        repo_compose_path=repo_compose,
        repo_compose_text=host.read(ws, repo_compose) if repo_compose else None,
        pkg_manager=detect_pkg_manager(host, ws),
    )
