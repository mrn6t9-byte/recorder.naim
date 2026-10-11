#!/usr/bin/env python3
"""Runs inside GitHub Actions. Records an HLS link with ffmpeg -c copy (no quality loss),
uploads finished parts to a GitHub Release, and reports to Telegram.
  python gh_record.py            record
  python gh_record.py finish     upload anything left over (used by the always() step)"""
import os, re, sys, json, glob, time, signal, shutil, threading, subprocess, urllib.request, urllib.parse, urllib.error

E = os.environ.get
TOKEN, CHAT = E("TG_TOKEN", ""), E("TG_CHAT", "")
REPO, RUN = E("REPO", ""), E("RUN_ID", "local")
URL = E("URL", "")
NAME = re.sub(r"[^\w\-]+", "_", E("NAME") or "ClipByNaim")
MINUTES = float(E("MINUTES") or 0)           # 0 = until /stop
PART_MIN = float(E("PART_MIN") or 20)         # minutes per file (keep under ~2 GB per file)
BACK = E("BACK_SEGS") or "5"                  # start N segments behind live (safety)
OUT = E("OUT_DIR") or "rec"
DRY = E("DRY") == "1"                         # test mode: no uploads
TAG = "rec-" + RUN
LIMIT = 345 * 60                              # one job can run ~6h; chain a new run before that
STAMP = time.strftime("%Y%m%d_%H%M%S")
REL_URL = "https://github.com/%s/releases/tag/%s" % (REPO, TAG)

stop = threading.Event()
state = {"proc": None, "t0": time.time(), "restarts": 0, "uploaded": 0, "bytes": 0, "finished": False}


def human(b):
    return "%.2f GB" % (b / 1e9) if b >= 1e9 else "%.0f MB" % (b / 1e6)


def hms(s):
    s = int(s)
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def tg(method, **data):
    if not TOKEN or DRY:
        print("[telegram]", method, data.get("text", ""))
        return {}
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (TOKEN, method), urllib.parse.urlencode(data).encode())
        return json.load(urllib.request.urlopen(req, timeout=40))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error_code": e.code}
    except Exception as e:
        print("telegram error:", e)
        return {}


def say(text):
    if CHAT or DRY:
        tg("sendMessage", chat_id=CHAT, text=text)
    else:
        print("[msg]", text)


def parts():
    return sorted(glob.glob(os.path.join(OUT, "%s_%s_part*.ts" % (NAME, STAMP))))


def status():
    fs = parts()
    cur = human(os.path.getsize(fs[-1])) if fs else "-"
    left = "" if not MINUTES else " | left " + hms(max(0, MINUTES * 60 - (time.time() - state["t0"])))
    return "Recording %s\nElapsed: %s%s\nUploaded parts: %d (%s)\nCurrent part: %s\nRestarts: %d\n%s" % (
        NAME, hms(time.time() - state["t0"]), left, state["uploaded"], human(state["bytes"]), cur, state["restarts"], REL_URL)


def halt():
    stop.set()
    p = state["proc"]
    if p and p.poll() is None:
        p.send_signal(signal.SIGINT)            # ffmpeg finishes the current file cleanly


def poll():
    off = 0
    r = tg("getUpdates", offset=-1, timeout=0)
    if r.get("error_code") == 409:
        return                                   # a webhook (worker) is active: it handles /stop
    for u in r.get("result", []):
        off = u["update_id"] + 1
    while not stop.is_set() and not state["finished"]:
        r = tg("getUpdates", offset=off, timeout=15)
        if r.get("error_code") == 409:
            return
        for u in r.get("result", []):
            off = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(CHAT):
                continue
            t = (m.get("text") or "").strip().lower()
            if t.startswith("/stop"):
                say("Stopping and saving..."); halt()
            elif t.startswith("/status"):
                say(status())
        if not r:
            time.sleep(3)


def ensure_release():
    if DRY or not shutil.which("gh") or not REPO:
        return
    subprocess.run(["gh", "release", "create", TAG, "--repo", REPO, "--title", "Recording " + STAMP,
                    "--notes", "%s\n%s" % (NAME, URL.split("?")[0])], capture_output=True)


def process(ts):
    """remux finished .ts part to .mp4 (proper duration), upload it, delete local files."""
    mp4 = ts[:-3] + ".mp4"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", ts, "-c", "copy", "-movflags", "+faststart", mp4],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(mp4):
        mp4 = ts                                  # keep the .ts if remux failed
    size = os.path.getsize(mp4)
    ok = True
    if DRY or not shutil.which("gh") or not REPO:
        print("[dry] would upload", mp4, human(size))
    else:
        ok = subprocess.run(["gh", "release", "upload", TAG, mp4, "--repo", REPO, "--clobber"]).returncode == 0
    if ok:
        state["uploaded"] += 1; state["bytes"] += size
        say("Part ready: %s (%s)\n%s" % (os.path.basename(mp4), human(size), REL_URL))
        if size <= 49 * 1024 * 1024 and TOKEN and CHAT and not DRY:
            subprocess.run(["curl", "-s", "-F", "chat_id=" + CHAT, "-F", "document=@" + mp4,
                            "https://api.telegram.org/bot%s/sendDocument" % TOKEN], capture_output=True)
        if not DRY:
            for f in {ts, mp4}:
                try: os.remove(f)
                except OSError: pass
    else:
        say("UPLOAD FAILED for %s. Part kept on the runner only until the job ends." % os.path.basename(mp4))


def uploader():
    ensure_release()
    done = set()
    while True:
        fs = parts()
        ready = fs if state["finished"] else fs[:-1]
        for f in ready:
            if f not in done:
                done.add(f); process(f)
        if state["finished"] and not [f for f in parts() if f not in done]:
            return
        time.sleep(5)


def precheck():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-t", "6", "-i", URL, "-f", "null", "-"],
                           capture_output=True, text=True, timeout=90)
        return r.returncode == 0, (r.stderr or "").strip()[-300:]
    except Exception as e:
        return False, str(e)


def where():
    try:
        d = json.load(urllib.request.urlopen("https://ipinfo.io/json", timeout=8))
        return "%s, %s (%s)" % (d.get("city", "?"), d.get("country", "?"), d.get("org", "?"))
    except Exception:
        return "unknown"


def chain(rem_minutes):
    time.sleep(LIMIT - 300)
    if stop.is_set() or not shutil.which("gh"):
        return
    say("Job time limit is near, starting the next run to continue...")
    subprocess.run(["gh", "workflow", "run", "record.yml", "--repo", REPO, "-f", "url=" + URL, "-f", "minutes=%d" % rem_minutes,
                    "-f", "name=" + NAME, "-f", "part_min=%s" % PART_MIN])


def record():
    os.makedirs(OUT, exist_ok=True)
    ok, err = precheck()
    if not ok:
        say("The link could not be opened from GitHub's server.\nLikely geo/IP blocked or the token expired.\n" + err)
        sys.exit(1)
    say("Recording started on GitHub (%s)\nRunner location: %s\nFiles will appear here: %s\nSend /status or /stop." % (NAME, where(), REL_URL))
    signal.signal(signal.SIGINT, lambda *a: halt())
    signal.signal(signal.SIGTERM, lambda *a: halt())
    total = MINUTES * 60
    if not total or total > LIMIT:
        rem = int((total - LIMIT) / 60) if total else 0
        threading.Thread(target=chain, args=(rem,), daemon=True).start()
    if TOKEN and CHAT and not DRY:
        threading.Thread(target=poll, daemon=True).start()
    up = threading.Thread(target=uploader)
    up.start()
    last_err = ""
    while not stop.is_set():
        used = time.time() - state["t0"]
        end = min(total, LIMIT) if total else LIMIT
        if used >= end - 3:
            break
        idx = len(parts()) + len(glob.glob(os.path.join(OUT, "%s_%s_part*.mp4" % (NAME, STAMP)))) + state["uploaded"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-rw_timeout", "30000000", "-live_start_index", "-" + BACK,
               "-i", URL, "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-t", str(int(end - used)),
               "-f", "segment", "-segment_time", str(int(PART_MIN * 60)), "-segment_format", "mpegts",
               "-segment_start_number", str(idx + 1), "-reset_timestamps", "1",
               os.path.join(OUT, "%s_%s_part%%03d.ts" % (NAME, STAMP))]
        state["proc"] = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
        last_err = state["proc"].communicate()[1] or ""
        if stop.is_set() or time.time() - state["t0"] >= end - 3:
            break
        state["restarts"] += 1
        if state["restarts"] == 1 or state["restarts"] % 5 == 0:
            say("Stream dropped or ended, retrying (%d)...\n%s" % (state["restarts"], last_err.strip()[-200:]))
        time.sleep(5)
    state["finished"] = True
    up.join()
    say("Recording finished. %d file(s), %s.\nDownload: %s" % (state["uploaded"], human(state["bytes"]), REL_URL))


def finish():
    """always() step: upload leftovers if the main step was cancelled or killed."""
    leftovers = sorted(glob.glob(os.path.join(OUT, "*.ts")))
    if not leftovers:
        return
    ensure_release()
    for f in leftovers:
        process(f)
    say("Recording was interrupted. Saved %d leftover part(s).\n%s" % (len(leftovers), REL_URL))


if __name__ == "__main__":
    if not URL:
        sys.exit("URL env var is empty")
    finish() if len(sys.argv) > 1 and sys.argv[1] == "finish" else record()
