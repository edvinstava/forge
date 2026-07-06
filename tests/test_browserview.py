import json
import time
from forge import browserview


class RecordingEnv:
    def __init__(self):
        self.detached = []

    def exec_detached(self, argv, workdir="/work", service=None):
        self.detached.append((argv, service))


class ExplodingEnv:
    def exec_detached(self, argv, workdir="/work", service=None):
        raise RuntimeError("docker down")


class NoDetachEnv:
    pass


def test_start_writes_script_clears_stale_files_and_execs(tmp_path):
    d = browserview.live_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    for stale in ("stop", "frame.jpg", "meta.json"):
        (d / stale).write_text("stale")
    env = RecordingEnv()

    assert browserview.start(tmp_path, "r1", env) is True

    assert not (d / "stop").exists()          # a stale stop would kill the new run
    assert not (d / "frame.jpg").exists()     # no stale frame shown as "live"
    script = (d / browserview.SCRIPT_NAME).read_text()
    assert "connectOverCDP" in script         # capital CDP — the actual JS API
    assert str(browserview.CDP_PORT) in script
    (argv, service), = env.detached
    assert service == "forge"
    assert argv[0] == "bash"
    joined = " ".join(argv)
    assert "NODE_PATH" in joined and browserview.SCRIPT_NAME in joined


def test_screencast_skips_blank_only_pages():
    # Executor turns start the screencaster long before the agent first opens a
    # page. Until a page has real content the script must emit NO frames —
    # otherwise the workspace pane flips to a white about:blank shot the moment
    # the turn starts, hiding the running app for nothing.
    assert "about:blank" in browserview.SCREENCAST_JS
    assert ("return busy.length ? busy[busy.length - 1] : null"
            in browserview.SCREENCAST_JS)


def test_start_is_best_effort_on_exec_failure(tmp_path):
    assert browserview.start(tmp_path, "r1", ExplodingEnv()) is False


def test_start_is_best_effort_without_exec_detached(tmp_path):
    assert browserview.start(tmp_path, "r1", NoDetachEnv()) is False


def test_stop_touches_stop_file(tmp_path):
    d = browserview.live_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    browserview.stop(tmp_path, "r1")
    assert (d / "stop").exists()


def test_stop_noop_when_never_started(tmp_path):
    browserview.stop(tmp_path, "r1")   # no dir → nothing to do, no raise
    assert not browserview.live_dir(tmp_path, "r1").exists()


def test_status_inactive_when_no_frame(tmp_path):
    assert browserview.status(tmp_path, "r1") == {
        "active": False, "ts": 0, "url": "", "title": ""}


def test_status_active_with_fresh_frame_and_meta(tmp_path):
    d = browserview.live_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    (d / "frame.jpg").write_bytes(b"\xff\xd8jpeg")
    (d / "meta.json").write_text(json.dumps(
        {"url": "http://web:3000/login", "title": "Sign in", "ts": 1, "seq": 9}))

    s = browserview.status(tmp_path, "r1")

    assert s["active"] is True
    assert s["url"] == "http://web:3000/login"
    assert s["title"] == "Sign in"
    assert s["ts"] > 0                        # <img> cache-buster


def test_status_stale_frame_is_inactive_but_keeps_ts(tmp_path):
    d = browserview.live_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    (d / "frame.jpg").write_bytes(b"\xff\xd8jpeg")

    s = browserview.status(tmp_path, "r1",
                           now=time.time() + browserview.FRESH_SECS + 1)

    assert s["active"] is False
    assert s["ts"] > 0


def test_status_survives_bad_meta_json(tmp_path):
    d = browserview.live_dir(tmp_path, "r1")
    d.mkdir(parents=True)
    (d / "frame.jpg").write_bytes(b"\xff\xd8jpeg")
    (d / "meta.json").write_text("{not json")
    s = browserview.status(tmp_path, "r1")
    assert s["active"] is True and s["url"] == ""
