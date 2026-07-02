import pytest
from forge.prref import parse_pr_ref, find_pr_ref, PRRef


def test_parses_hash_form():
    r = parse_pr_ref("dhis2/forge#123")
    assert r == PRRef("dhis2", "forge", 123)
    assert r.slug == "dhis2/forge"


def test_parses_url_form():
    r = parse_pr_ref("https://github.com/dhis2/forge/pull/45")
    assert (r.owner, r.repo, r.number) == ("dhis2", "forge", 45)


def test_parses_url_with_trailing_segments_and_slash():
    r = parse_pr_ref("https://github.com/o/r/pull/7/files/")
    assert (r.owner, r.repo, r.number) == ("o", "r", 7)


def test_strips_git_suffix_and_whitespace():
    assert parse_pr_ref("  o/r.git#9 ") == PRRef("o", "r", 9)


@pytest.mark.parametrize("bad", ["", "owner/repo", "not a ref", "o/r#abc", "#3"])
def test_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_pr_ref(bad)


@pytest.mark.parametrize("bad", ["-foo/bar#1", "o/-r#1",
                                 "github.com/-o/r/pull/1"])
def test_rejects_dash_leading_slug(bad):
    # A dash-leading owner/repo could be smuggled as a gh flag downstream.
    with pytest.raises(ValueError):
        parse_pr_ref(bad)


def test_find_pr_ref_extracts_from_free_text():
    assert find_pr_ref("please review o/r#3 now") == PRRef("o", "r", 3)
    assert find_pr_ref("look at https://github.com/dhis2/forge/pull/9") \
        == PRRef("dhis2", "forge", 9)
    assert find_pr_ref("just build me a thing") is None
