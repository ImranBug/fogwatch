"""live-coach server: GSI + CV in, world state out over SSE at <=4 Hz. Records every session unless --no-record.
uv run app.py [--port 3939] [--frames DIR] [--replay FILE --speed N] [--no-record] [--turbo] [--demo] [--no-cv] [--no-llm] [--no-jev] [--no-mic]"""
import argparse, asyncio, hashlib, json, logging, os, re, threading, time, tomllib
from contextlib import asynccontextmanager
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
import coach, data, inspector, jev, listen, llm, menu, obs, postgame, rec, review, rules, voice, world

ROOT = Path(__file__).parent
log = logging.getLogger("coach")
args = argparse.Namespace(port=3939, frames=None, replay=None, speed=1.0, no_record=True, rec_dir=None, data_dir=None, turbo=False,
                          demo=False, no_cv=False, no_llm=False, no_jev=False, no_mic=False, cv_fps=10)
TOKEN = None
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
SRC_HASH = hashlib.sha1(b"".join(p.read_bytes() for p in sorted([*ROOT.glob("*.py"), *ROOT.glob("static/*.html")]))).hexdigest()[:12]
WORLD_SKIP = {"_", "enemy_report", "events", "settings", "lineup", "postgame", "history", "ready"}  # bulky/static: world.jsonl rows stay ~3-5 KB
V = {"vision": None, "frame": None, "json": "{}", "warned": set(), "shape": None, "want_full": None, "t_mm": 0.0, "t_top": 0.0,
     "t_full": 0.0, "t_world": 0.0, "t_meta": 0.0, "alerts": {}, "att": None, "last_lu": None}
busy = threading.Lock()


def warn_once(key, msg, *a):
    if key not in V["warned"]:
        V["warned"].add(key)
        log.warning(msg, *a)


# ---- CV (capture thread, never the event loop) -------------------------------------------------------------------
DRAFT = ("DOTA_GAMERULES_STATE_HERO_SELECTION", "DOTA_GAMERULES_STATE_STRATEGY_TIME")


def on_frame(frame, *_):
    V["frame_wall"] = time.time()
    if not busy.acquire(blocking=False):
        return  # at most one frame in flight; drop the rest
    try:
        V["frame"] = frame
        if (world.W.get("src_age") or {}).get("gsi_s") is None or world.W["src_age"]["gsi_s"] > 5:
            menu.tick(frame, world.W)  # in the Dota menu: "Finding a match: Normal, Turbo" on the dashboard
        elif world.W.get("menu"):
            world.W["menu"] = None
        if world.W["game"].get("state") in DRAFT and time.time() - V.get("draft_t", 0) >= 2:  # picks as they happen (GSI has none)
            V["draft_t"] = time.time()
            got, old = V["vision"].draft_picks(frame), world.W.get("draft_seen") or {}
            if got:
                with world.lock:
                    world.W["draft_seen"] = {k: [n or o for n, o in zip(got[k], old.get(k) or [None] * 5)] for k in ("allies", "enemies")}
        cv = V["vision"].process(frame, time.time(), world.snapshot())
        world.merge_cv(cv)
        if inspector.offer(frame) and rec.on():  # bottom-HUD inventory read on its own thread, <= ~1.4 Hz
            V["want_full"] = V["want_full"] or "inspect"  # keep a full frame of every enemy inspection
        if rec.on():
            record_frame(frame, cv)
    except Exception as e:
        warn_once("frame", "vision.process failed (%r); continuing", e)
    finally:
        busy.release()


def record_frame(frame, cv):
    """cv.jsonl row per processed frame; minimap crop 2 fps, top-bar band 0.2 fps, full JPEG every 10 s / on demand."""
    now, (h, w) = time.time(), frame.shape[:2]
    V["shape"] = [w, h]
    row = dict(cv)
    if row.get("lineup") == V["last_lu"]:
        row.pop("lineup", None)  # only when it changes
    else:
        V["last_lu"] = row.get("lineup")
    rec.write("cv", {"w": round(now, 3), "clock": world.W["game"]["clock"], **row})
    if cv.get("minimap_rect") and now - V["t_mm"] >= 0.5:
        V["t_mm"] = now
        x, y, mw, mh = map(int, cv["minimap_rect"])
        rec.frame("mm", frame[y:y + mh, x:x + mw].copy())
    if now - V["t_top"] >= 5:
        V["t_top"] = now
        rec.frame("top", frame[:int(h * 0.1)].copy())  # top bar incl. a black band above it
    if now - V["t_full"] >= 10 or V["want_full"]:
        V["t_full"], V["want_full"] = now, None
        rec.frame("full", frame, "jpg")  # capture hands over a fresh array per frame; nothing mutates it


def session_loader(d):
    """--frames <session>/frames: paste the mm_/top_ crops back onto a black frame of the geometry in meta.json."""
    import bisect, cv2, numpy as np
    g = json.loads((d.parent / "meta.json").read_text())["geometry"]
    (w, h), (x, y, _, _) = g["frame"], g["minimap_rect"]
    tops = sorted(d.glob("top_*.png"))
    tms = [int(p.stem[4:]) for p in tops]

    def load(p):
        f = np.zeros((h, w, 3), np.uint8)
        i = bisect.bisect_right(tms, int(p.stem[3:])) - 1
        if i >= 0 and (t := cv2.imread(str(tops[i]))) is not None:
            f[:t.shape[0]] = t[:, :w]
        if (m := cv2.imread(str(p))) is not None:
            f[y:y + m.shape[0], x:x + m.shape[1]] = m[:h - y, :w - x]
        return f
    return load


def cv_err(msg):
    """Shown on the dashboard CV chip (the terminal is on the other monitor)."""
    with world.lock:
        world.W["cv"]["err"] = msg


def cv_loop():
    try:
        import capture, vision
        V["vision"] = vision
    except Exception as e:
        log.warning("CV disabled, GSI only (%r)", e)
        return cv_err(f"CV disabled: {e}")
    if args.frames:
        vision.CALIB = world.DATA / "calib_frames.json"  # synthetic calibration must never overwrite the real one
        try:
            d = Path(args.frames)
            if any(d.glob("mm_*.png")):  # a recorded session's frames/ dir
                V["cap"] = capture.start_files(d, on_frame, args.cv_fps, "mm_*.png", session_loader(d))
            else:
                vision.ASPECT = 0  # synthetic frames: not stretched, whatever video.txt says
                V["cap"] = capture.start_files(d, on_frame, args.cv_fps)
        except Exception as e:
            log.warning("--frames %s unusable (%r); GSI only", args.frames, e)
            cv_err(f"--frames unusable: {e}")
        return
    vision.CALIB = world.DATA / "calib.json"
    while True:
        try:
            V["cap"] = capture.start(on_frame, fps=args.cv_fps, scale=1.0)
            log.info("capture started")
            rec.event("capture", op="start")
            V["warned"].discard("capture")
            cv_err(None)
            V["frame_wall"] = time.time()
            while not V["cap"]["dead"].wait(5):  # stream stopped (window closed / permission revoked): start over
                if time.time() - V.get("frame_wall", 0) > 20:  # stream alive but frozen (seen live): restart it
                    log.warning("capture frozen for 20 s; restarting stream")
                    try:
                        capture.stop(V["cap"])
                    except Exception:
                        pass
                    break
            log.warning("capture stopped; retrying every 5 s")
            rec.event("capture", op="stop")
            cv_err("capture stopped; retrying")
        except Exception as e:
            warn_once("capture", "capture unavailable (%r); retrying every 5 s", e)
            cv_err(str(e))
        time.sleep(5)


# ---- GSI ingest ---------------------------------------------------------------------------------------------------
def ingest(body):
    body.pop("auth", None)  # never record the token
    rec.gsi(body)
    if world.merge_gsi(body, demo=args.demo):
        rec.match(world.W["game"]["matchid"])


def meta():
    """Session facts for meta.json (call under world.lock)."""
    W, shape = world.W, V["shape"]
    g, me, cv = W["game"], W["me"], W["cv"]
    return {"app": {"version": VERSION, "src_hash": SRC_HASH}, "args": vars(args), "matchid": g["matchid"], "hero": me["hero"],
            "team": me["team"], "mode": g["mode"], "clock": g["clock"], "settings": dict(W["settings"]),
            "geometry": {"frame": shape, "minimap_rect": cv["minimap_rect"], "top_band_h": int(shape[1] * 0.1) if shape else None},
            "cv": {k: cv.get(k) for k in ("icon_px", "affine_ok", "gsi_vs_cv_err_u", "fps", "ms", "err")},
            "lineup": W["lineup"], "jev": dict(W["jev"]), "llm": dict(W["llm"])}


def rec_tick(W, now):
    """events.jsonl: alert new/escalate/clear + attention changes; world.jsonl every 1 s; meta.json every 30 s."""
    cur = {a["id"]: a for a in W["alerts"]}
    for k, a in cur.items():
        old = V["alerts"].get(k)
        if old is None or rules.RANK[a["level"]] > rules.RANK[old]:
            rec.event("alert", op="new" if old is None else "escalate", id=k, rule=a["rule"], level=a["level"], text=a["text"])
            if a["level"] == "high":
                V["want_full"] = k
    for k in V["alerts"].keys() - cur.keys():
        rec.event("alert", op="clear", id=k)
    V["alerts"] = {k: a["level"] for k, a in cur.items()}
    at = W["attention"]
    if (key := (at["seq"], at["id"], at["level"], at["sticky"])) != V["att"]:
        V["att"] = key
        rec.event("attention", **at)
    if now - V["t_world"] >= 1:
        V["t_world"] = now
        rec.write("world", json.dumps({"w": round(now, 3), **{k: v for k, v in W.items() if k not in WORLD_SKIP}},
                                      separators=(",", ":"), default=str))
    if now - V["t_meta"] >= 30:
        V["t_meta"] = now
        rec.meta(meta())
    W["rec"] = rec.status()


async def tick_loop():
    while True:
        now = time.time()
        try:
            with world.lock:
                world.tick(now)
                rules.evaluate(world.W, now)
                llm.PAUSED = world.W["settings"].get("ai") is not True  # dashboard AI switch; the radar, rules and alerts never need it
                jev.maybe_decide(world.W, now)
                llm.maybe_call(world.W, now)
                coach.tick(world.W)  # goal, brief, debrief, praise: queues its lines before the voice picks one
                voice.tick(world.W)
                listen.tick(world.W)
                postgame_tick(world.W)
                if rec.on():
                    rec_tick(world.W, now)
                V["json"] = world.dumps()
        except Exception:
            log.exception("tick failed; continuing")
        await asyncio.sleep(0.25)


def postgame_tick(W):
    """W["postgame"]: None until the match ends (POST_GAME / win_team, or GSI silent > 60 s mid-game past 10:00), then a background
    postgame.analyze() summary. Once per match; a silence-triggered report is redone at the real end. Cleared on a new match."""
    g = W["game"]
    if "postgame" not in W or g["state"] == "DOTA_GAMERULES_STATE_HERO_SELECTION":
        W["postgame"] = None  # world.reset() (new matchid) drops the key
    mid, done = g["matchid"], V.setdefault("pg", {})
    end = g["state"] == "DOTA_GAMERULES_STATE_POST_GAME" or g["win_team"] not in (None, "none", "")
    silent = g["state"] == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS" and (g["clock"] or 0) >= 600 and (W["src_age"]["gsi_s"] or 0) > 60
    if not mid or mid == "0" or not (end or silent) or done.get(mid) == "end" or (silent and not end and mid in done):
        return
    done[mid] = "end" if end else "silent"
    over = W["llm"]["usd_est"] >= llm.MAX_USD

    def run():
        dirs = postgame.matches(args.rec_dir or ROOT / "recordings").get(mid)
        try:
            rep = postgame.analyze(dirs, llm_call=not over) if dirs else {"matchid": mid, "error": "no recordings for this match"}
        except Exception as e:
            log.exception("postgame failed")
            rep = {"matchid": mid, "error": repr(e)[:200]}
        with world.lock:
            if world.W["game"]["matchid"] == mid:
                world.W["postgame"] = postgame.summary(rep)
        rec.event("postgame", matchid=mid, result=rep.get("result"), path=rep.get("path"), error=rep.get("error"))
        log.info("postgame %s: %s", mid, rep.get("path") or rep.get("error"))
        if rep.get("path") and dirs and not llm.PAUSED:   # the AI switch covers the paid review too
            try:  # the deep learning review (one large Claude call, ~1-3 min); compaction below waits for it
                rv = review.review(dirs)
                log.info("review %s: %s", mid, rv)
                with world.lock:  # the end-of-game cost includes the review
                    pg = world.W.get("postgame")
                    if world.W["game"]["matchid"] == mid and pg and pg.get("scorecard") and rv.get("usd"):
                        pg["scorecard"]["usd"] = round(pg["scorecard"]["usd"] + rv["usd"], 4)
            except Exception:
                log.exception("review failed")
        if rep.get("path") and rec.on():  # this match is still the live session; compact the earlier analyzed ones (compact() refuses live dirs)
            for m, ds in postgame.matches(args.rec_dir or ROOT / "recordings").items():
                try:
                    if not review.done(ds):
                        continue  # not learned from yet: keep the full data (uv run review.py DIR, then it compacts after the next game)
                    if (c := postgame.compact(ds)).get("target"):
                        log.info("compacted %s: %s MB -> %s MB", m, c["before_mb"], c["after_mb"])
                except Exception:
                    log.exception("compact %s failed", m)
    threading.Thread(target=run, daemon=True).start()


class RecLog(logging.Handler):
    def emit(self, r):
        rec.event("log", level=r.levelname, logger=r.name, msg=llm.redact(r.getMessage())[:500])


@asynccontextmanager
async def lifespan(app):
    llm.DISABLED, jev.DISABLED = args.no_llm, args.no_jev
    voice.DRY = voice.E("VOICE_DRYRUN", "1" if args.replay or args.frames or args.data_dir else "0") == "1"  # tests never talk
    if not args.no_record:
        with world.lock:
            d = rec.start(args.rec_dir or ROOT / "recordings", meta())
        logging.getLogger().addHandler(RecLog(logging.WARNING))
        log.info("recording to %s (--no-record to disable)", d)
    threading.Thread(target=data.load_static, daemon=True).start()
    if not args.no_cv:
        threading.Thread(target=cv_loop, daemon=True).start()
    if not voice.DRY:
        coach.start(args.port)  # ready check: is Dota running, is the overlay running (starts it when it isn't)
    if not args.no_mic and voice.E("LISTEN", "0" if args.replay or args.frames or args.data_dir else "1") == "1":  # tests never listen
        listen.start()
    if args.replay:
        import tools
        threading.Thread(target=tools.play, args=(tools.rows_from(args.replay), args.speed, ingest), daemon=True).start()
    if not (args.no_obs or args.replay or args.frames or args.data_dir):
        obs.start(lambda: world.W)  # marketing clips via OBS's replay buffer; silently off when OBS isn't running
    task = asyncio.create_task(tick_loop())
    log.info("live-coach on http://127.0.0.1:%d  (GSI token %s, LLM %s, Jev %s, mode %s)", args.port, "set" if TOKEN else "NOT set: accepting any",
             "off" if args.no_llm or not llm.TARGETS else " -> ".join(t["model"] for t in llm.TARGETS),
             "off" if args.no_jev or not jev.E("JEV_API_KEY") else jev.MODEL, "turbo" if world.turbo() else "normal")
    yield
    task.cancel()
    listen.stop()
    if rec.on():
        with world.lock:
            rec.meta(meta())
        rec.stop()  # also on atexit; SIGINT/SIGTERM reach here through uvicorn's graceful shutdown


app = FastAPI(lifespan=lifespan)
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


@app.middleware("http")
async def local_only(req: Request, call_next):
    """The server binds 127.0.0.1, but a web page in the browser can still reach it: refuse a foreign Host header (DNS rebinding)
    and an /api write that is not JSON (a cross-site form or text/plain post skips the CORS preflight). /gsi has its own token."""
    host = (req.headers.get("host") or "").rsplit(":", 1)[0] if not (req.headers.get("host") or "").startswith("[") else "[::1]"
    if host not in LOCAL_HOSTS:
        return JSONResponse({"ok": False, "error": "local requests only"}, 403)
    if req.method == "POST" and req.url.path.startswith("/api/") and not req.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"ok": False, "error": "JSON only"}, 415)
    return await call_next(req)


@app.post("/gsi")
async def gsi(req: Request):
    try:
        body = json.loads(await req.body())
    except ValueError:
        warn_once("badjson", "GSI: non-JSON body ignored")
        return Response(status_code=200)
    if not isinstance(body, dict):
        return Response(status_code=200)
    if TOKEN and world.obj(body, "auth").get("token") != TOKEN:
        warn_once("token", "GSI: token mismatch (re-run tools.py install-gsi and restart Dota?)")
        return Response(status_code=403)
    ingest(body)  # ~0.02 ms; inline keeps payloads in order
    return Response(status_code=200)


@app.get("/events", response_class=EventSourceResponse)
async def events():
    last = None
    while True:
        if V["json"] != last:
            last = V["json"]
            yield ServerSentEvent(raw_data=last)
        await asyncio.sleep(0.25)


@app.get("/version")
def version():
    return {"src_hash": SRC_HASH}


app.mount("/img", StaticFiles(directory=ROOT / "static/img"), name="img")  # Dota 2 logo (Valve CDN copy), favicon


@app.get("/")
def index():
    p = ROOT / "static/index.html"
    return FileResponse(p) if p.exists() else HTMLResponse("dashboard not built yet; world state is at /events")


@app.get("/overlay")
def overlay():
    p = ROOT / "static/overlay.html"
    return FileResponse(p) if p.exists() else HTMLResponse("overlay not built yet; world state is at /events")


def _png(name):
    v = V["vision"]
    try:
        b = getattr(v, name)() if v else None
    except Exception as e:
        warn_once(name, "%s failed (%r)", name, e)
        b = None
    return Response(b, media_type="image/png") if b else Response(status_code=404)


@app.get("/calib.png")
def calib_png():
    return _png("last_calib_png")


@app.get("/map.png")
def map_png():
    import vision
    p = vision.overview_png()
    return FileResponse(p, headers={"Cache-Control": "max-age=3600"}) if p else Response(status_code=404)


@app.get("/minimap.png")
def minimap_png():
    return _png("last_minimap_png")


@app.post("/api/calibrate")
def calibrate():
    if not V["vision"] or V["frame"] is None:
        return JSONResponse({"ok": False, "error": "no frame yet (is Dota running and Screen Recording allowed?)"}, 409)
    try:
        return JSONResponse(V["vision"].calibrate(V["frame"], world.snapshot()))
    except Exception as e:
        log.exception("calibrate failed")
        return JSONResponse({"ok": False, "error": repr(e)}, 500)


@app.post("/api/mark")
def mark(name: str):
    if name not in ("rosh_top", "rosh_bot"):
        return JSONResponse({"ok": False, "error": "name must be rosh_top or rosh_bot"}, 400)
    ok = world.mark_pit(name[5:])
    return JSONResponse({"ok": ok, **({} if ok else {"error": "no GSI hero position yet"})}, 200 if ok else 409)


@app.post("/api/settings")
async def settings(req: Request):
    try:
        b = await req.json()
    except ValueError:
        b = None
    b = b if isinstance(b, dict) else {}
    kw = {k: world.num(b[k]) for k in ("role", "bracket") if k in b}  # only keys sent are changed
    if any(v is not None and v not in range(1, 6 if k == "role" else 9) for k, v in kw.items()) or \
            any(k in b and not isinstance(b[k], bool) and not (k == "turbo" and b[k] == "auto")
                for k in ("turbo", "voice", "mic", "commentary", "overlay", "quiet", "ai")) or \
            b.get("verbosity", "normal") not in ("low", "normal", "high") or b.get("mic_mode", "ptt") not in listen.MODES or \
            ("ptt_key" in b and not listen.parse_key(b["ptt_key"])) or \
            ("volume" in b and not (isinstance(b["volume"], (int, float)) and not isinstance(b["volume"], bool) and 0 <= b["volume"] <= 1)):
        return JSONResponse({"ok": False, "error": "role 1-5|auto, bracket 1-8 (or null), turbo true/false/auto, voice/mic/commentary/overlay/quiet/ai "
                                                   "true/false, verbosity low/normal/high, volume 0-1, mic_mode ptt/always/off, "
                                                   "ptt_key e.g. mouse4|mouse5|f13|ctrl+space"}, 400)
    kw |= {k: b[k] for k in ("turbo", "voice", "mic", "commentary", "overlay", "verbosity", "mic_mode", "volume", "quiet", "ai") if k in b}
    if "ptt_key" in b:
        kw["ptt_key"] = str(b["ptt_key"]).lower().replace(" ", "")
    if kw.get("turbo") == "auto":  # back to auto-detect
        kw.update(turbo=False, turbo_match=None)
    if b.get("role") == "auto":
        kw.update(role=None, role_match="auto")  # role_match != matchid -> world auto-detects
    world.set_settings(**kw)
    rec.event("settings", **kw)
    return {"ok": True, "settings": world.W["settings"]}


KEYS = {"openai": "OPENAI_API_KEY", "jev": "JEV_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}
LLM_DEFAULTS = {"LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "gpt-6-luna"}  # an OpenAI key alone turns the advice model on


@app.get("/api/keys")
def keys_get():
    """Which API keys are set (never the values)."""
    return {n: bool(os.environ.get(v)) for n, v in KEYS.items()}


@app.post("/api/keys")
async def keys_post(req: Request):
    """Dashboard key form: {openai|jev|anthropic|gemini: "key" ("" removes)} -> ./.env (chmod 600, gitignored) and the running process."""
    if not req.headers.get("content-type", "").startswith("application/json"):  # a JSON preflight keeps other websites from posting here
        return JSONResponse({"ok": False, "error": "JSON only"}, 415)
    b = await req.json()
    if not isinstance(b, dict) or not b or any(k not in KEYS or not isinstance(v, str) or (v and not re.fullmatch(r"[\w.\-]{16,400}", v.strip()))
                                               for k, v in b.items()):
        return JSONResponse({"ok": False, "error": "that does not look like an API key"}, 400)
    new = {KEYS[k]: v.strip() for k, v in b.items()}
    if new.get("OPENAI_API_KEY"):
        new |= {k: v for k, v in LLM_DEFAULTS.items() if not os.environ.get(k)}
    p = ROOT / ".env"
    lines = [ln for ln in (p.read_text().splitlines() if p.exists() else []) if ln.split("=", 1)[0].strip() not in new]
    lines += [f"{k}={v}" for k, v in new.items() if v]
    p.touch(mode=0o600)
    p.chmod(0o600)
    p.write_text("\n".join(lines) + "\n")
    for k, v in new.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    llm.reload()
    return {"ok": True, **keys_get()}


@app.post("/api/voice-test")
def voice_test():
    """Play one calm line ("I'm here. You'll hear me like this.") at the current volume so the player can check the sound."""
    with world.lock:
        voice.S["vol"] = v if isinstance(v := world.W["settings"].get("volume"), (int, float)) else 1.0
        ok = voice.test()
    return JSONResponse({"ok": ok, **({} if ok else {"error": "the test line is not recorded yet (or a line is playing)"})}, 200 if ok else 409)


@app.post("/api/feedback")
async def feedback(req: Request):
    """Mark a moment for post-game review: events.jsonl gets clock + attention + decision + top alerts, plus a full frame."""
    try:
        b = await req.json()
    except ValueError:
        b = None
    b = b if isinstance(b, dict) else {}
    if b.get("kind") not in ("good", "bad", "note"):
        return JSONResponse({"ok": False, "error": "kind must be good, bad or note"}, 400)
    if not rec.on():
        return JSONResponse({"ok": False, "error": "recording is off (--no-record)"}, 409)
    with world.lock:
        W = world.W
        n = rec.mark(fb=b["kind"], text=str(b.get("text") or "")[:200], ref=str(b["ref"])[:80] if b.get("ref") else None,
                     attention=dict(W["attention"]), decision=W["decision"], alerts=W["alerts"][:3],
                     me={k: W["me"][k] for k in ("hero", "alive", "hp_pct", "gold", "x", "y")})
    V["want_full"] = "feedback"
    return {"ok": True, "marks": n}


def main():
    global args, TOKEN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=3939)
    ap.add_argument("--frames", help="replay PNG frames from DIR instead of live capture")
    ap.add_argument("--replay", help="replay a gsi.jsonl (fixtures/sim.jsonl or recordings/*/gsi.jsonl) in-process")
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed")
    ap.add_argument("--record", action="store_true", help="(default now; kept for old command lines)")
    ap.add_argument("--no-record", action="store_true", help="don't write recordings/<ts>[_<matchid>]/")
    ap.add_argument("--rec-dir", help="recordings root (default ./recordings)")
    ap.add_argument("--data-dir", help="calib/settings/token dir (default ./data or $COACH_DATA_DIR); tests use a temp dir")
    ap.add_argument("--turbo", action="store_true", help="switch Turbo mode on (persisted like POST /api/settings {turbo: true})")
    ap.add_argument("--demo", action="store_true", help="accept hero demo / custom games")
    ap.add_argument("--no-cv", action="store_true", help="GSI only")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-jev", action="store_true")
    ap.add_argument("--no-obs", action="store_true", help="no OBS clip saving")
    ap.add_argument("--no-mic", action="store_true", help="no voice input (listen.py); also off for --replay/--frames/--data-dir unless LISTEN=1")
    ap.add_argument("--cv-fps", type=float, default=10)  # 10 Hz: a hero on the minimap for 100 ms is still caught (user 2026-11-24); CV ~5 ms/frame
    args = ap.parse_args()
    if args.data_dir:
        world.set_data_dir(args.data_dir)
    if args.turbo:
        world.set_settings(turbo=True, turbo_match="always")
    try:
        TOKEN = (world.DATA / "token").read_text().strip() or None
    except OSError:
        TOKEN = None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", timeout_graceful_shutdown=1)  # open /events tabs must not block Ctrl-C


if __name__ == "__main__":
    main()
