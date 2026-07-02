import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class ContainerRunner(Protocol):
    def start(self, run_id: str, env: dict[str, str],
              publish_port: int | None = None) -> str: ...
    def exec(self, cid: str, argv: list[str], workdir: str = "/work") -> ExecResult: ...
    def exec_detached(self, cid: str, argv: list[str], workdir: str = "/work") -> None: ...
    def port(self, cid: str, container_port: int) -> int | None: ...
    def stop(self, cid: str) -> None: ...


class DockerRunner:
    def __init__(self, image_tag: str):
        self.image_tag = image_tag

    def start(self, run_id: str, env: dict[str, str],
              publish_port: int | None = None) -> str:
        cmd = ["docker", "run", "-d", "--name", f"forge-{run_id}", "-w", "/work"]
        if publish_port is not None:
            # auto-assign a localhost host port → the container's web port
            cmd += ["-p", f"127.0.0.1::{publish_port}"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]   # secret values live here; never log `cmd`
        cmd.append(self.image_tag)
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"docker run failed (exit {out.returncode}): {out.stderr.strip()}")
        return out.stdout.strip()

    def exec(self, cid: str, argv: list[str], workdir: str = "/work") -> ExecResult:
        cmd = ["docker", "exec", "-w", workdir, cid] + argv
        out = subprocess.run(cmd, capture_output=True, text=True)
        return ExecResult(out.returncode, out.stdout, out.stderr)

    def exec_detached(self, cid: str, argv: list[str], workdir: str = "/work") -> None:
        # start a long-running process (e.g. the dev server) in the background
        subprocess.run(["docker", "exec", "-d", "-w", workdir, cid] + argv,
                       capture_output=True)

    def port(self, cid: str, container_port: int) -> int | None:
        from forge.commands import parse_host_port
        out = subprocess.run(["docker", "port", cid, str(container_port)],
                             capture_output=True, text=True)
        return parse_host_port(out.stdout) if out.returncode == 0 else None

    def stop(self, cid: str) -> None:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
