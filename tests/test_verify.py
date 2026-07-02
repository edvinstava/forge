import json
from forge.verify import parse_verify


def test_npm_scripts_detected():
    pj = json.dumps({"scripts": {"test": "jest", "build": "tsc", "start": "x"}})
    plan = parse_verify(pj, None, False)
    names = [c.name for c in plan.commands]
    assert names == ["build", "test"]           # only known checks, static first
    argv = {c.name: c.argv for c in plan.commands}
    assert argv["build"] == ["npm", "run", "build"]
    assert argv["test"] == ["npm", "test"]       # npm's test shortcut is preserved
    assert plan.has_real_verification is True


def test_e2e_test_script_is_skipped():
    # playwright/cypress suites need a live app + browser and are gated
    # conditionally in CI — they must not block every PR via the pre-push gate.
    pj = json.dumps({"scripts": {"test": "playwright test", "build": "next build"}})
    plan = parse_verify(pj, None, False)
    names = [c.name for c in plan.commands]
    assert "test" not in names
    assert "build" in names


def test_unit_test_script_is_kept():
    pj = json.dumps({"scripts": {"test": "vitest run"}})
    plan = parse_verify(pj, None, False)
    assert any(c.name == "test" for c in plan.commands)


def test_ts_check_alias_detected():
    # webapp names its type check `ts:check`, not `typecheck`.
    pj = json.dumps({"scripts": {"ts:check": "tsc --noEmit"}})
    plan = parse_verify(pj, None, False)
    cmd = next(c for c in plan.commands if c.name == "typecheck")
    assert cmd.argv == ["npm", "run", "ts:check"]


def test_format_check_alias_detected():
    # The read-only prettier check (`format:check`) is a real verification.
    pj = json.dumps({"scripts": {"format:check": "prettier --check ."}})
    plan = parse_verify(pj, None, False)
    cmd = next(c for c in plan.commands if c.name == "format")
    assert cmd.argv == ["npm", "run", "format:check"]
    assert plan.has_real_verification is True


def test_format_write_is_not_treated_as_a_check():
    # `format` (prettier --write) mutates files — it must NEVER be run as a
    # read-only verification, only surfaced as the auto-format command.
    pj = json.dumps({"scripts": {"format": "prettier --write ."}})
    plan = parse_verify(pj, None, False)
    assert [c.name for c in plan.commands] == []
    assert plan.has_real_verification is False
    assert plan.format_fix is not None
    assert plan.format_fix.argv == ["npm", "run", "format"]


def test_format_fix_prefers_dedicated_write_script():
    pj = json.dumps({"scripts": {"build": "x", "lint:fix": "eslint . --fix"}})
    plan = parse_verify(pj, None, False)
    # Only lint:fix available as a fixer.
    assert plan.format_fix.argv == ["npm", "run", "lint:fix"]


def test_pkg_manager_is_honored():
    pj = json.dumps({"scripts": {"lint": "eslint .", "build": "next build"}})
    plan = parse_verify(pj, None, False, pkg_manager="bun")
    argv = {c.name: c.argv for c in plan.commands}
    assert argv["lint"] == ["bun", "run", "lint"]
    assert argv["build"] == ["bun", "run", "build"]


def test_pkg_manager_bun_uses_run_for_test():
    # `bun test` is bun's own runner, NOT the package.json test script — so
    # bun must use `bun run test` (unlike npm's `npm test` shortcut).
    pj = json.dumps({"scripts": {"test": "vitest"}})
    plan = parse_verify(pj, None, False, pkg_manager="bun")
    assert plan.commands[0].argv == ["bun", "run", "test"]


def test_verify_sh_wins():
    plan = parse_verify(json.dumps({"scripts": {"test": "jest"}}), None, True)
    assert plan.commands[0].argv == ["bash", ".forge/verify.sh"]
    assert len(plan.commands) == 1
    assert plan.format_fix is None


def test_repo_yml_command():
    plan = parse_verify(None, "verification:\n  command: yarn verify\n", False)
    assert plan.commands[0].argv == ["yarn", "verify"]
    assert plan.format_fix is None


def test_nothing_detected_is_not_real():
    plan = parse_verify(json.dumps({"scripts": {"start": "x"}}), None, False)
    assert plan.commands == []
    assert plan.has_real_verification is False
    assert plan.format_fix is None
