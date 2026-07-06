"""Live agent-browser streaming: watch the QA agent drive the app.

For every browser-QA turn forge starts a *shared* headless Chromium (CDP on
127.0.0.1:9222) plus a screencaster inside the worker container. The QA prompt
tells the agent to `connectOverCDP` to that browser instead of launching its
own, so the screencaster — a second CDP client — can screenshot whatever page
the agent is driving (~1 fps JPEG) into `/work/.forge/live/`. The bind-mounted
workspace makes the frames host-visible, where `GET
/api/sessions/{id}/browser[/frame]` serves them to the workspace UI.

Everything here is strictly best-effort: a missing browser, a failed exec or an
agent that launches its own Chromium degrades to "no live view" — never a
failed QA turn. Frames bypass the EventBus on purpose (base64 frames would
bloat the replay buffer and the Slack tap); the UI polls instead.

Shutdown is a stop *file*, not a signal: the worker image has no pkill/ps, and
the host owns the bind mount, so touching `.forge/live/stop` is both the
simplest and the only reliable kill switch. The screencaster also exits when
its browser dies or after a 2 h safety cap.
"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CDP_PORT = 9222
SCRIPT_NAME = "screencast.cjs"
# A frame older than this is a dead/finished stream, not a live one.
FRESH_SECS = 6.0

# Validated against the forge-worker image (see docs/specs/2026-07-06-agent-
# browser-live-view-design.md): global playwright needs NODE_PATH, the method
# is connectOverCDP (capital), and a second CDP client sees pages the agent
# opens even in fresh browser contexts.
SCREENCAST_JS = r"""
// forge screencaster: keep a shared CDP Chromium alive and dump the newest
// active page as JPEG frames for the workspace live view. Managed by forge —
// do not edit; a `stop` file in this directory ends it.
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const DIR = __dirname;
const STOP = path.join(DIR, 'stop');
const PORT = %(port)d;
const CDP = `http://127.0.0.1:${PORT}`;
const TICK_MS = 700;
const MAX_MS = 2 * 60 * 60 * 1000; // safety cap

async function cdpAlive() {
  try { return (await fetch(CDP + '/json/version')).ok; } catch { return false; }
}

async function ensureBrowser() {
  if (await cdpAlive()) return null; // reuse a previous turn's browser
  const chrome = spawn(chromium.executablePath(), [
    '--headless=new',
    `--remote-debugging-port=${PORT}`,
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--user-data-dir=/tmp/forge-live-profile',
    '--window-size=1280,800',
    'about:blank',
  ], { stdio: 'ignore' });
  for (let i = 0; i < 40; i++) {
    if (await cdpAlive()) return chrome;
    await new Promise((r) => setTimeout(r, 250));
  }
  chrome.kill();
  throw new Error('chromium never opened its CDP port');
}

// The agent may open pages in the default context or a context of its own;
// both are visible here. Prefer the newest page that has real content.
function pickPage(browser) {
  const pages = browser.contexts().flatMap((c) => c.pages());
  if (!pages.length) return null;
  const busy = pages.filter((p) => p.url() && p.url() !== 'about:blank');
  const pool = busy.length ? busy : pages;
  return pool[pool.length - 1];
}

function writeAtomic(name, data) {
  fs.writeFileSync(path.join(DIR, name + '.tmp'), data);
  fs.renameSync(path.join(DIR, name + '.tmp'), path.join(DIR, name));
}

(async () => {
  const chrome = await ensureBrowser();
  const browser = await chromium.connectOverCDP(CDP);
  console.log('screencast: attached to', CDP);
  const deadline = Date.now() + MAX_MS;
  let seq = 0;
  while (!fs.existsSync(STOP) && browser.isConnected() && Date.now() < deadline) {
    try {
      const page = pickPage(browser);
      if (page) {
        const buf = await page.screenshot({ type: 'jpeg', quality: 55, timeout: 3000 });
        writeAtomic('frame.jpg', buf);
        seq++;
        writeAtomic('meta.json', JSON.stringify({
          url: page.url(),
          title: await page.title().catch(() => ''),
          ts: Date.now(),
          seq,
        }));
      }
    } catch (e) { /* page mid-navigation / just closed — skip this tick */ }
    await new Promise((r) => setTimeout(r, TICK_MS));
  }
  console.log('screencast: exiting after', seq, 'frames');
  if (chrome) chrome.kill();
  process.exit(0);
})().catch((e) => { console.error('screencast:', e); process.exit(1); });
""" % {"port": CDP_PORT}

# Global npm packages (playwright) are not on node's default require path;
# NODE_PATH must be resolved inside the container. Output goes to a log file
# beside the frames so a silent stream is debuggable after the fact.
_LAUNCH = ("export NODE_PATH=$(npm root -g); "
           f"exec node /work/.forge/live/{SCRIPT_NAME} "
           ">> /work/.forge/live/screencast.log 2>&1")


def live_dir(runs_dir, run_id) -> Path:
    return Path(runs_dir) / run_id / "workspace" / ".forge" / "live"


def frame_path(runs_dir, run_id) -> Path:
    return live_dir(runs_dir, run_id) / "frame.jpg"


def start(runs_dir, run_id, env, service="forge") -> bool:
    """Write the screencaster into the run's workspace and start it detached in
    the worker container. Idempotent per turn: stale stop/frame/meta files are
    cleared first, and the script reuses an already-running browser (a second
    launch on a busy CDP port just attaches). Never raises."""
    try:
        d = live_dir(runs_dir, run_id)
        d.mkdir(parents=True, exist_ok=True)
        for stale in ("stop", "frame.jpg", "meta.json"):
            (d / stale).unlink(missing_ok=True)
        (d / SCRIPT_NAME).write_text(SCREENCAST_JS)
        env.exec_detached(["bash", "-lc", _LAUNCH], service=service)
        return True
    except Exception:
        logger.exception("browserview start failed (run %s)", run_id)
        return False


def stop(runs_dir, run_id) -> None:
    """End the screencaster (and its Chromium) by touching the stop file the
    script polls every tick. Host-side only — no container exec. Never raises."""
    try:
        d = live_dir(runs_dir, run_id)
        if d.is_dir():
            (d / "stop").touch()
    except Exception:
        logger.exception("browserview stop failed (run %s)", run_id)


def status(runs_dir, run_id, fresh_secs=FRESH_SECS, now=None) -> dict:
    """What the workspace UI polls: {active, ts, url, title}. `active` means a
    frame exists and is fresh; `ts` (frame mtime, ms) doubles as the <img>
    cache-buster so the UI only reloads when a new frame landed."""
    fp = frame_path(runs_dir, run_id)
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return {"active": False, "ts": 0, "url": "", "title": ""}
    meta = {}
    try:
        meta = json.loads((fp.parent / "meta.json").read_text())
    except (OSError, ValueError):
        pass
    fresh = ((now or time.time()) - mtime) < fresh_secs
    return {"active": fresh, "ts": int(mtime * 1000),
            "url": str(meta.get("url") or ""),
            "title": str(meta.get("title") or "")}
