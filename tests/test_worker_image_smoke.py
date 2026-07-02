import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker not available")


def _has_image():
    r = subprocess.run(["docker", "image", "inspect", "forge-worker"],
                       capture_output=True)
    return r.returncode == 0


@pytest.mark.skipif(not _has_image(), reason="forge-worker image not built")
def test_image_has_bun_corepack_and_browser_libs():
    # bun + corepack-provided pnpm/yarn on PATH
    for tool in ("bun", "pnpm", "yarn"):
        r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                            "forge-worker", "-lc", f"command -v {tool}"],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{tool} missing from image: {r.stderr}"
    # key Chromium runtime libs are installed (dpkg is reliable; ldconfig isn't
    # on PATH in the slim image)
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                        "forge-worker", "-lc", "dpkg -s libnss3 libglib2.0-0"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"browser libs missing: {r.stderr or r.stdout}"
