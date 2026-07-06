"""Pure argv builders for `docker compose` (v2). Compose files are passed as
absolute paths; secrets travel via the project's env files / `-e`, never argv."""


def project_name(run_id: str) -> str:
    return f"forge-{run_id}"


def _base(project: str, files: list) -> list:
    cmd = ["docker", "compose", "-p", project]
    for f in files:
        cmd += ["-f", str(f)]
    return cmd


def up_cmd(project: str, files: list) -> list:
    return _base(project, files) + ["up", "-d", "--remove-orphans"]


def down_cmd(project: str, files: list) -> list:
    # -v drops the project's named volumes (disposable env)
    return _base(project, files) + ["down", "-v", "--remove-orphans"]


def stop_cmd(project: str, files: list) -> list:
    # Stop containers but keep them + named volumes (warm snapshot for fast wake)
    return _base(project, files) + ["stop"]


def start_cmd(project: str, files: list) -> list:
    # Restart previously-stopped containers (no image build / container recreate)
    return _base(project, files) + ["start"]


def restart_cmd(project: str, files: list, service: str) -> list:
    # Restart a SINGLE service in place (self-heal restarts the web/dev server
    # after clearing its cache, leaving the worker + supabase untouched).
    return _base(project, files) + ["restart", service]


def exec_cmd(project: str, files: list, service: str, argv: list,
             workdir: str = "/work", env_keys=()) -> list:
    # -T disables TTY allocation so output is captured cleanly. env_keys are
    # passed NAME-ONLY (`-e KEY`): docker forwards each value from the client
    # process env, so per-exec secrets never appear in argv / `ps`.
    cmd = _base(project, files) + ["exec", "-T", "-w", workdir]
    for k in env_keys:
        cmd += ["-e", k]
    return cmd + [service] + list(argv)


def port_cmd(project: str, files: list, service: str, container_port: int) -> list:
    return _base(project, files) + ["port", service, str(container_port)]


def logs_cmd(project: str, files: list, service: str | None = None) -> list:
    cmd = _base(project, files) + ["logs", "--no-color", "--tail", "200"]
    if service:
        cmd.append(service)
    return cmd
