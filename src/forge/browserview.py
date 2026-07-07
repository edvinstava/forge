"""Live agent-browser streaming: watch the agent drive the app.

For every browser-visible turn — the executor reproducing a bug and checking
its fix, browser QA, QA-fix — forge starts a *shared* headless Chromium (CDP
on 127.0.0.1:9222) plus a screencaster inside the worker container. The turn's
prompt tells the agent to `connectOverCDP` to that browser instead of launching
its own, so the screencaster — a second CDP client — can capture whatever page
the agent is driving into `/work/.forge/live/`. The bind-mounted workspace
makes the frames host-visible, where `GET /api/sessions/{id}/browser[...]`
serves them to the workspace UI.

Frames are *pushed*, not polled: `Page.startScreencast` makes Chromium emit a
JPEG per paint (throttled to ~12 fps on disk), so typing and scrolling stream
smoothly while a static page costs nothing. Because a static page emits no
frames, liveness is a separate heartbeat (`meta.json`'s `beat`, written every
control tick) — `active` must not flap off while the agent thinks. Where
screencast setup fails the script degrades to the old explicit-screenshot loop.

Delivery to the UI is an MJPEG stream (`/browser/frame` + `/browser/stream`):
`stream_frames` watches `frame.jpg` and pushes each new frame down a
`multipart/x-mixed-replace` response an `<img>` renders natively — latency is
one file-poll tick, not the workspace's status-poll interval. Frames still
bypass the EventBus on purpose (base64 frames would bloat the replay buffer
and the Slack tap).

Everything here is strictly best-effort: a missing browser, a failed exec or an
agent that launches its own Chromium degrades to "no live view" — never a
failed QA turn.

Shutdown is a stop *file*, not a signal: the worker image has no pkill/ps, and
the host owns the bind mount, so touching `.forge/live/stop` is both the
simplest and the only reliable kill switch. The screencaster also exits when
its browser dies or after a 2 h safety cap.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CDP_PORT = 9222
SCRIPT_NAME = "screencast.cjs"
# No frame AND no heartbeat for this long = a dead/finished stream.
FRESH_SECS = 6.0
# MJPEG boundary for /browser/stream (must match the route's media type).
STREAM_BOUNDARY = b"forgeframe"

# Validated against the forge-worker image (see docs/specs/2026-07-06-agent-
# browser-live-view-design.md): global playwright needs NODE_PATH, the method
# is connectOverCDP (capital), a second CDP client sees pages the agent opens
# even in fresh browser contexts, and Page.startScreencast over
# context.newCDPSession delivers ~30 fps while the page paints (spike:
# 91 frames/3 s active, 6/3 s static — hence the heartbeat).
SCREENCAST_JS = r"""
// forge screencaster: keep a shared CDP Chromium alive and stream the newest
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
const TICK_MS = 250;             // control loop: stop file, page pick, heartbeat
const MIN_FRAME_MS = 80;         // write throttle: ~12 fps max onto the bind mount
const SHOT_MS = 600;             // fallback-mode explicit screenshot cadence
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
// both are visible here. Pick the newest page with real content — and none
// while everything is still about:blank: executor turns start this stream
// long before the agent first navigates, and a white placeholder frame would
// flip the workspace pane away from the running app for nothing.
function pickPage(browser) {
  const pages = browser.contexts().flatMap((c) => c.pages());
  const busy = pages.filter((p) => p.url() && p.url() !== 'about:blank');
  return busy.length ? busy[busy.length - 1] : null;
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

  let seq = 0, frameTs = 0, lastWrite = 0, lastShot = 0, titleTs = 0;
  let page = null, cast = null, title = '';

  const writeFrame = (buf) => {
    const now = Date.now();
    if (now - lastWrite < MIN_FRAME_MS) return; // acked but not written
    lastWrite = now;
    writeAtomic('frame.jpg', buf);
    seq++; frameTs = now;
  };

  // Push mode: Chromium emits a frame per paint — smooth while the agent
  // types/scrolls, silent while the page is static (the heartbeat below keeps
  // `active` true). Every frame is acked immediately so the stream never
  // stalls; the disk throttle happens after the ack. Returns null (fallback
  // poll mode) where screencast setup fails.
  const startCast = async (p) => {
    try {
      const s = await p.context().newCDPSession(p);
      s.on('Page.screencastFrame', (ev) => {
        s.send('Page.screencastFrameAck', { sessionId: ev.sessionId }).catch(() => {});
        try { writeFrame(Buffer.from(ev.data, 'base64')); } catch {}
      });
      await s.send('Page.startScreencast', {
        format: 'jpeg', quality: 60, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1,
      });
      return s;
    } catch (e) {
      console.log('screencast: push mode unavailable, polling instead:', e.message);
      return null;
    }
  };

  while (!fs.existsSync(STOP) && browser.isConnected() && Date.now() < deadline) {
    try {
      const next = pickPage(browser);
      if (next !== page) {                    // agent moved to another page
        if (cast) { cast.detach().catch(() => {}); cast = null; }
        page = next; title = ''; titleTs = 0;
        if (page) cast = await startCast(page);
      }
      if (page) {
        const now = Date.now();
        if (!cast && now - lastShot >= SHOT_MS) { // fallback: explicit screenshots
          lastShot = now;
          writeFrame(await page.screenshot({ type: 'jpeg', quality: 55, timeout: 3000 }));
        }
        if (now - titleTs > 1000) {           // title is a CDP roundtrip — go easy
          titleTs = now;
          title = await page.title().catch(() => title);
        }
        writeAtomic('meta.json', JSON.stringify({
          url: page.url(), title, ts: frameTs, seq, beat: Date.now(),
        }));
      }
    } catch (e) { /* page mid-navigation / just closed — skip this tick */ }
    await new Promise((r) => setTimeout(r, TICK_MS));
  }
  console.log('screencast: exiting after', seq, 'frames');
  if (cast) cast.detach().catch(() => {});
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
    """What the workspace UI polls: {active, ts, url, title}. `active` needs a
    frame to exist plus either a fresh frame or a fresh screencaster heartbeat
    (push mode writes frames only when the page paints — a static page must not
    read as dead). `ts` (frame mtime, ms) is the <img> cache-buster in poll
    fallback mode, so it only bumps when a new frame actually landed."""
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
    t = now or time.time()
    try:
        beat = float(meta.get("beat") or 0) / 1000.0
    except (TypeError, ValueError):
        beat = 0.0
    fresh = (t - mtime) < fresh_secs or (t - beat) < fresh_secs
    return {"active": fresh, "ts": int(mtime * 1000),
            "url": str(meta.get("url") or ""),
            "title": str(meta.get("title") or "")}


async def stream_frames(runs_dir, run_id, *, poll_secs=0.08,
                        fresh_secs=FRESH_SECS, grace_secs=3.0,
                        max_secs=2 * 60 * 60):
    """MJPEG parts for GET /browser/stream: push every new frame.jpg the moment
    it lands (one poll tick of latency) until the screencast dies — the browser
    <img> renders the multipart stream natively. Ends cleanly when the stream
    goes inactive (turn finished), after `grace_secs` if no frame ever shows
    up, or at the same safety cap the screencaster has; a client disconnect
    just cancels the generator."""
    fp = frame_path(runs_dir, run_id)
    started = time.time()
    last = 0.0
    sent = False
    while time.time() - started < max_secs:
        if not status(runs_dir, run_id, fresh_secs=fresh_secs)["active"]:
            if sent or time.time() - started >= grace_secs:
                return
        else:
            try:
                mtime = fp.stat().st_mtime
                if mtime > last:
                    data = fp.read_bytes()  # atomic rename → always a whole JPEG
                    last = mtime
                    sent = True
                    yield (b"--" + STREAM_BOUNDARY + b"\r\n"
                           b"Content-Type: image/jpeg\r\n"
                           b"Content-Length: " + str(len(data)).encode()
                           + b"\r\n\r\n" + data + b"\r\n")
            except OSError:
                pass                          # frame mid-swap — next tick has it
        await asyncio.sleep(poll_secs)
