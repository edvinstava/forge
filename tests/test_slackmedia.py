import json

from forge.slackmedia import parse_manifest


def _mk(d, name, size=10):
    p = d / name
    p.write_bytes(b"x" * size)
    return p


def _manifest(*entries):
    return json.dumps({"artifacts": list(entries)})


def test_valid_manifest_returns_descriptors(tmp_path):
    _mk(tmp_path, "before.png")
    _mk(tmp_path, "after.png")
    text = _manifest(
        {"path": "before.png", "kind": "before", "caption": "Broken footer"},
        {"path": "after.png", "kind": "after", "caption": "Fixed footer"})
    arts = parse_manifest(text, tmp_path)
    assert [(a.path.name, a.kind, a.caption) for a in arts] == [
        ("before.png", "before", "Broken footer"),
        ("after.png", "after", "Fixed footer")]


def test_rejects_path_traversal_and_absolute(tmp_path):
    _mk(tmp_path, "ok.png")
    text = _manifest(
        {"path": "../secret.png", "kind": "after", "caption": "x"},
        {"path": "/etc/passwd", "kind": "after", "caption": "x"},
        {"path": "sub/ok.png", "kind": "after", "caption": "x"},
        {"path": "ok.png", "kind": "after", "caption": "fine"})
    arts = parse_manifest(text, tmp_path)
    assert [a.path.name for a in arts] == ["ok.png"]


def test_drops_missing_files_and_bad_extensions(tmp_path):
    _mk(tmp_path, "real.png")
    _mk(tmp_path, "evil.sh")
    text = _manifest(
        {"path": "ghost.png", "kind": "after", "caption": "x"},   # not on disk
        {"path": "evil.sh", "kind": "after", "caption": "x"},     # bad ext
        {"path": "real.png", "kind": "after", "caption": "ok"})
    arts = parse_manifest(text, tmp_path)
    assert [a.path.name for a in arts] == ["real.png"]


def test_drops_oversized_files(tmp_path):
    _mk(tmp_path, "huge.mp4", size=9 * 1024 * 1024)   # > 8MB cap
    _mk(tmp_path, "small.png", size=100)
    text = _manifest(
        {"path": "huge.mp4", "kind": "video", "caption": "x"},
        {"path": "small.png", "kind": "after", "caption": "ok"})
    arts = parse_manifest(text, tmp_path)
    assert [a.path.name for a in arts] == ["small.png"]


def test_caps_total_count_at_six(tmp_path):
    entries = []
    for i in range(10):
        _mk(tmp_path, f"shot{i}.png")
        entries.append({"path": f"shot{i}.png", "kind": "after", "caption": str(i)})
    arts = parse_manifest(_manifest(*entries), tmp_path)
    assert len(arts) == 6


def test_garbage_manifest_falls_back_to_glob(tmp_path):
    _mk(tmp_path, "before.png")
    _mk(tmp_path, "after.png")
    arts = parse_manifest("not json {{{", tmp_path)
    kinds = {a.path.name: a.kind for a in arts}
    assert kinds == {"before.png": "before", "after.png": "after"}


def test_missing_manifest_falls_back_and_infers_video(tmp_path):
    _mk(tmp_path, "flow.mp4")
    arts = parse_manifest("", tmp_path)
    assert len(arts) == 1 and arts[0].kind == "video"


def test_nothing_valid_returns_empty(tmp_path):
    arts = parse_manifest("", tmp_path)
    assert arts == []


def test_unknown_kind_in_manifest_is_normalized(tmp_path):
    _mk(tmp_path, "after.png")
    text = _manifest({"path": "after.png", "kind": "weird", "caption": "x"})
    arts = parse_manifest(text, tmp_path)
    assert arts and arts[0].kind in {"before", "after", "video"}
