from types import SimpleNamespace

from forge import proxy
from forge.proxy import caddy_config, container_name, local_url, routes_for, Route


class _FakeRun:
    """Capture docker argv lists so imperative proxy helpers can be unit-tested
    without a real Docker daemon."""
    def __init__(self):
        self.calls = []

    def __call__(self, argv, *a, **k):
        self.calls.append(argv)
        return SimpleNamespace(stdout="", returncode=0)

    def joined(self):
        return [" ".join(c) for c in self.calls]


def test_ensure_proxy_defaults_to_production_name_and_port(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(proxy.subprocess, "run", fake)
    proxy.ensure_proxy("/tmp/Caddyfile", 8088)
    flat = fake.joined()
    assert any(f"--name {proxy.PROXY_NAME}" in c for c in flat)
    assert any("127.0.0.1:8088:8088" in c for c in flat)


def test_ensure_proxy_isolated_name_and_port_never_touch_production(monkeypatch):
    # A test (or any caller) must be able to run a throwaway proxy that can't
    # clobber a live daemon's forge-proxy on 8088.
    fake = _FakeRun()
    monkeypatch.setattr(proxy.subprocess, "run", fake)
    proxy.ensure_proxy("/tmp/Caddyfile", 8099, name="forge-proxy-smoketest")
    flat = fake.joined()
    assert any("--name forge-proxy-smoketest" in c for c in flat)
    assert any("127.0.0.1:8099:8099" in c for c in flat)
    # the production container is never named in any docker command
    assert not any(f"{proxy.PROXY_NAME}" in c and "smoketest" not in c for c in flat)


def test_reload_proxy_targets_named_container(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(proxy.subprocess, "run", fake)
    proxy.reload_proxy(name="forge-proxy-smoketest")
    assert any("exec forge-proxy-smoketest caddy reload" in c for c in fake.joined())


def test_connect_networks_attaches_named_container(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(proxy.subprocess, "run", fake)
    proxy.connect_networks(["r1"], name="forge-proxy-smoketest")
    assert any("network connect forge-r1_default forge-proxy-smoketest" in c
               for c in fake.joined())


def test_container_name():
    assert container_name("ab12", "web") == "forge-ab12-web-1"


def test_local_url_builds_browser_resolvable_proxy_host():
    # `*.localhost` resolves to 127.0.0.1 in every browser with no external DNS,
    # so this URL works on the forge host even when the public tunnel hostname
    # can't be resolved (e.g. router DNS-rebind protection blocks trycloudflare).
    assert local_url("ab12", "forge.localhost", 8088) == \
        "http://run-ab12.forge.localhost:8088"


def test_routes_for_skips_envs_without_service_or_port():
    envs = [
        {"run_id": "ab", "web_service": "web", "web_port": 3000},
        {"run_id": "cd", "web_service": None, "web_port": 3000},
        {"run_id": "ef", "web_service": "frontend", "web_port": 3000},
    ]
    r = routes_for(envs, domain="forge.localhost")
    hosts = {x.host for x in r}
    assert "run-ab.forge.localhost" in hosts
    assert "run-ef.forge.localhost" in hosts
    assert "run-cd.forge.localhost" not in hosts
    ab = next(x for x in r if x.host == "run-ab.forge.localhost")
    assert ab.web == "http://forge-ab-web-1:3000"
    assert ab.supabase is None  # no offset → app-only route


def test_routes_for_adds_supabase_upstream_when_offset_known():
    envs = [{"run_id": "ab", "web_service": "web", "web_port": 3000}]
    r = routes_for(envs, supabase_offsets={"ab": 100}, domain="forge.localhost")
    assert r[0].supabase == "http://host.docker.internal:54421"  # 54321 + 100


def test_caddy_config_app_only_route():
    c = caddy_config([Route("run-ab.forge.localhost", "http://forge-ab-web-1:3000", None)], 8088)
    assert "http://run-ab.forge.localhost:8088" in c
    assert "reverse_proxy http://forge-ab-web-1:3000" in c
    assert "@supabase" not in c


def test_caddy_config_splits_supabase_paths():
    c = caddy_config([Route("run-ab.forge.localhost", "http://forge-ab-web-1:3000",
                            "http://host.docker.internal:54421")], 8088)
    assert "@supabase path /rest/* /auth/* /storage/* /realtime/* /functions/* /graphql/*" in c
    assert "reverse_proxy @supabase http://host.docker.internal:54421" in c
    assert "reverse_proxy http://forge-ab-web-1:3000" in c


def test_caddy_config_empty():
    assert "no live envs" in caddy_config([], 8088)
