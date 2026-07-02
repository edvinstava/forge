def health_poll_argv(port, path, timeout_secs, host="localhost") -> list:
    """Bash argv (run inside a container) that polls the app until it serves
    a 2xx/3xx, or fails after `timeout_secs` seconds. `host` is the target
    hostname — `localhost` for same-container, or a compose service name when
    polling across the project network."""
    url = f"http://{host}:{port}{path}"
    script = (
        f'for i in $(seq 1 {timeout_secs}); do '
        f'if curl -fs -o /dev/null "{url}"; then exit 0; fi; sleep 1; done; '
        f'echo "health timeout: {url}" >&2; exit 1'
    )
    return ["bash", "-lc", script]
