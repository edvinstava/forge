def web_url(host_port) -> str:
    return f"http://localhost:{host_port}"


def superseded_run_ids(live_envs, keep_run_id) -> list:
    """Concurrency-1: every live env other than the one we're keeping is
    superseded and should be reaped."""
    return [e["run_id"] for e in live_envs if e["run_id"] != keep_run_id]
