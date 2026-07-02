from datetime import datetime
from pathlib import Path


class RunDir:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, runs_dir: Path, run_id: str) -> "RunDir":
        return cls(Path(runs_dir) / run_id)

    def path(self, name: str) -> Path:
        return self.root / name

    def write(self, name: str, content: str) -> Path:
        p = self.path(name)
        p.write_text(content)
        return p

    def timeline(self, line: str, *, ts: str | None = None) -> None:
        stamp = ts if ts is not None else datetime.now().strftime("%H:%M")
        with self.path("timeline.md").open("a") as f:
            f.write(f"{stamp}  {line}\n")
