"""Opt-in live smoke: prove the real `supabase` CLI honors a config.toml that
`rewrite_config` shifted to an offset block — i.e. a per-run stack actually comes
up on the offset ports under a unique project_id.

Heavy (spins a full Supabase stack) and touches your Docker, so it is gated on
BOTH the `supabase` CLI being present AND `FORGE_SUPABASE_SMOKE=1`. It uses an
isolated offset project, so it does not disturb a dev Supabase on the base ports.

    FORGE_SUPABASE_SMOKE=1 python3 -m pytest tests/test_supabase_smoke.py
"""
import os
import shutil
import subprocess

import pytest

from forge import supaports

pytestmark = pytest.mark.skipif(
    shutil.which("supabase") is None or not os.environ.get("FORGE_SUPABASE_SMOKE"),
    reason="supabase CLI absent or FORGE_SUPABASE_SMOKE unset",
)


def test_rewritten_config_starts_on_offset_block(tmp_path):
    wd = tmp_path / "proj"
    wd.mkdir()
    subprocess.run(["supabase", "init", "--workdir", str(wd), "--force"],
                   check=True, capture_output=True, text=True)
    cfg = wd / "supabase" / "config.toml"
    offset = 100
    cfg.write_text(supaports.rewrite_config(cfg.read_text(), "forge-smoke", offset))
    try:
        subprocess.run(["supabase", "start", "--workdir", str(wd)],
                       check=True, capture_output=True, text=True, timeout=600)
        status = subprocess.run(["supabase", "status", "--workdir", str(wd), "-o", "env"],
                                capture_output=True, text=True)
        # API came up on the offset block (54321 + 100), not the base port.
        assert "54421" in status.stdout
        assert "54321" not in status.stdout
    finally:
        subprocess.run(["supabase", "stop", "--workdir", str(wd)], capture_output=True)
