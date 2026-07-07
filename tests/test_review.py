from forge.review import (Comment, Review, parse_review, diff_line_map,
                          partition, build_payload, parse_review_url)

DIFF = """diff --git a/foo.py b/foo.py
index 111..222 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)
"""


def test_parse_review_from_json_string():
    r = parse_review('{"summary": "ok", "comments": '
                     '[{"path": "foo.py", "line": 3, "body": "nit"}]}')
    assert r.summary == "ok"
    assert r.comments[0] == Comment("foo.py", 3, "RIGHT", "nit")  # side defaults RIGHT


def test_parse_review_tolerant_of_garbage_and_missing():
    assert parse_review("not json").summary == ""
    assert parse_review('{"summary": "s"}').comments == []
    # malformed comment entries are skipped, valid ones kept
    r = parse_review('{"summary":"s","comments":[{"line":1},{"path":"a","line":2,"body":"b"}]}')
    assert [c.path for c in r.comments] == ["a"]


def test_diff_line_map_marks_added_removed_context():
    m = diff_line_map(DIFF)["foo.py"]
    assert ("RIGHT", 3) in m and ("RIGHT", 4) in m       # added lines x=2, y=3
    assert ("LEFT", 2) in m                              # removed x=1
    assert ("RIGHT", 1) in m and ("LEFT", 1) in m        # context "import os"
    assert ("RIGHT", 99) not in m


def test_partition_keeps_in_diff_drops_off_diff():
    rev = Review("s", [Comment("foo.py", 3, "RIGHT", "good"),
                       Comment("foo.py", 50, "RIGHT", "off-diff"),
                       Comment("other.py", 1, "RIGHT", "no-file")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert [c.body for c in valid] == ["good"]
    assert {c.body for c in dropped} == {"off-diff", "no-file"}


def test_partition_snaps_near_miss_to_nearest_line():
    # (RIGHT, 6) is off-diff; nearest anchor within ±3 is (RIGHT, 4), dist 2.
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "near miss")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert dropped == []
    assert (valid[0].line, valid[0].side, valid[0].body) == (4, "RIGHT", "near miss")


def test_partition_snap_prefers_right_on_distance_tie():
    # hand-built map: RIGHT 5 and LEFT 5 are equidistant (dist 1) from line 6.
    line_map = {"foo.py": {("RIGHT", 5), ("LEFT", 5)}}
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "tie")])
    valid, _ = partition(rev, line_map)
    assert (valid[0].line, valid[0].side) == (5, "RIGHT")


def test_partition_snap_nearest_beats_farther():
    line_map = {"foo.py": {("RIGHT", 4), ("RIGHT", 7)}}
    rev = Review("s", [Comment("foo.py", 6, "RIGHT", "x")])
    valid, _ = partition(rev, line_map)
    assert valid[0].line == 7          # dist 1 beats dist 2


def test_partition_drops_when_no_anchor_within_window():
    # (RIGHT, 8): nearest anchor (RIGHT, 4) is dist 4 (> 3) → still dropped.
    rev = Review("s", [Comment("foo.py", 8, "RIGHT", "too far"),
                       Comment("other.py", 1, "RIGHT", "no file")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert valid == []
    assert {c.body for c in dropped} == {"too far", "no file"}


def test_partition_exact_anchor_still_valid_unchanged():
    # a directly-anchorable comment is kept as-is (not re-snapped).
    rev = Review("s", [Comment("foo.py", 3, "RIGHT", "exact")])
    valid, dropped = partition(rev, diff_line_map(DIFF))
    assert dropped == [] and valid[0] == Comment("foo.py", 3, "RIGHT", "exact")


def test_build_payload_shape_and_dropped_folding():
    rev = Review("Summary text", [])
    valid = [Comment("foo.py", 3, "RIGHT", "inline note")]
    dropped = [Comment("x.py", 9, "RIGHT", "couldn't anchor")]
    p = build_payload(rev, valid, dropped, header="🔨 Forge Review\n\n")
    assert p["event"] == "COMMENT"
    assert p["body"].startswith("🔨 Forge Review")
    assert "Summary text" in p["body"]
    assert "couldn't anchor" in p["body"]          # dropped folded into body
    assert p["comments"] == [{"path": "foo.py", "line": 3,
                              "side": "RIGHT", "body": "inline note"}]


def test_build_payload_no_dropped_section_when_empty():
    p = build_payload(Review("s", []), [], [])
    assert "couldn't anchor" not in p["body"].lower()
    assert p["comments"] == []


def test_parse_review_url():
    assert parse_review_url('{"html_url": "https://github.com/o/r/pull/1#r9"}') \
        == "https://github.com/o/r/pull/1#r9"
    assert parse_review_url("not json") is None


def test_parse_review_live_check_absent_is_none():
    assert parse_review('{"summary":"s","comments":[]}').live_check is None


def test_parse_review_live_check_malformed_is_none():
    # non-dict, and dict with an unknown status → None (tolerant)
    assert parse_review('{"summary":"s","live_check":"nope"}').live_check is None
    assert parse_review('{"summary":"s","live_check":{"status":"weird"}}').live_check is None


def test_parse_review_live_check_normalized():
    r = parse_review('{"summary":"s","live_check":{"status":"PASS",'
                     '"tested":["login flow", 0, "logout"],"notes":"all good"}}')
    assert r.live_check == {"status": "pass",
                            "tested": ["login flow", "0", "logout"],
                            "notes": "all good"}


def test_build_payload_live_check_section_order_and_emoji():
    rev = Review("Overall solid.", [], {"status": "pass",
                                         "tested": ["ordered a widget"],
                                         "notes": "checkout worked"})
    p = build_payload(rev, [], [], header="🔨 Forge Review\n\n")
    body = p["body"]
    assert body.startswith("🔨 Forge Review")
    # live-check block appears, with the pass emoji, before the summary text
    assert "✅" in body and "Live check" in body
    assert "ordered a widget" in body and "checkout worked" in body
    assert body.index("Live check") < body.index("Overall solid.")


def test_build_payload_live_check_emoji_per_status():
    for status, emoji in [("fail", "❌"), ("skipped", "⏭️"), ("blocked", "🔒")]:
        p = build_payload(Review("s", [], {"status": status, "tested": [],
                                           "notes": ""}), [], [])
        assert emoji in p["body"]


def test_build_payload_omits_live_check_when_absent():
    # old workers / self-review: no live_check → no section, fully compatible
    p = build_payload(Review("just a summary", []), [], [])
    assert "Live check" not in p["body"]
    assert p["body"] == "just a summary"
