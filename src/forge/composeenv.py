import os
import subprocess

from forge import compose
from forge.commands import parse_host_port
from forge.container import ExecResult


class ComposeEnv:
    """A per-run Docker Compose project. The repo's app services + a `forge`
    worker service run together on the project network, sharing the cloned
    workspace. The orchestrator execs the worker via this env."""

    def __init__(self, run_id: str, files: list, worker_service: str = "forge",
                 up_timeout: float | None = None, down_timeout: float = 120.0):
        self.run_id = run_id
        self.project = compose.project_name(run_id)
        self.files = [str(f) for f in files]
        self.worker_service = worker_service
        # Cap `compose up`: it pulls/builds images and creates containers (with
        # `-d` it returns before the app's install/dev-server runs, so this does
        # NOT clip a slow `npm install`). A stalled registry or a runaway build
        # would otherwise hang the provisioning thread forever. None = no cap.
        self.up_timeout = up_timeout
        # down() is teardown — including the path the up()-timeout handler takes.
        # Always bounded: against a hung docker daemon an un-timed `compose down`
        # would block forever, re-pinning the thread the up cap meant to free.
        self.down_timeout = down_timeout
        self._proc = None

    def up(self, env: dict | None = None) -> None:
        # secrets reach compose via process env (${VAR} substitution); the
        # compose files on disk hold only references, never values
        proc_env = {**os.environ, **(env or {})}
        try:
            out = subprocess.run(compose.up_cmd(self.project, self.files),
                                 capture_output=True, text=True, env=proc_env,
                                 timeout=self.up_timeout)
        except subprocess.TimeoutExpired:
            # Tear down whatever half-started so the timeout doesn't leak a
            # partial project (containers/networks/volumes) onto the host.
            self.down()
            raise RuntimeError(
                f"compose up timed out after {self.up_timeout}s "
                f"(image pull/build too slow or hung)")
        if out.returncode != 0:
            # stderr can echo env values; keep only the last line and no values
            tail = (out.stderr or out.stdout).strip().splitlines()
            msg = tail[-1] if tail else "unknown error"
            raise RuntimeError(f"compose up failed (exit {out.returncode}): {msg}")

    def exec(self, argv: list, workdir: str = "/work",
             service: str | None = None, env: dict | None = None) -> ExecResult:
        # `env` is for per-exec secrets (e.g. the GitHub token on push): the
        # keys ride as name-only `-e KEY` flags and the values via the client
        # process env — inside the exec'd process only, never argv, never the
        # container's resident environment.
        svc = service or self.worker_service
        out = subprocess.run(
            compose.exec_cmd(self.project, self.files, svc, argv, workdir,
                             env_keys=tuple(env or ())),
            capture_output=True, text=True,
            env={**os.environ, **env} if env else None)
        return ExecResult(out.returncode, out.stdout, out.stderr)

    def exec_detached(self, argv: list, workdir: str = "/work",
                      service: str | None = None) -> None:
        svc = service or self.worker_service
        cmd = compose.exec_cmd(self.project, self.files, svc, argv, workdir)
        cmd.insert(cmd.index("exec") + 1, "-d")
        subprocess.run(cmd, capture_output=True)

    def port(self, service: str, container_port: int) -> int | None:
        out = subprocess.run(
            compose.port_cmd(self.project, self.files, service, container_port),
            capture_output=True, text=True)
        return parse_host_port(out.stdout) if out.returncode == 0 else None

    def logs(self, service: str | None = None) -> str:
        out = subprocess.run(compose.logs_cmd(self.project, self.files, service),
                             capture_output=True, text=True)
        return out.stdout + out.stderr

    def down(self) -> None:
        # Best-effort teardown: a TimeoutExpired (hung daemon) must not propagate
        # — callers (sleep/end/up-timeout handler) don't expect down() to raise.
        try:
            subprocess.run(compose.down_cmd(self.project, self.files),
                           capture_output=True, timeout=self.down_timeout)
        except subprocess.TimeoutExpired:
            pass

    def stop(self) -> None:
        # Warm snapshot: stop containers (keep them + named volumes). Best-effort
        # and bounded, exactly like down() — sleep() must not raise on a hung daemon.
        try:
            subprocess.run(compose.stop_cmd(self.project, self.files),
                           capture_output=True, timeout=self.down_timeout)
        except subprocess.TimeoutExpired:
            pass

    def start(self) -> None:
        # Resume a warm snapshot: restart stopped containers (no build/recreate).
        # Raises on failure so wake() can fall back to a full cold provision.
        try:
            out = subprocess.run(compose.start_cmd(self.project, self.files),
                                 capture_output=True, text=True, timeout=self.down_timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("compose start timed out")
        if out.returncode != 0:
            tail = (out.stderr or out.stdout).strip().splitlines()
            raise RuntimeError(f"compose start failed (exit {out.returncode}): "
                               f"{tail[-1] if tail else 'unknown error'}")

    def exec_stream(self, argv: list, workdir: str = "/work",
                    service: str | None = None):
        svc = service or self.worker_service
        cmd = compose.exec_cmd(self.project, self.files, svc, argv, workdir)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._proc = proc
        try:
            for line in proc.stdout:
                yield line.rstrip("\n")
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
            proc.wait()
            self._proc = None

    def cancel(self) -> None:
        p = getattr(self, "_proc", None)
        if p and p.poll() is None:
            p.kill()
