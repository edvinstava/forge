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
