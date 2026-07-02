from forge.probing import build_probe


class FakeHost:
    def __init__(self, files): self.files = files
    def read(self, ws, rel): return self.files.get(rel)
    def exists(self, ws, rel): return rel in self.files


def test_build_probe_detects_node_and_compose():
    host = FakeHost({"package.json": '{"scripts":{"dev":"vite"}}',
                     "docker-compose.yml": "services: {}"})
    p = build_probe(host, "/ws")
    assert p.package_json and p.repo_compose_path == "docker-compose.yml"
    assert p.repo_compose_text == "services: {}"   # content read for wrapping
    assert not p.is_chap_frontend


def test_build_probe_compose_text_none_without_compose():
    p = build_probe(FakeHost({"package.json": "{}"}), "/ws")
    assert p.repo_compose_path is None and p.repo_compose_text is None


def test_build_probe_detects_chap_frontend():
    host = FakeHost({"d2.config.js": "id: 'a29851f9-...'"})
    p = build_probe(host, "/ws")
    assert p.is_chap_frontend is True


def test_build_probe_detects_bun():
    assert build_probe(FakeHost({"package.json": "{}", "bun.lockb": ""}),
                       "/ws").pkg_manager == "bun"


def test_build_probe_detects_pnpm_yarn_npm():
    assert build_probe(FakeHost({"pnpm-lock.yaml": ""}), "/ws").pkg_manager == "pnpm"
    assert build_probe(FakeHost({"yarn.lock": ""}), "/ws").pkg_manager == "yarn"
    assert build_probe(FakeHost({"package.json": "{}"}), "/ws").pkg_manager == "npm"
