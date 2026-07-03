import json
import shutil
import subprocess

import pytest

from forge.nextdev import (DEV_ORIGINS, ensure_dev_origins, inject,
                           server_action_origins)


def _has_all_origins(text):
    return all(o in text for o in DEV_ORIGINS)


def _eval_cjs_config(source, tmp_path):
    """Evaluate an injected CommonJS next.config with node and return its config
    object as parsed JSON. This tests the RUNTIME effect of injection (the
    appended statements actually mutate the exported object), not just substrings.
    Skips when node isn't available."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    cfg = tmp_path / "next.config.js"
    cfg.write_text(source)
    script = (f"const c = require({json.dumps(str(cfg))});"
              "process.stdout.write(JSON.stringify("
              "typeof c === 'function' ? {} : c));")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_inject_cjs_named_export():
    text = "const nextConfig = { images: {} };\nmodule.exports = nextConfig;\n"
    out = inject(text)
    assert out is not None
    assert "allowedDevOrigins" in out
    assert _has_all_origins(out)
    # original config is preserved
    assert "module.exports = nextConfig;" in out


def test_inject_cjs_inline_object():
    text = "module.exports = { images: {} };\n"
    out = inject(text)
    assert out is not None and "allowedDevOrigins" in out and _has_all_origins(out)


def test_inject_esm_named_default_export():
    text = "const nextConfig = { images: {} };\nexport default nextConfig;\n"
    out = inject(text)
    assert out is not None
    assert "allowedDevOrigins" in out and _has_all_origins(out)
    assert "nextConfig.allowedDevOrigins" in out  # mutates the exported object


def test_inject_idempotent_on_forge_marker():
    # Re-injecting forge's own output is a no-op (detected by the marker), so
    # provision + every wake stays idempotent.
    text = "module.exports = {};\n"
    once = inject(text)
    assert once is not None
    assert inject(once) is None


def test_inject_patches_user_config_that_already_sets_dev_origins():
    # A user-defined allowedDevOrigins (no forge marker) must STILL be patched:
    # they need the serverActions fix too, and the dev-origins union is safe.
    text = ("const nextConfig = { allowedDevOrigins: ['x'] };\n"
            "module.exports = nextConfig;\n")
    out = inject(text)
    assert out is not None
    assert "serverActions" in out


def test_inject_skips_unrecognized_export_shape():
    # function config / wrapped export we can't safely mutate by appending
    text = "export default withPlugins({ images: {} });\n"
    assert inject(text) is None


def test_inject_merges_with_existing_array_at_runtime():
    # The appended code unions with any pre-existing array, so a config that adds
    # origins via spread elsewhere isn't clobbered. We assert the union helper is
    # used rather than a bare assignment.
    text = "const c = {};\nmodule.exports = c;\n"
    out = inject(text)
    assert "Set(" in out and "allowedDevOrigins" in out


# ── serverActions.allowedOrigins (CSRF) — a SEPARATE guard from dev origins ──
# Next aborts Server Actions whose Origin host != Host ("Invalid Server Actions
# request"). That match keeps the PORT, so the local proxy URL needs a
# port-bearing wildcard (unlike allowedDevOrigins, matched on hostname only).

def test_server_action_origins_includes_port_bearing_local_pattern():
    origins = server_action_origins("forge.localhost", 8088)
    assert "*.forge.localhost:8088" in origins   # matches run-<id>.forge.localhost:8088
    assert "*.trycloudflare.com" in origins       # tunnel host carries no port


def test_server_action_origins_respects_custom_domain_and_port():
    origins = server_action_origins("dev.example", 9000)
    assert "*.dev.example:9000" in origins


def test_inject_adds_server_actions_allowed_origins_under_experimental():
    out = inject("module.exports = { images: {} };\n")
    assert out is not None
    assert "experimental" in out and "serverActions" in out
    assert "allowedOrigins" in out


def test_injected_config_runtime_has_server_action_origins(tmp_path):
    src = inject("module.exports = { images: {} };\n",
                 sa_origins=server_action_origins("forge.localhost", 8088))
    cfg = _eval_cjs_config(src, tmp_path)
    origins = cfg["experimental"]["serverActions"]["allowedOrigins"]
    assert "*.forge.localhost:8088" in origins
    assert "*.trycloudflare.com" in origins
    assert cfg["images"] == {}                    # original config preserved


def test_injected_config_runtime_still_has_dev_origins(tmp_path):
    cfg = _eval_cjs_config(inject("module.exports = {};\n"), tmp_path)
    assert "*.forge.localhost" in cfg["allowedDevOrigins"]


def test_injected_config_runtime_unions_existing_dev_origins(tmp_path):
    src = inject("module.exports = { allowedDevOrigins: ['mine.test'] };\n")
    cfg = _eval_cjs_config(src, tmp_path)
    assert "mine.test" in cfg["allowedDevOrigins"]
    assert "*.forge.localhost" in cfg["allowedDevOrigins"]


def test_injected_config_runtime_coerces_legacy_serverActions_true(tmp_path):
    # Legacy `serverActions: true` must be replaced with an object, not have a
    # property assigned onto a boolean primitive.
    src = inject("module.exports = { experimental: { serverActions: true } };\n")
    cfg = _eval_cjs_config(src, tmp_path)
    assert isinstance(cfg["experimental"]["serverActions"], dict)
    assert "*.trycloudflare.com" in cfg["experimental"]["serverActions"]["allowedOrigins"]


def test_injected_config_runtime_preserves_existing_experimental_keys(tmp_path):
    src = inject("module.exports = { experimental: { typedRoutes: true } };\n")
    cfg = _eval_cjs_config(src, tmp_path)
    assert cfg["experimental"]["typedRoutes"] is True
    assert "allowedOrigins" in cfg["experimental"]["serverActions"]


# ── ensure_dev_origins (workspace integration via a tiny fake host) ──

class FakeHost:
    def __init__(self, files):
        self.files = dict(files)          # relpath -> content
        self.written = {}                 # full path -> content

    def exists(self, ws, rel):
        return rel in self.files

    def read(self, ws, rel):
        return self.files.get(rel)

    def write_file(self, path, content):
        self.written[path] = content
        # reflect back so a re-read sees the change (idempotency tests)
        for rel in list(self.files):
            if path.endswith(rel):
                self.files[rel] = content


def test_ensure_patches_existing_next_config():
    host = FakeHost({"next.config.js":
                     "const c = {};\nmodule.exports = c;\n"})
    changed = ensure_dev_origins(host, "/ws")
    assert changed is True
    written = next(iter(host.written.values()))
    assert "allowedDevOrigins" in written and _has_all_origins(written)


def test_ensure_is_noop_when_already_marked():
    # Already carries forge's marker (a prior provision/wake) -> no rewrite.
    marked = inject("module.exports = {};\n")
    host = FakeHost({"next.config.js": marked})
    assert ensure_dev_origins(host, "/ws") is False
    assert host.written == {}


def test_ensure_creates_config_for_next_app_without_one():
    host = FakeHost({"package.json":
                     json.dumps({"dependencies": {"next": "16.2.9"}})})
    assert ensure_dev_origins(host, "/ws") is True
    written = next(iter(host.written.values()))
    assert "allowedDevOrigins" in written and _has_all_origins(written)
    assert "serverActions" in written and "allowedOrigins" in written


def test_ensure_threads_proxy_domain_and_port_into_server_actions(tmp_path):
    host = FakeHost({"next.config.js": "module.exports = {};\n"})
    assert ensure_dev_origins(host, "/ws", proxy_domain="dev.example",
                              proxy_port=9000) is True
    written = next(iter(host.written.values()))
    cfg = _eval_cjs_config(written, tmp_path)
    assert "*.dev.example:9000" in cfg["experimental"]["serverActions"]["allowedOrigins"]


def test_ensure_skips_non_next_project():
    host = FakeHost({"package.json":
                     json.dumps({"dependencies": {"react": "19"}})})
    assert ensure_dev_origins(host, "/ws") is False
    assert host.written == {}


class GitHost(FakeHost):
    def __init__(self, files, tracked=True):
        super().__init__(files)
        self.ran = []
        self._tracked = tracked

    def run(self, argv, env=None):
        from forge.container import ExecResult
        self.ran.append(argv)
        if "ls-files" in argv:
            return ExecResult(0 if self._tracked else 1, "", "")
        return ExecResult(0, "", "")


def test_ensure_hides_patched_config_from_git():
    # The origin patch is forge infrastructure — it must never appear in the
    # worker's diff or the PR (bit us in acme/webapp#222).
    from forge.hostops import hardened_git
    host = GitHost({"next.config.js": "module.exports = {};\n"})
    assert ensure_dev_origins(host, "/ws") is True
    assert hardened_git("/ws", "update-index", "--skip-worktree",
                        "next.config.js") in host.ran


def test_ensure_rehides_already_marked_config():
    # A workspace patched by an older forge (marker present, never hidden)
    # gets the hide re-asserted on wake even though no rewrite happens.
    marked = inject("module.exports = {};\n")
    host = GitHost({"next.config.js": marked})
    assert ensure_dev_origins(host, "/ws") is False
    assert any("--skip-worktree" in argv for argv in host.ran)


def test_ensure_excludes_fresh_config_it_created():
    host = GitHost({"package.json":
                     json.dumps({"dependencies": {"next": "16.2.9"}})})
    assert ensure_dev_origins(host, "/ws") is True
    exclude = host.written.get("/ws/.git/info/exclude")
    assert exclude is not None and "/next.config.js" in exclude


def test_unpatch_round_trips_injected_config():
    from forge.nextdev import unpatch
    original = "const c = { images: {} };\nmodule.exports = c;\n"
    assert unpatch(inject(original)) == original
    assert unpatch(original) is None                # no marker → not ours


def test_unpatch_survives_formatter_rewrap():
    # A repo formatter (prettier) may rewrap the injected statements, but they
    # stay below the marker line — stripping from the marker must still work.
    from forge.nextdev import unpatch, _MARKER
    original = "module.exports = { images: {} };\n"
    patched = inject(original)
    idx = patched.find(f"// {_MARKER}")
    reformatted = patched[:idx] + patched[idx:].replace(", ", ",\n    ")
    assert unpatch(reformatted) == original


def test_unpatch_for_commit_strips_tracked_config_and_unhides():
    from forge.nextdev import unpatch_for_commit, _MARKER
    from forge.hostops import hardened_git
    original = "module.exports = { images: {} };\n"
    host = GitHost({"next.config.js": inject(original)})
    assert unpatch_for_commit(host, "/ws") == ["next.config.js"]
    assert hardened_git("/ws", "update-index", "--no-skip-worktree",
                        "next.config.js") in host.ran
    assert host.files["next.config.js"] == original
    assert _MARKER not in host.files["next.config.js"]


def test_unpatch_for_commit_leaves_fresh_untracked_config_alone():
    # Forge-created configs are untracked + excluded: stripping would gut them
    # and git ignores them anyway.
    from forge.nextdev import unpatch_for_commit
    host = GitHost({"package.json":
                    json.dumps({"dependencies": {"next": "16.2.9"}})},
                   tracked=False)
    ensure_dev_origins(host, "/ws")
    # the fake only reflects writes for pre-seeded relpaths — adopt the created file
    host.files["next.config.js"] = host.written["/ws/next.config.js"]
    patched = host.files["next.config.js"]
    assert unpatch_for_commit(host, "/ws") == []
    assert host.files["next.config.js"] == patched
