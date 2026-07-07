import json
from forge.worker import parse_stream_line, workspace_relpath


def test_assistant_text_is_narration():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": "Reading files"}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "narration"
    assert ev.text == "Reading files"


def test_tool_use_is_tool_event():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                "input": {"file_path": "src/a.ts"}}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "tool"
    assert "Edit" in ev.text
    # The target is the file basename so the UI can render "Edit  a.ts".
    assert ev.target == "a.ts"


def test_tool_use_target_for_bash_is_first_command_line():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                "input": {"command": "npm test\nnpm run lint"}}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "tool" and ev.text == "Bash"
    assert ev.target == "npm test"


def test_tool_use_target_for_grep_is_pattern():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Grep",
                                                "input": {"pattern": "TODO"}}]}})
    ev = parse_stream_line(line)
    assert ev.target == "TODO"


def test_tool_use_without_input_has_empty_target():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Tool"}]}})
    ev = parse_stream_line(line)
    assert ev.kind == "tool" and ev.target == ""


def test_result_line_carries_worker_result():
    line = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                       "session_id": "sess-1", "result": "all done",
                       "total_cost_usd": 0.2, "num_turns": 3,
                       "usage": {"input_tokens": 10, "output_tokens": 5}})
    ev = parse_stream_line(line)
    assert ev.kind == "result"
    assert ev.result.session_id == "sess-1"
    assert ev.result.ok is True


def test_blank_line_returns_none():
    assert parse_stream_line("") is None
    assert parse_stream_line("not json") is None


def test_tool_use_carries_workspace_relative_path():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                "input": {"file_path": "/work/src/app/page.tsx"}}]}})
    ev = parse_stream_line(line)
    assert ev.path == "src/app/page.tsx"
    assert ev.target == "page.tsx"


def test_tool_path_outside_workspace_is_dropped():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Read",
                                                "input": {"file_path": "/etc/passwd"}}]}})
    ev = parse_stream_line(line)
    assert ev.path == ""


def test_tool_path_relative_and_dotted_forms_normalize():
    assert workspace_relpath("./src/a.ts") == "src/a.ts"
    assert workspace_relpath("src/a.ts") == "src/a.ts"
    assert workspace_relpath("/work/a.ts") == "a.ts"
    assert workspace_relpath("/work") == ""
    assert workspace_relpath("") == ""


def test_non_file_tools_have_no_path():
    line = json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                "input": {"command": "rm -rf /work/src"}}]}})
    ev = parse_stream_line(line)
    assert ev.path == ""
