import json

from forge.recipe import (SUPABASE_LOCAL_ANON_KEY, Probe, apply_overlay,
                          dhis2_chap_recipe, next_supabase_recipe,
                          node_web_recipe, resolve, synthesized_recipe)


def test_node_web_recipe_structure():
    r = node_web_recipe("/ws", "forge-worker", '{"scripts":{"dev":"next dev"}}')
    assert r.name == "node-web" and r.multi
    assert r.web_service == "web" and r.web_port == 3000
    svc = r.compose["services"]
    assert "web" in svc and "forge" in svc
    assert svc["web"]["ports"] == ["127.0.0.1::3000"]
    assert "/ws:/work" in svc["web"]["volumes"]
    assert "npm run dev" in svc["web"]["command"][0]
    assert svc["web"]["entrypoint"] == ["sh", "-lc"]   # overrides image sleep ENTRYPOINT
    json.dumps(r.compose)  # JSON-serializable (we write it as JSON-is-YAML)


def test_node_web_uses_start_when_no_dev():
    r = node_web_recipe("/ws", "img", '{"scripts":{"start":"node s"}}')
    assert "npm run start" in r.compose["services"]["web"]["command"][0]


def test_next_supabase_recipe():
    r = next_supabase_recipe("/ws", "img")
    assert r.name == "next-supabase" and r.multi
    web = r.compose["services"]["web"]
    assert web["environment"]["NEXT_PUBLIC_SUPABASE_URL"] == "http://host.docker.internal:54321"
    assert "host.docker.internal:host-gateway" in web["extra_hosts"]
    assert any(c[:2] == ["supabase", "start"] for c in r.host_pre)
    assert any(c[:2] == ["supabase", "stop"] for c in r.host_post)
    json.dumps(r.compose)


def test_next_supabase_offset_shifts_api_url():
    r = next_supabase_recipe("/ws", "img", offset=100)
    web = r.compose["services"]["web"]
    assert web["environment"]["NEXT_PUBLIC_SUPABASE_URL"] == \
        "http://host.docker.internal:54421"
    json.dumps(r.compose)


def test_next_supabase_default_offset_is_base_port():
    r = next_supabase_recipe("/ws", "img")
    assert r.compose["services"]["web"]["environment"]["NEXT_PUBLIC_SUPABASE_URL"] == \
        "http://host.docker.internal:54321"


def test_resolve_threads_supabase_offset():
    probe = Probe(package_json='{"dependencies":{"next":"14"}}',
                  has_supabase_config=True)
    r = resolve(probe, "/ws", "img", supabase_offset=200)
    assert r.compose["services"]["web"]["environment"]["NEXT_PUBLIC_SUPABASE_URL"] == \
        "http://host.docker.internal:54521"


def test_dhis2_chap_frontend_target():
    r = dhis2_chap_recipe("/ws", "img", "frontend", target_repo="frontend")
    svc = r.compose["services"]
    for s in ("dhis2-db", "dhis2-web", "chap", "chap-worker", "chap-redis",
              "chap-db", "frontend", "forge"):
        assert s in svc
    assert r.web_service == "frontend" and r.web_port == 3000
    assert "/ws:/work" in svc["frontend"]["volumes"]      # live-mount the app
    assert any("routes" in " ".join(argv) for _, argv in r.seed)  # Route bootstrap
    json.dumps(r.compose)


def test_dhis2_chap_core_target_publishes_dhis2():
    r = dhis2_chap_recipe("/ws", "img", "chap-core", target_repo="chap-core")
    assert r.web_service == "dhis2-web" and r.web_port == 8080
    assert r.health_path == "/api/system/info.json"
    assert "/ws:/work" in r.compose["services"]["chap"]["volumes"]


def test_resolve_precedence():
    assert resolve(Probe(is_chap_frontend=True), "/ws", "img").name == "dhis2-chap"
    assert resolve(Probe(is_chap_core=True), "/ws", "img").name == "dhis2-chap"
    assert resolve(Probe(package_json='{"dependencies":{"next":"14"}}',
                         has_supabase_config=True), "/ws", "img").name == "next-supabase"
    assert resolve(Probe(package_json='{"scripts":{"dev":"vite"}}'),
                   "/ws", "img").name == "node-web"
    # A package.json with no dev/start script is still a JS app: node-web, but
    # low-confidence so the self-heal probe can learn a dev_cmd.
    r = resolve(Probe(package_json='{"scripts":{"test":"x"}}'), "/ws", "img")
    assert r.name == "node-web" and r.confidence == "low"
    assert resolve(Probe(), "/ws", "img").name == "none"   # no package.json → worker-only


def test_dhis2_chap_seed_dir_mount():
    r = dhis2_chap_recipe("/ws", "img", "frontend", target_repo="frontend",
                          seed_dir="/cache/seed")
    vols = r.compose["services"]["dhis2-db"]["volumes"]
    assert any(v.startswith("/cache/seed:") for v in vols)


# --- runtime_facts: the operational facts block handed to the agent ----------

def test_runtime_facts_node_web_has_app_and_commands():
    r = node_web_recipe("/ws", "img",
                        '{"scripts":{"dev":"next dev"}}', pkg_manager="bun")
    f = r.runtime_facts()
    assert f["stack"] == "node-web"
    assert f["app"] == "http://web:3000"
    assert f["pkg_manager"] == "bun"
    assert f["dev_cmd"] == "bun run dev"
    assert f["endpoints"] == []
    assert f["test_cmds"] == []


def test_runtime_facts_includes_test_commands():
    r = node_web_recipe(
        "/ws", "img",
        '{"scripts":{"dev":"next dev","test":"vitest","test:e2e":"pw test"}}',
        pkg_manager="bun")
    assert r.runtime_facts()["test_cmds"] == ["bun run test", "bun run test:e2e"]


def test_runtime_facts_app_url_override():
    r = node_web_recipe("/ws", "img", '{"scripts":{"dev":"vite"}}')
    assert r.runtime_facts("http://run-x.forge.localhost:8088")["app"] == \
        "http://run-x.forge.localhost:8088"


def test_runtime_facts_next_supabase_exposes_supabase_endpoint():
    r = next_supabase_recipe("/ws", "img", offset=100, pkg_manager="pnpm",
                             package_json='{"scripts":{"test":"vitest"}}')
    f = r.runtime_facts()
    assert ("Supabase", "http://host.docker.internal:54421") in f["endpoints"]
    assert f["dev_cmd"] == "pnpm run dev"
    assert f["test_cmds"] == ["pnpm run test"]


def test_runtime_facts_dhis2_chap_lists_dhis2_and_chap():
    r = dhis2_chap_recipe("/ws", "img", "frontend", target_repo="frontend")
    eps = dict(r.runtime_facts()["endpoints"])
    assert eps["DHIS2"] == "http://dhis2-web:8080"
    assert eps["CHAP"] == "http://chap:8000"
    assert r.runtime_facts()["app"] == "http://frontend:3000"


def test_runtime_facts_dhis2_chap_core_dedupes_app_endpoint():
    # When the app IS dhis2-web, don't repeat it as an endpoint line.
    r = dhis2_chap_recipe("/ws", "img", "chap-core", target_repo="chap-core")
    labels = [lbl for lbl, _ in r.runtime_facts()["endpoints"]]
    assert "DHIS2" not in labels            # app == http://dhis2-web:8080
    assert "CHAP" in labels


def test_runtime_facts_worker_only_is_minimal():
    from forge.recipe import none_recipe
    f = none_recipe("/ws", "img").runtime_facts()
    assert f["app"] is None
    assert f["dev_cmd"] is None
    assert f["endpoints"] == []


def test_dhis2_seed_url():
    from forge.recipe import dhis2_seed_url
    assert "sierra-leone/2.42/" in dhis2_seed_url("2.42")
    assert dhis2_seed_url("2.41").endswith(".sql.gz")


def test_anon_key_is_ascii_jwt():
    SUPABASE_LOCAL_ANON_KEY.encode("ascii")          # no stray unicode
    assert SUPABASE_LOCAL_ANON_KEY.count(".") == 2   # header.payload.sig


def test_web_command_per_pkg_manager():
    from forge.recipe import web_command
    assert web_command("bun", "dev", 3000) == "bun install && PORT=3000 bun run dev"
    assert web_command("pnpm", "dev", 3000) == "pnpm install && PORT=3000 pnpm run dev"
    assert web_command("yarn", "start", 3000) == "yarn install && PORT=3000 yarn run start"
    assert web_command("npm", "dev", 3000) == "npm install && PORT=3000 npm run dev"


def test_web_command_prepends_apt_as_root():
    from forge.recipe import web_command
    cmd = web_command("bun", "dev", 3000, apt=["libnss3"])
    assert cmd.startswith("apt-get update && apt-get install -y --no-install-recommends libnss3 && ")
    assert "bun install && PORT=3000 bun run dev" in cmd


def test_confidence_low_for_none_recipe():
    r = resolve(Probe(), "/ws", "img")           # no signals at all
    assert r.name == "none" and r.confidence == "low"


def test_confidence_high_for_next_supabase():
    r = resolve(Probe(package_json='{"dependencies":{"next":"1"}}',
                      has_supabase_config=True), "/ws", "img")
    assert r.name == "next-supabase" and r.confidence == "high"


def test_apply_overlay_overrides_pkg_manager_apt_and_port():
    from forge.recipe import apply_overlay
    base = resolve(Probe(package_json='{"scripts":{"dev":"vite"}}'), "/ws", "img")
    out = apply_overlay(base, {"pkg_manager": "bun", "apt": ["libnss3"],
                               "web_port": 4000, "health_path": "/health"})
    cmd = out.compose["services"]["web"]["command"][0]
    assert "bun install" in cmd and "apt-get install -y --no-install-recommends libnss3" in cmd
    assert out.web_port == 4000 and out.health_path == "/health"
    assert out.compose["services"]["web"]["user"] == "root"   # apt needs root


def test_apply_overlay_dev_cmd_escape_hatch():
    from forge.recipe import apply_overlay
    base = resolve(Probe(package_json='{"scripts":{"dev":"vite"}}'), "/ws", "img")
    out = apply_overlay(base, {"dev_cmd": "make serve PORT=3000"})
    assert out.compose["services"]["web"]["command"] == ["make serve PORT=3000"]


# --- resource limits: contain a leaky dev server so it can't eat the host ---

def test_apply_resource_limits_caps_web_service():
    from forge.recipe import apply_resource_limits
    base = node_web_recipe("/ws", "img", '{"scripts":{"dev":"next dev"}}')
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    web = out.compose["services"]["web"]
    assert web["mem_limit"] == "8g"                 # hard container backstop
    assert web["restart"] == "unless-stopped"       # auto-recover after an OOM kill
    assert web["environment"]["NODE_OPTIONS"] == "--max-old-space-size=4096"
    json.dumps(out.compose)                         # still JSON-serializable


def test_apply_resource_limits_merges_existing_env():
    # next-supabase already sets NEXT_PUBLIC_* env; the V8 heap cap must be added
    # alongside it, never replace it.
    from forge.recipe import apply_resource_limits
    base = next_supabase_recipe("/ws", "img")
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    env = out.compose["services"]["web"]["environment"]
    assert env["NEXT_PUBLIC_SUPABASE_URL"] == "http://host.docker.internal:54321"
    assert "--max-old-space-size=4096" in env["NODE_OPTIONS"]


def test_apply_resource_limits_keeps_explicit_node_options():
    # A recipe/overlay that already pinned a heap size wins — don't stack a
    # second --max-old-space-size (Node would honor the last one anyway).
    from forge.recipe import apply_resource_limits
    base = node_web_recipe("/ws", "img", '{"scripts":{"dev":"next dev"}}')
    base.compose["services"]["web"].setdefault(
        "environment", {})["NODE_OPTIONS"] = "--max-old-space-size=2048"
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    assert out.compose["services"]["web"]["environment"]["NODE_OPTIONS"] == \
        "--max-old-space-size=2048"


def test_apply_resource_limits_clears_stale_next_dev_lock():
    # next dev refuses to boot if .next/dev/lock survives a hard kill (OOM /
    # container restart) — clearing it first is what makes restart-based recovery
    # actually work instead of crash-looping on "already running".
    from forge.recipe import apply_resource_limits
    base = node_web_recipe("/ws", "img", '{"scripts":{"dev":"next dev"}}')
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    cmd = out.compose["services"]["web"]["command"][0]
    assert "rm -f .next/dev/lock" in cmd
    assert cmd.endswith("npm run dev")               # original command preserved


def test_apply_resource_limits_noop_for_worker_only():
    from forge.recipe import apply_resource_limits, none_recipe
    base = none_recipe("/ws", "img")                 # no web_service to cap
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    assert "mem_limit" not in out.compose["services"]["forge"]
    assert "restart" not in out.compose["services"]["forge"]


def test_apply_resource_limits_disabled_when_unset():
    # Empty mem_limit / zero heap = opt out (still set restart for recovery).
    from forge.recipe import apply_resource_limits
    base = node_web_recipe("/ws", "img", '{"scripts":{"dev":"next dev"}}')
    out = apply_resource_limits(base, mem_limit="", node_max_old_space_mb=0)
    web = out.compose["services"]["web"]
    assert "mem_limit" not in web
    assert "NODE_OPTIONS" not in (web.get("environment") or {})


def test_apply_resource_limits_handles_list_form_environment():
    # Docker Compose allows `environment` as a LIST (`["KEY=val"]`), not just a
    # dict. A wrapped repo-compose web service is the author's own, so it can
    # legitimately use either form. The cap must not crash on the list form and
    # must add the heap flag alongside the author's vars, in the same form.
    from forge.recipe import apply_resource_limits
    base = _wrap("""
        services:
          web:
            image: myapp
            ports: ["3000:3000"]
            environment:
              - NODE_ENV=production
    """)
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    env = out.compose["services"]["web"]["environment"]
    assert isinstance(env, list)                       # author's form preserved
    assert "NODE_ENV=production" in env                 # original var kept
    assert any(e.startswith("NODE_OPTIONS=")
               and "--max-old-space-size=4096" in e for e in env)
    json.dumps(out.compose)                            # still JSON-serializable


def test_apply_resource_limits_list_form_keeps_explicit_node_options():
    # Same no-double-cap rule as the dict form, for the list form.
    from forge.recipe import apply_resource_limits
    base = _wrap("""
        services:
          web:
            image: myapp
            ports: ["3000:3000"]
            environment:
              - NODE_OPTIONS=--max-old-space-size=2048
    """)
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    env = out.compose["services"]["web"]["environment"]
    opts = [e for e in env if e.startswith("NODE_OPTIONS=")]
    assert opts == ["NODE_OPTIONS=--max-old-space-size=2048"]


def test_apply_resource_limits_preserves_author_mem_and_restart():
    # On the wrapped repo-compose path the web service is the author's own; an
    # explicit mem_limit / restart is a deliberate choice and must not be
    # clobbered by the default cap.
    from forge.recipe import apply_resource_limits
    base = _wrap("""
        services:
          web:
            image: myapp
            ports: ["3000:3000"]
            mem_limit: 1g
            restart: "no"
    """)
    out = apply_resource_limits(base, mem_limit="8g", node_max_old_space_mb=4096)
    web = out.compose["services"]["web"]
    assert web["mem_limit"] == "1g"                    # author cap preserved
    assert web["restart"] == "no"                      # author policy preserved


# --- repo's own compose wrapping ---------------------------------------------

def _wrap(text, ws="/ws"):
    import textwrap
    from forge.recipe import repo_compose_recipe
    return repo_compose_recipe(ws, "forge-worker", textwrap.dedent(text))


def test_repo_compose_injects_worker_and_picks_web():
    r = _wrap("""
        services:
          web:
            image: myapp
            ports: ["3000:3000"]
          db:
            image: postgres
    """)
    assert r.name == "repo-compose" and r.multi
    assert r.web_service == "web" and r.web_port == 3000
    svc = r.compose["services"]
    assert "forge" in svc                              # worker injected
    assert svc["forge"]["entrypoint"] == ["sh", "-lc"]
    assert "db" in svc                                 # original services kept
    json.dumps(r.compose)                              # JSON-serializable


def test_repo_compose_container_port_forms():
    from forge.recipe import _container_port
    assert _container_port({"ports": ["127.0.0.1:8080:80"]}) == 80
    assert _container_port({"ports": ["8080"]}) == 8080
    assert _container_port({"ports": ["3000:3000/tcp"]}) == 3000
    assert _container_port({"ports": [{"target": 5000, "published": 80}]}) == 5000
    assert _container_port({"expose": ["9000"]}) == 9000
    assert _container_port({"image": "x"}) is None


def test_repo_compose_prefers_weblike_name_over_db():
    r = _wrap("""
        services:
          database:
            image: postgres
            ports: ["5432:5432"]
          frontend:
            image: app
            ports: ["8080:8080"]
    """)
    assert r.web_service == "frontend" and r.web_port == 8080


def test_repo_compose_absolutizes_relative_paths():
    r = _wrap("""
        services:
          web:
            build: ./app
            ports: ["3000:3000"]
            volumes:
              - ./data:/var/lib/data
              - named_vol:/cache
              - /abs/host:/x
            env_file: ./.env
    """, ws="/runs/ws")
    web = r.compose["services"]["web"]
    assert web["build"] == "/runs/ws/app"
    assert "/runs/ws/data:/var/lib/data" in web["volumes"]
    assert "named_vol:/cache" in web["volumes"]        # named volume untouched
    assert "/abs/host:/x" in web["volumes"]            # absolute source untouched
    assert web["env_file"] == "/runs/ws/.env"


def test_repo_compose_build_context_dict_absolutized():
    r = _wrap("""
        services:
          web:
            build:
              context: .
              dockerfile: Dockerfile
            ports: ["3000"]
    """, ws="/x/y")
    assert r.compose["services"]["web"]["build"]["context"] == "/x/y"


def test_repo_compose_single_service_no_port_is_worker_only():
    r = _wrap("""
        services:
          batch:
            image: cruncher
    """)
    assert r.web_service is None and r.web_port is None
    assert "forge" in r.compose["services"]            # still editable/PR-able


def test_repo_compose_refuses_forge_service_collision():
    assert _wrap("services:\n  forge:\n    image: x\n") is None


def test_repo_compose_rejects_unparseable_and_empty():
    assert _wrap("web: [unclosed") is None             # YAMLError
    assert _wrap("name: just-a-string") is None        # no services mapping
    assert _wrap("services: {}") is None               # empty services


def test_repo_compose_worker_joins_named_networks():
    r = _wrap("""
        services:
          web:
            image: app
            ports: ["3000:3000"]
            networks: [appnet]
        networks:
          appnet: {}
    """)
    assert r.compose["services"]["forge"]["networks"] == ["appnet"]


def test_resolve_uses_repo_compose_when_no_package_json():
    p = Probe(repo_compose_text=(
        "services:\n  web:\n    image: a\n    ports: ['3000:3000']\n"))
    r = resolve(p, "/ws", "img")
    assert r.name == "repo-compose" and r.web_service == "web"


def test_resolve_prefers_node_web_over_incidental_compose():
    # A JS app that also ships a compose (e.g. just a db) must still run via its
    # dev script, not be hijacked into the repo-compose path.
    p = Probe(package_json='{"scripts":{"dev":"vite"}}',
              repo_compose_text="services:\n  db:\n    image: postgres\n")
    assert resolve(p, "/ws", "img").name == "node-web"


def test_resolve_repo_compose_collision_falls_back_to_none():
    p = Probe(repo_compose_text="services:\n  forge:\n    image: x\n")
    assert resolve(p, "/ws", "img").name == "none"


# --- synthesized recipes: spin up anything from a learned/committed overlay ---

def _synth_overlay(**extra):
    ov = {"image": "python:3.12-slim",
          "setup_cmds": ["pip install -e ."],
          "dev_cmd": "python manage.py runserver 0.0.0.0:8000",
          "web_port": 8000,
          "health_path": "/healthz",
          "env": {"DATABASE_URL": "postgresql://forge:forge@db:5432/app"},
          "apt": ["libpq5"],
          "services": {"db": {"image": "postgres:16",
                              "environment": {"POSTGRES_USER": "forge",
                                              "POSTGRES_PASSWORD": "forge",
                                              "POSTGRES_DB": "app"}}}}
    ov.update(extra)
    return ov


def test_synthesized_recipe_structure():
    r = synthesized_recipe("/ws", "forge-worker", _synth_overlay())
    assert r.name == "synthesized" and r.multi and r.confidence == "high"
    assert r.web_service == "web" and r.web_port == 8000
    assert r.health_path == "/healthz"
    svc = r.compose["services"]
    web = svc["web"]
    assert web["image"] == "python:3.12-slim"
    assert "/ws:/work" in web["volumes"]
    assert web["entrypoint"] == ["sh", "-lc"]
    assert web["ports"] == ["127.0.0.1::8000"]
    assert web["user"] == "root"                      # apt needs root
    cmd = web["command"][0]
    assert "apt-get install -y --no-install-recommends libpq5" in cmd
    assert "pip install -e ." in cmd
    assert "PORT=8000 python manage.py runserver" in cmd
    assert web["environment"]["DATABASE_URL"].endswith("db:5432/app")
    assert svc["db"]["image"] == "postgres:16"        # extra service carried
    assert "ports" not in svc["db"]                   # nothing published
    assert "forge" in svc                             # worker injected
    json.dumps(r.compose)


def test_synthesized_defaults_worker_image_and_health():
    r = synthesized_recipe("/ws", "forge-worker",
                           {"dev_cmd": "go run .", "web_port": 8080})
    web = r.compose["services"]["web"]
    assert web["image"] == "forge-worker"
    assert "user" not in web                          # no apt → unprivileged
    assert r.health_path == "/"
    assert web["command"] == ["PORT=8080 go run ."]


def test_resolve_learned_overlay_synthesizes_when_no_marker():
    r = resolve(Probe(), "/ws", "img",
                overlay={"dev_cmd": "go run .", "web_port": 8080})
    assert r.name == "synthesized" and r.web_port == 8080
    # An overlay that doesn't describe an app can't upgrade none.
    assert resolve(Probe(), "/ws", "img", overlay={"apt": ["curl"]}).name == "none"


def test_resolve_repo_compose_beats_learned_synthesis():
    p = Probe(repo_compose_text=(
        "services:\n  web:\n    image: a\n    ports: ['3000:3000']\n"))
    r = resolve(p, "/ws", "img", overlay={"dev_cmd": "go run .", "web_port": 8080})
    assert r.name == "repo-compose"


def test_resolve_env_yml_beats_markers():
    # A committed .forge/env.yml that declares the app is the repo author's
    # explicit recipe: precedence #1, even over package.json.
    p = Probe(package_json='{"scripts":{"dev":"vite"}}',
              env_yml="dev_cmd: python app.py\nweb_port: 8000\n")
    r = resolve(p, "/ws", "img")
    assert r.name == "synthesized"
    assert "PORT=8000 python app.py" in r.compose["services"]["web"]["command"][0]


def test_resolve_env_yml_patch_applies_to_resolved_recipe():
    # An env.yml with no dev_cmd is an overlay patch, not a full recipe.
    p = Probe(package_json='{"scripts":{"dev":"vite"}}',
              env_yml="env:\n  VITE_FLAG: '1'\n")
    r = resolve(p, "/ws", "img")
    assert r.name == "node-web"
    assert r.compose["services"]["web"]["environment"]["VITE_FLAG"] == "1"


def test_resolve_env_yml_invalid_is_ignored():
    p = Probe(package_json='{"scripts":{"dev":"vite"}}',
              env_yml="bogus_key: 1\n")
    assert resolve(p, "/ws", "img").name == "node-web"
    p2 = Probe(env_yml=":\nnot yaml: [\n")
    assert resolve(p2, "/ws", "img").name == "none"


def test_resolve_env_yml_wins_over_learned_overlay():
    p = Probe(env_yml="dev_cmd: python app.py\nweb_port: 8000\n")
    r = resolve(p, "/ws", "img", overlay={"dev_cmd": "ruby app.rb",
                                          "web_port": 4567,
                                          "apt": ["libpq5"]})
    cmd = r.compose["services"]["web"]["command"][0]
    assert "PORT=8000 python app.py" in cmd           # committed beats learned
    assert "libpq5" in cmd                            # apt still unions in


def test_apply_overlay_rebuilds_synthesized_and_keeps_forge_service():
    r = synthesized_recipe("/ws", "img", _synth_overlay())
    # session mutates the forge service in place (e.g. codex auth mount) before
    # the repair loop runs — a rebuild must not lose that.
    r.compose["services"]["forge"]["volumes"].append("/home/u/.codex:/home/forge/.codex")
    out = apply_overlay(r, {"apt": ["libxml2"], "web_port": 8001})
    web = out.compose["services"]["web"]
    cmd = web["command"][0]
    assert "libpq5" in cmd and "libxml2" in cmd       # apt unioned
    assert "pip install -e ." in cmd                  # setup steps survive
    assert "PORT=8001" in cmd and out.web_port == 8001
    assert web["ports"] == ["127.0.0.1::8001"]
    assert "/home/u/.codex:/home/forge/.codex" in out.compose["services"]["forge"]["volumes"]
    assert out.compose["services"]["db"]["image"] == "postgres:16"


def test_apply_overlay_synthesized_is_idempotent():
    ov = _synth_overlay()
    r = synthesized_recipe("/ws", "img", ov)
    again = apply_overlay(r, ov)
    assert again.compose["services"]["web"] == r.compose["services"]["web"]


def test_runtime_facts_synthesized():
    r = synthesized_recipe("/ws", "img", _synth_overlay())
    facts = r.runtime_facts()
    assert facts["stack"] == "synthesized"
    assert facts["app"] == "http://web:8000"
    assert facts["dev_cmd"] == "python manage.py runserver 0.0.0.0:8000"
    assert facts["pkg_manager"] is None
    assert facts["test_cmds"] == []


def test_synthesized_overlay_secrets_never_reach_synth_state():
    # qa_credentials/lessons ride along in the repo overlay; the recipe must
    # not carry them (synth_overlay is rebuilt into composes on repair).
    ov = _synth_overlay(qa_credentials=[{"username": "a", "password": "b"}],
                        lessons=[{"text": "x"}])
    r = synthesized_recipe("/ws", "img", ov)
    assert "qa_credentials" not in (r.synth_overlay or {})
    assert "lessons" not in (r.synth_overlay or {})
    assert "password" not in json.dumps(r.compose)
