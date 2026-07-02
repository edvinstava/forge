import pytest
from forge import inbox


def test_save_writes_and_returns_stored_name(tmp_path):
    name = inbox.save(tmp_path, "r1", "bug shot.png", b"\x89PNG data")
    p = tmp_path / "r1" / "inbox" / name
    assert p.is_file() and p.read_bytes() == b"\x89PNG data"
    assert name.endswith("bug_shot.png")


def test_save_sanitizes_traversal_names(tmp_path):
    name = inbox.save(tmp_path, "r1", "../../etc/passwd.png", b"x")
    assert "/" not in name and ".." not in name
    assert (tmp_path / "r1" / "inbox" / name).is_file()


def test_save_rejects_non_image_extension(tmp_path):
    with pytest.raises(ValueError):
        inbox.save(tmp_path, "r1", "notes.txt", b"hello")


def test_save_derives_extension_from_mimetype(tmp_path):
    name = inbox.save(tmp_path, "r1", "pasted-image", b"x", mimetype="image/png")
    assert name.endswith(".png")


def test_save_rejects_oversize(tmp_path):
    with pytest.raises(ValueError):
        inbox.save(tmp_path, "r1", "big.png", b"x" * (inbox.MAX_BYTES + 1))


def test_save_never_overwrites(tmp_path):
    a = inbox.save(tmp_path, "r1", "a.png", b"1")
    b = inbox.save(tmp_path, "r1", "a.png", b"2")
    assert a != b
    d = tmp_path / "r1" / "inbox"
    assert (d / a).read_bytes() == b"1" and (d / b).read_bytes() == b"2"


def test_sync_copies_into_workspace_and_returns_container_paths(tmp_path):
    name = inbox.save(tmp_path, "r1", "a.png", b"1")
    (tmp_path / "r1" / "workspace").mkdir(parents=True)
    paths = inbox.sync(tmp_path, "r1", [name])
    assert paths == [f"/work/.forge/inbox/{name}"]
    assert (tmp_path / "r1" / "workspace" / ".forge" / "inbox" / name).read_bytes() == b"1"


def test_sync_skips_missing_and_suspicious_names(tmp_path):
    (tmp_path / "r1" / "workspace").mkdir(parents=True)
    assert inbox.sync(tmp_path, "r1", ["nope.png", "../evil.png", ".hidden"]) == []


def test_sync_empty_names_is_noop(tmp_path):
    assert inbox.sync(tmp_path, "r1", []) == []
    assert inbox.sync(tmp_path, "r1", None) == []
