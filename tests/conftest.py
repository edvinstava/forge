import pytest


@pytest.fixture(autouse=True)
def _isolate_forge_config(tmp_path_factory, monkeypatch):
    """Point FORGE_CONFIG at a guaranteed-nonexistent path so Config.from_env's
    file load is a no-op unless a test opts in by setting FORGE_CONFIG itself."""
    missing = tmp_path_factory.mktemp("noforge") / "config.env"
    monkeypatch.setenv("FORGE_CONFIG", str(missing))
