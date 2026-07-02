from forge.retrospective import parse_lessons, MAX_PER_RUN


def test_parse_lessons_valid():
    out = parse_lessons('{"lessons":[{"text":"use pnpm","kind":"build","evidence":"lockfile"},'
                        '{"text":"no kind here"}]}')
    assert out[0] == {"text": "use pnpm", "kind": "build", "evidence": "lockfile"}
    assert out[1]["kind"] == "gotcha"          # default kind
    assert out[1]["evidence"] == ""


def test_parse_lessons_skips_textless_and_invalid():
    assert parse_lessons('{"lessons":[{"kind":"build"}, "junk", 5]}') == []
    assert parse_lessons("not json") == []
    assert parse_lessons("[1,2]") == []        # non-dict


def test_parse_lessons_caps_per_run():
    many = '{"lessons":[' + ",".join(f'{{"text":"l{i}"}}' for i in range(20)) + ']}'
    assert len(parse_lessons(many)) == MAX_PER_RUN
