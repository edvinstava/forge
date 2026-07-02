import forge.tunnel as T


SAMPLE = """2024-01-01 INF Thank you for trying Cloudflare Tunnel.
2024-01-01 INF +-------------------------------------+
2024-01-01 INF |  https://calm-river-1234.trycloudflare.com  |
2024-01-01 INF +-------------------------------------+
"""


class FakeProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.terminated = False
    def terminate(self): self.terminated = True
    def poll(self): return None


def test_extract_url_finds_trycloudflare():
    assert T.extract_url(SAMPLE) == "https://calm-river-1234.trycloudflare.com"
    assert T.extract_url("nothing here") is None


def test_start_returns_url_and_tracks():
    proc = FakeProc(SAMPLE.splitlines())
    tm = T.TunnelManager(spawn=lambda target: proc, timeout=5)
    url = tm.start("run1", "http://localhost:3001")
    assert url == "https://calm-river-1234.trycloudflare.com"
    assert tm.url_for("run1") == url
    assert tm.running_ids() == {"run1"}


def test_start_passes_host_header_to_spawn():
    captured = {}
    def spawn(target, host_header=None):
        captured["target"] = target
        captured["host_header"] = host_header
        return FakeProc(SAMPLE.splitlines())
    tm = T.TunnelManager(spawn=spawn, timeout=5)
    tm.start("run1", "http://localhost:8088", host_header="run-run1.forge.localhost")
    assert captured["target"] == "http://localhost:8088"
    assert captured["host_header"] == "run-run1.forge.localhost"


def test_default_spawn_argv_includes_host_header():
    seen = {}
    class _FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"] = argv
            self.stdout = iter(())
    orig = T.subprocess.Popen
    T.subprocess.Popen = _FakePopen
    try:
        T._default_spawn("http://localhost:8088", host_header="run-x.forge.localhost")
    finally:
        T.subprocess.Popen = orig
    assert "--http-host-header" in seen["argv"]
    assert "run-x.forge.localhost" in seen["argv"]


def test_start_idempotent():
    calls = []
    def spawn(target):
        calls.append(target)
        return FakeProc(SAMPLE.splitlines())
    tm = T.TunnelManager(spawn=spawn, timeout=5)
    tm.start("run1", "http://localhost:3001")
    tm.start("run1", "http://localhost:3001")
    assert len(calls) == 1  # second call reused cached tunnel


def test_start_no_url_terminates_and_returns_none():
    proc = FakeProc(["INF starting", "INF no url here"])
    tm = T.TunnelManager(spawn=lambda target: proc, timeout=5)
    assert tm.start("run1", "http://localhost:3001") is None
    assert proc.terminated is True
    assert tm.running_ids() == set()


def test_start_missing_binary_returns_none():
    def spawn(target): raise FileNotFoundError("cloudflared")
    tm = T.TunnelManager(spawn=spawn, timeout=5)
    assert tm.start("run1", "http://localhost:3001") is None


def test_stop_is_idempotent():
    proc = FakeProc(SAMPLE.splitlines())
    tm = T.TunnelManager(spawn=lambda target: proc, timeout=5)
    tm.start("run1", "http://localhost:3001")
    tm.stop("run1")
    assert proc.terminated is True
    assert tm.url_for("run1") is None
    tm.stop("run1")  # no raise


import io
import urllib.error


def test_start_reprobes_and_skips_error_1000():
    urls = ["https://bad.trycloudflare.com", "https://good.trycloudflare.com"]
    made = []
    def spawn(target):
        u = urls[len(made)]
        p = FakeProc([f"INF |  {u}  |"])
        made.append(p)
        return p
    tm = T.TunnelManager(spawn=spawn, timeout=5,
                         probe=lambda url: "good" in url, max_attempts=3)
    assert tm.start("run1", "http://localhost:3001") == "https://good.trycloudflare.com"
    assert made[0].terminated is True       # bad tunnel torn down
    assert len(made) == 2                    # respawned exactly once


def test_start_all_bad_returns_none():
    tm = T.TunnelManager(
        spawn=lambda t: FakeProc(["INF |  https://bad.trycloudflare.com  |"]),
        timeout=5, probe=lambda u: False, max_attempts=3)
    assert tm.start("run1", "http://localhost:3001") is None


def test_http_probe_rejects_530(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 530, "x", {}, None)
    monkeypatch.setattr(T.urllib.request, "urlopen", boom)
    assert T.http_probe("https://x.trycloudflare.com") is False


def test_http_probe_accepts_app_404(monkeypatch):
    def four04(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"not found"))
    monkeypatch.setattr(T.urllib.request, "urlopen", four04)
    assert T.http_probe("https://x.trycloudflare.com") is True
