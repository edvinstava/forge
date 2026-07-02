"""cloudflared quick tunnels: one public https URL per run, pointed at the
run's local app port. Quick tunnels need no Cloudflare account or domain.
Degrades gracefully when the binary is absent (start() -> None)."""
import queue
import re
import subprocess
import threading
import urllib.error
import urllib.request

_TRYCF = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CF_ERR = re.compile(r"Error 1000|prohibited IP", re.I)


def extract_url(text: str):
    m = _TRYCF.search(text or "")
    return m.group(0) if m else None


def http_probe(url: str, timeout: float = 5.0) -> bool:
    """True if the tunnel serves real traffic. False only on the Cloudflare
    *edge* failure (Error 1000 / "DNS points to prohibited IP", HTTP 530) — a
    bad random hostname. The app's own redirects (3xx, followed) and 4xx are
    healthy. Ambiguous probe errors (DNS not warm yet, timeout) are treated as
    healthy so we never reject a tunnel that just needs a moment."""
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return getattr(resp, "status", 200) != 530
    except urllib.error.HTTPError as e:
        if e.code == 530:
            return False
        try:
            body = e.read(2048).decode("utf-8", "replace")
        except Exception:
            body = ""
        return not _CF_ERR.search(body)
    except Exception:
        return True


def _default_spawn(target: str, host_header: str | None = None):
    argv = ["cloudflared", "tunnel", "--url", target]
    if host_header:
        # Singleton Caddy routes by Host; rewrite it so the per-run site matches
        # even though the public hostname is the random trycloudflare one.
        argv += ["--http-host-header", host_header]
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)


class TunnelManager:
    def __init__(self, spawn=None, timeout=30, probe=None, max_attempts=3):
        self._spawn = spawn or _default_spawn
        self._timeout = timeout
        self._probe = probe                 # None => no health check (back-compat)
        self._max_attempts = max(1, max_attempts)
        self._procs: dict = {}
        self._urls: dict = {}

    def start(self, run_id, target, host_header=None):
        if run_id in self._urls:
            return self._urls[run_id]
        for _ in range(self._max_attempts):
            try:
                proc = (self._spawn(target, host_header=host_header)
                        if host_header is not None else self._spawn(target))
            except FileNotFoundError:
                return None
            url = self._read_url(proc)
            if not url:
                self._safe_terminate(proc)
                continue                    # respawn for a fresh hostname
            if self._probe is not None and not self._probe(url):
                self._safe_terminate(proc)
                continue                    # bad edge (Error 1000) — retry
            self._procs[run_id] = proc
            self._urls[run_id] = url
            return url
        return None

    def _read_url(self, proc):
        q: queue.Queue = queue.Queue()

        def reader():
            for line in proc.stdout:
                u = extract_url(line)
                if u:
                    q.put(u)
                    return
            q.put(None)            # EOF without a URL

        threading.Thread(target=reader, daemon=True).start()
        try:
            return q.get(timeout=self._timeout)
        except queue.Empty:
            return None

    @staticmethod
    def _safe_terminate(proc):
        try:
            proc.terminate()
        except Exception:
            pass

    def stop(self, run_id):
        proc = self._procs.pop(run_id, None)
        self._urls.pop(run_id, None)
        if proc is not None:
            self._safe_terminate(proc)

    def url_for(self, run_id):
        return self._urls.get(run_id)

    def running_ids(self):
        return set(self._urls)
