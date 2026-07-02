"""Per-run Supabase port-block isolation.

The `next-supabase` recipe runs Supabase on the host via the `supabase` CLI,
which binds fixed ports from the repo's `supabase/config.toml` and names its
containers by `project_id`. To run several sessions of the same repo at once,
each session gets a unique `project_id` and a unique, free port block: we shift
every host-bound port in the cloned config by a per-run offset chosen by probing
for a free block.

This module is pure (rewrite/probe helpers) plus a small lock-guarded
`SupabaseAllocator` that records reservations in the store.
"""
import re
import socket
import threading

# The default Supabase local API (Kong) port. Per-run port = this + offset.
SUPABASE_BASE_API_PORT = 54321

# Allowlist of (section, key) entries in config.toml that bind a host port and
# must be shifted. Deliberately excludes (auth.email.smtp, port) = 465, which is
# a remote SMTP setting, not a local bind.
PORT_KEYS = {
    ("api", "port"),
    ("db", "port"),
    ("db", "shadow_port"),
    ("db.pooler", "port"),
    ("studio", "port"),
    ("inbucket", "port"),
    ("analytics", "port"),
    ("edge_runtime", "inspector_port"),
}

_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_PORT_RE = re.compile(r"^(\s*)(port|shadow_port|inspector_port)(\s*=\s*)(\d+)(.*)$")
_PROJECT_RE = re.compile(r'^(\s*project_id\s*=\s*)"[^"]*"(.*)$')
_PROJECT_VAL_RE = re.compile(r'^\s*project_id\s*=\s*"([^"]*)"')


class NoFreePortBlock(Exception):
    """Raised when no free Supabase port block is found within max_blocks."""


def _iter_ports(config_text):
    """Yield (section, key, value) for allowlisted, non-comment port lines."""
    section = None
    for line in config_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        sm = _SECTION_RE.match(line)
        if sm:
            section = sm.group(1)
            continue
        pm = _PORT_RE.match(line)
        if pm and (section, pm.group(2)) in PORT_KEYS:
            yield section, pm.group(2), int(pm.group(4))


def read_project_id(config_text: str):
    """The current top-level project_id, or None if absent."""
    for line in config_text.splitlines():
        if line.lstrip().startswith(("#", "[")):
            continue
        m = _PROJECT_VAL_RE.match(line)
        if m:
            return m.group(1)
    return None


def base_ports(config_text) -> list:
    """The host ports declared for allowlisted keys (the base block)."""
    return [v for _, _, v in _iter_ports(config_text)]


def required_ports(config_text, offset: int) -> list:
    """The ports a run will actually bind: base block shifted by offset."""
    return [v + offset for v in base_ports(config_text)]


def rewrite_config(config_text: str, project_id: str, offset: int) -> str:
    """Return config_text with project_id set and every allowlisted port shifted
    by offset. Comments, formatting, and non-allowlisted values are untouched."""
    out = []
    section = None
    for line in config_text.splitlines():
        if line.lstrip().startswith("#"):
            out.append(line)
            continue
        sm = _SECTION_RE.match(line)
        if sm:
            section = sm.group(1)
            out.append(line)
            continue
        if section is None:
            pj = _PROJECT_RE.match(line)
            if pj:
                out.append(f'{pj.group(1)}"{project_id}"{pj.group(2)}')
                continue
        pm = _PORT_RE.match(line)
        if pm and (section, pm.group(2)) in PORT_KEYS:
            new_port = int(pm.group(4)) + offset
            out.append(f"{pm.group(1)}{pm.group(2)}{pm.group(3)}{new_port}{pm.group(5)}")
            continue
        out.append(line)
    text = "\n".join(out)
    return text + "\n" if config_text.endswith("\n") else text


def default_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True iff a TCP bind on host:port succeeds (nothing is holding it)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_free_offset(base, reserved, is_free, stride: int = 100,
                     max_blocks: int = 20) -> int:
    """Smallest offset = k*stride (k >= 1) where every base port shifted by the
    offset is free and the offset is not already reserved. k starts at 1 so the
    base block (the developer's own dev Supabase) is never claimed."""
    base = list(base)
    for k in range(1, max_blocks + 1):
        offset = k * stride
        if offset in reserved:
            continue
        if all(is_free(p + offset) for p in base):
            return offset
    raise NoFreePortBlock(f"no free Supabase port block within {max_blocks} blocks")


class SupabaseAllocator:
    """Reserves a free Supabase port block per run, recording it in the store so
    concurrent sessions never collide. The reserve step is lock-guarded because
    the FastAPI server is a single process serving many sessions."""

    def __init__(self, store, is_free=default_is_free, stride: int = 100,
                 max_blocks: int = 20, lock=None):
        self.store = store
        self.is_free = is_free
        self.stride = stride
        self.max_blocks = max_blocks
        self._lock = lock or threading.Lock()

    def reserve(self, run_id: str, config_text: str, project: str) -> int:
        with self._lock:
            # Idempotent: a run keeps its block across re-provisions/wakes. Without
            # this, a re-reserve treats the run's own offset as occupied and picks
            # a NEW block, desyncing config.toml/Supabase (rewritten to the new
            # offset) from the stored offset the proxy routes by -> Caddy proxies
            # /auth to a dead port -> 502.
            existing = self.store.get_supabase(run_id)
            if existing:
                return existing["offset"]
            reserved = {r["offset"] for r in self.store.list_supabase()}
            offset = find_free_offset(base_ports(config_text), reserved,
                                      self.is_free, self.stride, self.max_blocks)
            self.store.reserve_supabase(run_id, offset, project)
            return offset

    def release(self, run_id: str) -> None:
        with self._lock:
            self.store.release_supabase(run_id)

    def reconcile(self, active_run_ids) -> list:
        """Release reservations whose run is no longer active; return their ids."""
        active = set(active_run_ids)
        with self._lock:
            stale = [r["run_id"] for r in self.store.list_supabase()
                     if r["run_id"] not in active]
            for run_id in stale:
                self.store.release_supabase(run_id)
            return stale
