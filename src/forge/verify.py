import json
import re
from dataclasses import dataclass

# Canonical check -> ordered candidate package.json script names. The first
# script that exists wins, so list the most specific/idiomatic name first.
# Real repos don't agree on naming (typecheck vs ts:check vs tsc; format:check
# vs prettier:check), and CI gates on whatever name they chose — so we cast a
# wide net here. ONLY read-only checks belong in this map; the auto-fixing
# variants (prettier --write, eslint --fix) live in _FORMAT_FIX so we never run
# a mutating script as if it were a verification. Static checks come before the
# slower build/test so the fix loop sees the cheap failures first.
_CHECKS = [
    ("lint", ["lint", "lint:check", "eslint"]),
    ("typecheck", ["typecheck", "type-check", "ts:check", "tsc", "types",
                   "types:check", "check-types", "typescript"]),
    ("format", ["format:check", "fmt:check", "prettier:check", "format-check",
                "check-format"]),
    ("build", ["build"]),
    ("test", ["test"]),
]

# A `test` script driving a browser/e2e runner needs a live app + browser and
# is gated conditionally in CI (not on every commit). Running it as a pre-push
# gate would be slow and flaky and would draft every PR — so skip it here. Unit
# runners (jest/vitest/mocha/node --test) stay in the gate.
_E2E = re.compile(r"playwright|cypress|webdriver|\be2e\b|\bnightwatch\b", re.I)

# Scripts that auto-fix formatting/lint. Run (best-effort) before committing so
# deterministic style issues never reach CI. Ordered by preference: a dedicated
# write/format script over a lint --fix.
_FORMAT_FIX = ["format", "format:write", "format:fix", "fmt", "prettier:write",
               "prettier:fix", "lint:fix"]

# How each package manager runs a named script. `<pm> run <script>` is valid
# for all four; npm additionally has the bare `npm test` shortcut (see below).
_RUN = {
    "npm": ["npm", "run"],
    "pnpm": ["pnpm", "run"],
    "yarn": ["yarn"],
    "bun": ["bun", "run"],
}


@dataclass(frozen=True)
class VerifyCmd:
    name: str
    argv: list


@dataclass(frozen=True)
class VerifyPlan:
    commands: list
    has_real_verification: bool
    # Best-effort deterministic formatter (e.g. `npm run format`), run before
    # committing so style-only diffs don't fail CI. None when the repo has no
    # auto-fix script. Never one of `commands` — it mutates the tree.
    format_fix: "VerifyCmd | None" = None


def _script_argv(pkg_manager: str, script: str) -> list:
    run = _RUN.get(pkg_manager, _RUN["npm"])
    # `npm test` is the conventional shortcut and what existing recipes expect;
    # bun/yarn/pnpm must go through `run` (notably `bun test` is bun's OWN test
    # runner, not the package.json script).
    if pkg_manager == "npm" and script == "test":
        return ["npm", "test"]
    return [*run, script]


def _repo_yml_command(repo_yml: str) -> list | None:
    # Minimal extraction: a line 'command: <cmd>' under 'verification:'.
    m = re.search(r"verification:\s*\n(?:\s+.*\n)*?\s+command:\s*(.+)", repo_yml)
    if not m:
        m = re.search(r"^\s*command:\s*(.+)$", repo_yml, re.MULTILINE)
    return m.group(1).strip().split() if m else None


def parse_verify(package_json, repo_yml, has_verify_sh,
                 pkg_manager="npm") -> VerifyPlan:
    if has_verify_sh:
        return VerifyPlan([VerifyCmd("verify.sh", ["bash", ".forge/verify.sh"])], True)
    if repo_yml:
        cmd = _repo_yml_command(repo_yml)
        if cmd:
            return VerifyPlan([VerifyCmd("repo.yml", cmd)], True)
    scripts = {}
    if package_json:
        try:
            scripts = json.loads(package_json).get("scripts", {}) or {}
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
    cmds = []
    for canonical, aliases in _CHECKS:
        script = next((a for a in aliases if a in scripts), None)
        if not script:
            continue
        if canonical == "test" and _E2E.search(scripts.get(script) or ""):
            continue   # browser/e2e suite — not a per-push gate (see _E2E)
        cmds.append(VerifyCmd(canonical, _script_argv(pkg_manager, script)))
    fix = next((s for s in _FORMAT_FIX if s in scripts), None)
    format_fix = VerifyCmd(fix, _script_argv(pkg_manager, fix)) if fix else None
    return VerifyPlan(cmds, len(cmds) > 0, format_fix)
