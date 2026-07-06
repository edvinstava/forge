"""Detect a Next.js/Turbopack dev server stuck 5xx because its persistent cache
is corrupted, and recover it (clear .next + restart the web service).

A `.meta` in `.next/dev/cache/turbopack/` can end up referencing an `.sst` file
that no longer exists; Turbopack then panics and EVERY route 500s indefinitely.
The container is up (returns 500/307, not connection-refused) but the app is
unusable, and it silently blocks the agent's own QA. A plain restart does NOT
fix it (the web entrypoint clears only `.next/dev/lock`, not the cache), so the
recovery must delete `.next` before restarting.

This module holds the pure, testable pieces; SessionManager.heal_corrupted_web
drives them from the reap loop."""

# Substrings that identify the Turbopack persistent-cache corruption in the dev
# server's logs. Matching on the log (not just an HTTP 500) keeps the heal
# surgical: a genuine application 500 (a bug the agent wrote) carries none of
# these, so it is left alone rather than pointlessly restarted.
CORRUPTION_MARKERS = (
    "TurbopackInternalError",
    "Failed to open SST file",
    "Unable to open static sorted file",
)


def is_corruption(log_text: str | None) -> bool:
    """True when the dev-server log carries the Turbopack cache-corruption
    signature (a missing .sst, or an ENOENT on a `.next/dev` build-manifest)."""
    if not log_text:
        return False
    if any(m in log_text for m in CORRUPTION_MARKERS):
        return True
    # The same corruption also surfaces as a missing build manifest under
    # `.next/dev/…/build-manifest.json`.
    return ("ENOENT" in log_text and ".next/dev" in log_text
            and "build-manifest.json" in log_text)


def status_probe_argv(host: str, port, path: str) -> list:
    """curl argv (run from the worker, over the compose network) that prints
    ONLY the final HTTP status code. `-L` follows the app's auth redirect
    (e.g. / → /sign-in) so we reach a route that actually compiles — under
    corruption the redirect itself is a healthy 307 while its destination 500s.
    --max-redirs / --max-time bound a misbehaving app."""
    url = f"http://{host}:{port}{path}"
    return ["curl", "-sL", "--max-redirs", "5", "--max-time", "10",
            "-o", "/dev/null", "-w", "%{http_code}", url]


def is_server_error(status: str | None) -> bool:
    """True for a 5xx status string. `000` (curl's connection-failure code),
    empty, or any non-5xx is False — a container that's down or merely
    redirecting is not this failure mode."""
    s = (status or "").strip()
    return len(s) == 3 and s[0] == "5"


def is_reachable(status: str | None) -> bool:
    """True when curl reported a real HTTP status (any 3-digit code). `000`
    (connection failure) and empty output mean the app never answered."""
    s = (status or "").strip()
    return len(s) == 3 and s.isdigit() and s != "000"


def clear_cache_argv() -> list:
    """argv (run in the workspace, workdir=/work) that deletes the corrupted
    Next build cache so the restarted dev server rebuilds it clean."""
    return ["rm", "-rf", ".next"]
