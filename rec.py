"""Crash-safe session recorder: <root>/<YYYYmmdd-HHMMSS>[_<matchid>]/{meta.json, gsi|cv|world|events.jsonl, frames/}.
Callers only enqueue (never block; overflow = drop + count). One writer thread does all disk I/O: every JSONL line is
one unbuffered write(2) (survives kill -9 whole), os.fsync at most every 5 s (power loss), meta.json atomic every 30 s."""
import atexit, json, logging, os, queue, shutil, threading, time
from pathlib import Path

log = logging.getLogger("coach")
FSYNC_S, MIN_FREE, MAX_FRAMES, MAX_QUEUED_FRAMES = 5, 5e9, 4e9, 64
Q = queue.Queue(20000)
R = {"dir": None, "t0": None, "mid": None, "files": {}, "lines": {}, "bytes": 0, "frames": 0, "frame_bytes": 0,
     "dropped": 0, "marks": 0, "paused": None, "meta": {}, "start": None, "thread": None}
_lock = threading.Lock()  # guards R["dir"] swaps vs status() readers


def on():
    return R["dir"] is not None


def _new(root, mid=None):
    R.update(t0=time.time(), start=time.time(), mid=mid, lines={}, bytes=0, frames=0, frame_bytes=0, dropped=0, marks=0, paused=None)
    d = Path(root) / (time.strftime("%Y%m%d-%H%M%S") + (f"_{mid}" if mid else ""))
    while d.exists():  # two sessions in one second (match rotation right after start)
        d = d.with_name(d.name + "x")
    (d / "frames").mkdir(parents=True)
    R["dir"] = d


def start(root, meta=None):
    """Open a new session under `root` and start the writer thread. Returns the session dir."""
    _new(root)
    R["meta"] = json.loads(json.dumps(meta or {}, default=str))
    _write_meta()
    R["thread"] = threading.Thread(target=_run, name="rec-writer", daemon=True)
    R["thread"].start()
    atexit.register(stop)
    return R["dir"]


def _put(job):
    try:
        Q.put_nowait(job)
    except queue.Full:
        R["dropped"] += 1


def write(name, obj):
    """Append one JSON line to <name>.jsonl (serialized now, so later mutation of obj can't race the writer)."""
    if on():
        _put(("line", name, obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), default=str)))


def gsi(body):
    if on():
        write("gsi", {"t": round(time.time() - R["t0"], 3), "body": body})  # keeps the v1 format: tools.play/--replay read it


HOOKS = []  # fn(kind, kw) called on every event, recording on or off (obs.on_event); must not block


def event(kind, **kw):
    for h in HOOKS:
        try:
            h(kind, kw)
        except Exception:
            log.exception("rec hook")
    if on():
        import world  # lazy: rec.py stays importable on its own (selfcheck crash test)
        c = (world.W.get("game") or {}).get("clock")
        write("events", {"w": round(time.time(), 3), "clock": c, "kind": kind, **kw})


def mark(**kw):
    R["marks"] += 1
    event("feedback", n=R["marks"], **kw)
    return R["marks"]


def frame(kind, img, ext="png"):
    """Queue an image (BGR ndarray) as frames/<kind>_<epoch ms>.<ext>; dropped while paused or backed up."""
    if not on() or R["paused"]:
        return
    if Q.qsize() > MAX_QUEUED_FRAMES:
        R["dropped"] += 1
        return
    _put(("frame", f"{kind}_{int(time.time() * 1000)}.{ext}", img, ext))


def match(mid):
    """matchid seen: first one renames the session (pre-match data stays in it), a different one starts a new session."""
    if on() and mid and mid != "0" and mid != R["mid"]:
        _put(("match", mid))


def meta(d):
    if on():
        R["meta"] = json.loads(json.dumps(d, default=str))  # snapshot now; the writer serializes later
        _put(("meta",))


def status():
    with _lock:
        d = R["dir"]
    return {"on": d is not None, "dir": d.name if d else None, "mb": round((R["bytes"] + R["frame_bytes"]) / 1e6, 1),
            "frames": R["frames"], "dropped": R["dropped"], "marks": R["marks"], "frames_paused": R["paused"] or False}


def _file(name):
    f = R["files"].get(name)
    if f is None:
        f = R["files"][name] = open(R["dir"] / f"{name}.jsonl", "ab", buffering=0)
    return f


def _sync(close=False):
    for f in R["files"].values():
        try:
            os.fsync(f.fileno())
            if close:
                f.close()
        except (OSError, ValueError):
            pass
    if close:
        R["files"] = {}


def _write_meta(end=False):
    d = R["dir"]
    try:
        free = shutil.disk_usage(d).free
    except OSError:
        free = None
    m = {**R["meta"], "dir": d.name, "start": R["start"], "start_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(R["start"])),
         "updated": time.time(), "end": time.time() if end else None, "matchid": R["mid"] or R["meta"].get("matchid"),
         "counts": {"lines": dict(R["lines"]), "frames": R["frames"], "dropped": R["dropped"], "marks": R["marks"]},
         "disk": {"session_mb": round((R["bytes"] + R["frame_bytes"]) / 1e6, 1), "frames_mb": round(R["frame_bytes"] / 1e6, 1),
                  "free_gb": round(free / 1e9, 1) if free is not None else None, "frames_paused": R["paused"]}}
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(m, indent=1, default=str))
    os.replace(tmp, d / "meta.json")
    if free is not None and free < MIN_FREE and not R["paused"]:
        _pause(f"free disk {free / 1e9:.1f} GB < {MIN_FREE / 1e9:.0f} GB")


def _pause(why):
    R["paused"] = why
    log.warning("recording: frames paused (%s); JSONL continues", why)  # also lands in events.jsonl via app's log handler
    line = json.dumps({"w": round(time.time(), 3), "kind": "frames_paused", "why": why})
    _file("events").write((line + "\n").encode())


def _do(job):
    kind = job[0]
    if kind == "line":
        _, name, s = job
        b = (s + "\n").encode()
        _file(name).write(b)
        R["lines"][name] = R["lines"].get(name, 0) + 1
        R["bytes"] += len(b)
    elif kind == "frame":
        import cv2
        _, fname, img, ext = job
        ok, buf = cv2.imencode("." + ext, img, [cv2.IMWRITE_JPEG_QUALITY, 80] if ext == "jpg" else [cv2.IMWRITE_PNG_COMPRESSION, 3])
        if ok:
            (R["dir"] / "frames" / fname).write_bytes(buf.tobytes())
            R["frames"] += 1
            R["frame_bytes"] += len(buf)
            if R["frame_bytes"] > MAX_FRAMES and not R["paused"]:
                _pause(f"session frames > {MAX_FRAMES / 1e9:.0f} GB")
    elif kind == "match":
        mid, root = job[1], R["dir"].parent
        if R["mid"] is None:  # same session, now named after the match
            new = R["dir"].with_name(R["dir"].name + f"_{mid}")
            os.rename(R["dir"], new)  # open handles stay valid across a rename
            with _lock:
                R["dir"], R["mid"] = new, mid
        else:  # a different match: close this session, open a new one
            _sync(close=True)
            _write_meta(end=True)
            keep = R["meta"]
            with _lock:
                _new(root, mid)
            R["meta"] = keep
        _write_meta()
    elif kind == "meta":
        _write_meta()


def _run():
    last = time.time()
    while True:
        try:
            job = Q.get(timeout=1)
        except queue.Empty:
            job = ()
        if job is None:
            break
        try:
            if job:
                _do(job)
        except Exception as e:  # ponytail: a failed write is logged and skipped, never kills the writer
            log.warning("recording: %s failed: %r", job[0], e)
        if time.time() - last >= FSYNC_S:
            last = time.time()
            _sync()


def stop():
    """Drain the queue, fsync, close, final meta.json. Idempotent (lifespan shutdown + atexit)."""
    t = R["thread"]
    if not t:
        return
    R["thread"] = None
    Q.put(None)
    t.join(10)
    _sync(close=True)
    try:
        _write_meta(end=True)
    except OSError as e:
        log.warning("recording: final meta.json failed: %r", e)
