"""Marketing clips through OBS (obs-websocket, localhost only; it never touches the game or presses keys).
OBS keeps a 60 s replay buffer in memory (hardware H.264, 1080p30). At key moments the coach asks OBS to save it, a few
seconds after the moment so the outcome is in the clip: a death (debrief), praise (you got out / nice fight), urgent and
call lines. Each saved clip is logged as events.jsonl kind "clip" (path, reasons, clock), so the video can be cut against
what the coach saw and said. Scene "Fogwatch" = the Dota display (with the overlay)
+ desktop audio (the coach voice) + the mic. No OBS running = silently off. `uv run obs.py --setup` builds the scene."""
import ctypes, json, logging, os, sys, threading, time
from pathlib import Path

log = logging.getLogger("coach")
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)  # "OBS not running" is normal: it logged an ERROR every 10 s
CFG = Path.home() / "Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json"
SCENE = "Fogwatch"
DELAY = {"U": 10, "C": 10, "P": 3, "D": 2}  # seconds after the trigger: the outcome belongs in the clip
GAP = 25  # at most one save per 25 s; a later trigger inside the window just joins that clip's reasons
S = {"client": None, "next": 0.0, "due": None, "reasons": [], "clock": None, "lock": threading.Lock(), "buffer": False}


def _client():
    """A connected ReqClient, or None (OBS closed / websocket off). Retries at most every 10 s."""
    import obsws_python as obs
    if S["client"] or time.time() < S["next"]:
        return S["client"]
    S["next"] = time.time() + 10
    try:
        pw = json.loads(CFG.read_text()).get("server_password") if CFG.exists() else None
        S["client"] = obs.ReqClient(host="localhost", port=4455, password=pw, timeout=3)
    except Exception:
        S["client"] = None
    return S["client"]


def _call(name, *a):
    c = _client()
    if not c:
        return None
    try:
        r = getattr(c, name)(*a)
        return True if r is None else r  # requests without response data (SaveReplayBuffer) return None on success
    except Exception as e:
        log.info("obs %s: %r", name, e)
        S["client"] = None  # reconnect next time
        return None


def displays():
    """[(uuid, width, height, is_main)] of the active displays, via CoreGraphics (asking OBS for its list crashes OBS 33
    when it has no Screen Recording grant)."""
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cs = ctypes.CDLL("/System/Library/Frameworks/ColorSync.framework/ColorSync")  # where CGDisplayCreateUUIDFromDisplayID lives now
    cs.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
    cf.CFUUIDCreateString.restype, cf.CFUUIDCreateString.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    ids, n = (ctypes.c_uint32 * 8)(), ctypes.c_uint32()
    cg.CGGetActiveDisplayList(8, ids, ctypes.byref(n))
    out, main = [], cg.CGMainDisplayID()
    for d in ids[:n.value]:
        buf = ctypes.create_string_buffer(64)
        cf.CFStringGetCString(cf.CFUUIDCreateString(None, cs.CGDisplayCreateUUIDFromDisplayID(d)), buf, 64, 0x08000100)
        out.append((buf.value.decode(), cg.CGDisplayPixelsWide(d), cg.CGDisplayPixelsHigh(d), d == main))
    return out


def setup(game_display=None):
    """Idempotent: scene Fogwatch with the game display (largest screen unless given) and desktop audio, 1080p30."""
    c = _client()
    if not c:
        return "OBS not reachable (is it running with the websocket server on?)"
    ds = displays()
    uuid = game_display or max(ds, key=lambda x: x[1] * x[2])[0]
    if SCENE not in [s["sceneName"] for s in c.get_scene_list().scenes]:
        c.create_scene(SCENE)
    have = {i["inputName"] for i in c.get_input_list().inputs}
    stale = {"fw-probe", "fw-dashboard"}  # ponytail: no dashboard corner view; a 2nd display capture lagged Dota. Re-add on a faster setup
    for old in stale & have:
        c.remove_input(old)
        have.discard(old)
        time.sleep(1)  # removal is asynchronous in OBS; creating the same name right away fails
    want = [("fw-game", "screen_capture", {"type": 0, "display_uuid": uuid, "show_cursor": True}),
            ("fw-desktop-audio", "sck_audio_capture", {"type": 0})]
    for name, kind, settings in want:
        if name in have:
            c.set_input_settings(name, settings, True)
        else:
            c.create_input(SCENE, name, kind, settings, True)
    items = {i["sourceName"]: i["sceneItemId"] for i in c.get_scene_item_list(SCENE).scene_items}
    c.set_scene_item_transform(SCENE, items["fw-game"], {"boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": 1920.0,
                                                         "boundsHeight": 1080.0, "positionX": 0.0, "positionY": 0.0})
    try:  # 1080p30: 60 fps doubled the encode/compose work while Dota renders; fails harmlessly while an output is active
        c.set_video_settings(30, 1, 1920, 1080, 1920, 1080)
    except Exception:
        pass
    c.set_current_program_scene(SCENE)
    return f"scene {SCENE}: game display {uuid}, desktop audio, 1080p30"


def on_event(kind, kw):
    """rec.event hook (any thread, never blocks): schedule a replay save after a key moment."""
    tier = kw.get("tier") if kind == "voice" and kw.get("op") == "hit" else "D" if kind == "debrief" else None
    import world
    kw = {**kw, "clock": (world.W.get("game") or {}).get("clock")}
    if tier not in DELAY or kw.get("cue") in ("skill", "buy", "tp"):  # routine reminders are not clip-worthy
        return
    with S["lock"]:
        due = time.time() + DELAY[tier]
        S["due"] = max(S["due"] or 0, due) if S["due"] else due
        S["reasons"].append(kw.get("text") or kw.get("line") or kind)
        S["clock"] = S["clock"] if S["clock"] is not None else kw.get("clock")


def tick(W):
    """Called about once a second from a background thread: replay buffer on while a match runs, saves when due."""
    import rec
    g = W.get("game") or {}
    playing = g.get("state") in ("DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS")
    if playing and not S["buffer"]:
        st = _call("get_replay_buffer_status")
        if st is not None and not st.output_active:
            _call("start_replay_buffer")
        S["buffer"] = st is not None
    elif not playing and S["buffer"] and g.get("state") == "DOTA_GAMERULES_STATE_POST_GAME" and not S["due"]:
        _call("stop_replay_buffer")
        S["buffer"] = False
    with S["lock"]:
        if not S["due"] or time.time() < S["due"]:
            return
        reasons, clock = S["reasons"], S["clock"]
        S.update(due=None, reasons=[], clock=None)
    if time.time() - S.get("saved", 0) < GAP:
        return
    if _call("save_replay_buffer") is None:
        return
    S["saved"] = time.time()
    time.sleep(1.5)  # OBS writes the file asynchronously
    r = _call("get_last_replay_buffer_replay")
    path = getattr(r, "saved_replay_path", None)
    rec.event("clip", path=path, reasons=reasons, at_clock=clock)
    log.info("clip saved: %s (%s)", path, "; ".join(reasons)[:120])


def start(W_getter):
    import rec
    rec.HOOKS.append(on_event)

    def loop():
        done = False
        while True:
            if not done and _client():  # (re)build the scene once OBS is reachable; idempotent
                try:
                    log.info("obs: %s", setup())
                    done = True
                except Exception as e:
                    log.info("obs setup: %r", e)
            try:
                tick(W_getter())
            except Exception:
                log.exception("obs tick")
            time.sleep(1)
    threading.Thread(target=loop, name="obs", daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if "--setup" in sys.argv:
        print(displays())
        print(setup(next((a for a in sys.argv[1:] if not a.startswith("-")), None)))
    elif "--test" in sys.argv:
        print("buffer:", _call("start_replay_buffer") is not None or "already on")
        time.sleep(5)
        _call("save_replay_buffer"), time.sleep(2)
        print("saved:", getattr(_call("get_last_replay_buffer_replay"), "saved_replay_path", None))
