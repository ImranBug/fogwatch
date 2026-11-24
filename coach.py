"""Coach v2: one goal per game, stated before the horn, counted live, scored after and carried to the next game.
tick(W) runs in app.py's tick loop under world.lock, before voice.tick, and fills:
  W.goal    deaths target from the profile: max(3, round(0.75 x average deaths of the last 5 games)), clamp 3-10; 6 with no history
  W.brief   pre-horn: goal, lane job, talk hint (also queued as voice lines once per game, at about -0:40)
  W.debrief while dead: WHY, from the world state at the moment of death (cause), the call the coach made before it, the budget
  W.coach   quiet / eased rules / spoken-line budget / demoted rules;  W.history  last 5 games + next goal (data/profile);  W.ready
It also queues the debrief lines (voice tier D) and the praise lines (tier P). The game's goal result is written to
data/profile/games.jsonl by postgame.record(), so W.history survives restarts."""
import json, logging, random, subprocess, sys, threading, time
from pathlib import Path
import rec, rules, voice, world
from tools import _jsonl

log = logging.getLogger("coach")
PRE = ("DOTA_GAMERULES_STATE_STRATEGY_TIME", "DOTA_GAMERULES_STATE_TEAM_SHOWCASE", "DOTA_GAMERULES_STATE_WAIT_FOR_MAP_TO_LOAD", voice.PRE_GAME)
DEBRIEF_S = 3.0  # the kill my death bought lands in the score by then (1.5 s judged useful trades as "warned", 2026-11-13)
CAUSES = ("streak", "burst", "warned", "good_trade", "fight", "coach_sent", "late_call", "stayed_low", "outnumbered", "ganked", "first_in", "lane_loss", "alone",
          "coach_miss")  # checked in this order
S, H, P = {}, {}, {"dota": None, "overlay": None, "on": False}  # per-match state, history cache, process probe


def _reset(mid=None):
    S.update(match=mid, ka=[], d=None, deaths=[], streak_from=0, dying=None, last_cause=None, over=False, debrief=None, up_at=None,
             briefed=False, low_since=None, low_seen=-1e9, score=[], snap=None, r1=None, praised=set(), fight=None, goal=None)


_reset()


# ---- goal + history (data/profile) -----------------------------------------------------------------------------------
def _games():
    """games.jsonl rows, oldest first (by match id: re-analysing an old match must not make it the newest), cached by mtime."""
    p = world.DATA / "profile" / "games.jsonl"
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        key = (str(p), None)
    if H.get("key") != key:
        H.update(key=key, games=sorted(_jsonl(p), key=lambda g: int(m) if (m := str(g.get("matchid"))).isdigit() else 0) if key[1] else [])
    return H["games"]


def _deaths(g):
    return world.num((g.get("kda") or [0, None])[1], len(g.get("deaths") or []))


def text(n):
    return f"{n} deaths or fewer"


def target(mid=None):
    """(deaths target, why) for match `mid` from the games before it; mid None = the next game."""
    prior = [g for g in _games() if not str(mid or "").isdigit() or int(str(g.get("matchid") or 0)) < int(mid)][-5:]
    if not prior:
        return 6, "no games on record yet"
    avg = sum(_deaths(g) for g in prior) / len(prior)
    return max(3, min(10, int(0.75 * avg + 0.5))), f"avg {avg:.1f} deaths in your last {len(prior)} game{'s' if len(prior) > 1 else ''}"


def history():
    def row(g):
        ds, gl = g.get("deaths") or [], g.get("goal") or {}
        return {"result": g.get("result"), "hero": g.get("hero"), "deaths": _deaths(g), "goal": gl.get("text"), "met": gl.get("met"),
                "warned_died": round(sum(d.get("warned") is True for d in ds) / len(ds), 2) if ds else None,
                "turbo": g.get("mode") == "turbo", "date": g.get("date")}
    return {"games": [row(g) for g in _games()[-5:][::-1]], "next_goal": text(target()[0])}  # newest first


# ---- ready check -----------------------------------------------------------------------------------------------------
def start(port=3939):
    """Background probe (app.py lifespan): is Dota running, is the overlay running. Every 5 s, never on the tick.
    The overlay is started here when it is switched on and not running (at most every 30 s); the dashboard's
    Overlay button hides / shows it."""
    def run():
        tried = 0.0
        while True:
            for k, cmd in (("dota", ["pgrep", "-x", "dota2"]), ("overlay", ["pgrep", "-f", "overlay.py"])):
                try:
                    P[k] = subprocess.run(cmd, capture_output=True, timeout=3).returncode == 0
                except Exception:
                    P[k] = None
            if P["overlay"] is False and world.W.get("settings", {}).get("overlay") is not False and time.time() - tried > 30:
                tried = time.time()
                try:
                    root = Path(__file__).parent
                    subprocess.Popen([sys.executable, str(root / "overlay.py"), "--port", str(port)], cwd=root, start_new_session=True,
                                     stdout=open(world.DATA / "overlay.log", "ab"), stderr=subprocess.STDOUT)
                    log.info("coach: overlay was not running; started it")
                except Exception as e:
                    log.warning("coach: could not start the overlay (%r)", e)
            try:  # macOS secure keyboard input (often left on by the lock screen): no key press reaches a listen-only tap
                P["secure"] = b"kCGSSessionSecureInputPID" in subprocess.run(["ioreg", "-l", "-d", "1", "-w", "0"], capture_output=True, timeout=3).stdout
            except Exception:
                P["secure"] = None
            time.sleep(5)
    if not P["on"]:
        P["on"] = True
        threading.Thread(target=run, daemon=True, name="coach-probe").start()


def ready(W):
    st, cv, tk, age = W["settings"], W["cv"], W.get("talk") or {}, W["src_age"]
    live = age["gsi_s"] is not None and age["gsi_s"] <= 5
    cvok = age["cv_s"] is not None and age["cv_s"] <= 5 and cv.get("frame_ok")
    mm = W.get("menu") or {}
    if live:
        gsi = (True, "Ready: game data is arriving.")
    elif mm.get("state") == "found":
        gsi = (True, "Match found. Accept it in Dota.")
    elif mm.get("state") == "searching":
        what = " ".join(x for x in (mm.get("kind"), mm.get("mode")) if x)
        gsi = (True, f"Finding a match{': ' + what if what else ''}" + (f" ({mm['for_s'] // 60}:{mm['for_s'] % 60:02d})" if mm.get("for_s") else "") + ".")
    elif P["dota"] or cvok:  # the one case that needs the player: the game is open but GSI is not set up
        # Dota sends nothing in its menus, so silence there is only a setup problem for someone who has never had a game recorded
        gsi = (None, "Dota is open. Waiting for a match.") if _games() else (False, "Dota is running but not sending data: add the launch option.")
    else:
        gsi = (None, "Dota is not running." if P["dota"] is False else "Waiting for Dota.")
    screen = (True, "The coach can see the game.") if cvok else (False, str(cv["err"])[:120]) if cv.get("err") and (P["dota"] or live) else \
        (None, "Checked when Dota is open.")
    p, urgent = voice.provider(), all(voice.find(t, "U") for t in voice.U.values())
    vo = (None, "Voice is switched off.") if st.get("voice") is False else (True, "Voice is ready.") if urgent else \
        (False, "Voice lines are still being recorded." if p else "No voice provider key: the coach stays silent.")
    key = tk.get("key") or st.get("ptt_key") or "mouse4"
    mic = (None, "The mic is muted.") if st.get("mic") is False or tk.get("mode") == "off" else \
        (False, f"macOS is blocking the talk key ({key}): lock your screen and unlock it, or use a mouse button.") \
        if P.get("secure") and not key.startswith("mouse") and tk.get("state") == "on" else \
        (True, f"Hold {key} to talk to me.") if tk.get("state") == "on" else \
        (False, str(tk.get("err") or "The mic needs a permission.")[:120]) if tk.get("state") == "denied" or tk.get("err") else (None, "The mic is starting.")
    ov = (None, "The overlay is switched off.") if st.get("overlay") is False else (True, "The overlay is running.") if P["overlay"] else \
        (None, "Not checked yet.") if P["overlay"] is None else (False, "The overlay is not running; starting it…")
    return {k: {"ok": ok, "text": t} for k, (ok, t) in (("gsi", gsi), ("screen", screen), ("voice", vo), ("mic", mic), ("overlay", ov))}


# ---- per tick --------------------------------------------------------------------------------------------------------
def tick(W):
    g, me, c = W["game"], W["me"], W["game"]["clock"]
    mid = g["matchid"]
    if S["match"] != mid:
        _reset(mid)
    if W["settings"].get("quiet") and mid and W["settings"].get("quiet_match") != mid:
        world.set_settings(quiet=False)  # "quiet" lasts until the next match
    st = W["settings"]
    verb, vol = st.get("verbosity") or "normal", st.get("volume")
    W["_"]["eased"] = eased = {r: t for r, t in (W["_"].get("eased") or {}).items() if c is not None and t > c}
    W["coach"] = {"quiet": bool(st.get("quiet")), "eased": eased, "spoken": voice.S["n_nu"], "cap": voice.CAP.get(verb, voice.CAP["normal"]),
                  "verbosity": verb, "volume": vol if isinstance(vol, (int, float)) else 1.0, "demoted": sorted(W["_"].get("demoted") or [])}
    W["history"], W["ready"] = history(), ready(W)
    if not mid or g["state"] is None or c is None:
        W["goal"] = W["brief"] = W["debrief"] = None
        return
    S["goal"] = S["goal"] or target(mid)
    tgt, d = S["goal"][0], me["d"] or 0
    over = g["state"] == "DOTA_GAMERULES_STATE_POST_GAME" or g["win_team"] not in (None, "none", "")
    W["goal"] = {"kind": "deaths", "target": tgt, "now": d, "left": tgt - d, "met": False if d > tgt else True if over else None,
                 "text": text(tgt), "why": S["goal"][1]}
    _brief(W, c, tgt)
    if g["state"] == voice.IN_PROGRESS and c > 0 and not g["paused"]:
        _track(W, c)
        if S["d"] is not None and me["d"] is not None and me["d"] > S["d"]:
            S.update(dying={"c": c, **(S["snap"] or {"c": c})}, r1=None, fight=None, up_at=None, debrief=None)
            S["deaths"].append(c)
        if S["dying"] and c >= S["dying"]["c"] + DEBRIEF_S:
            _debrief(W, c, tgt)
    S["d"] = me["d"] if me["d"] is not None else S["d"]
    if S["debrief"] and me["alive"]:  # gone the moment the hero is back (it lingered 5 s and read as old data, player 2026-11-12)
        S["debrief"] = None
    W["debrief"] = S["debrief"]


def _brief(W, c, tgt):
    me, st, tk = W["me"], W["settings"], W.get("talk") or {}
    if W["game"]["state"] not in PRE:
        W["brief"] = None
        return
    lane = voice.B[{1: "lane_core", 2: "lane_mid", 3: "lane_off"}.get(me["role"], "lane_support") if me.get("role_src") == "manual"
                   else "lane_support" if (me["role"] or 0) >= 4 else "lane_any"]  # a support hero: the support job (Lich, -0:42)  # the hero guess made a mid Warlock "stay behind your carry" (review -0:41)
    mic = st.get("mic") is not False and tk.get("mode") == "ptt"  # open mic needs no key: no "hold your talk key" (player, 2026-11-12)
    W["brief"] = {"goal": voice.B[f"goal_{tgt}"], "lane": lane,
                  "talk": f"Hold {tk.get('key') or st.get('ptt_key') or 'mouse4'} to talk to me" if mic else None}
    if S["briefed"] or W["game"]["state"] != voice.PRE_GAME or not -45 <= c <= -12:
        return
    S["briefed"] = True
    p = world.DATA / "profile" / "coach.json"
    n = world.num(world._load(p).get("briefs"), 0) or 0
    lines = [("goal", voice.B[f"goal_{tgt}"]), ("lane", lane)] + ([("talk_hint", voice.B["talk_hint"])] if mic and n % 5 == 0 else [])
    for cue, t in lines:  # the first install's brief has the talk hint, then every 5th game
        voice.queue(cue, "B", t, c, c + 16)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"briefs": n + 1}))
    except OSError as e:
        log.warning("coach: profile not writable (%r)", e)


def _track(W, c):
    """While alive: the snapshot a debrief is classified from, the low-HP timer, score changes, and the praise triggers."""
    me, team = W["me"], W["me"]["team"]
    my = 0 if team == "radiant" else 1
    sc = W["game"]["score"]
    if not S["score"] or S["score"][-1][1:] != (sc[my], sc[1 - my]):
        S["score"] = [x for x in S["score"] if c - x[0] <= 60] + [(c, sc[my], sc[1 - my])]
    if not me["alive"] or me["x"] is None:
        return
    pos, hp = (me["x"], me["y"]), me["hp_pct"] if me["hp_pct"] is not None else 100
    dist = lambda e: voice._d((e["x"], e["y"]), pos)  # noqa: E731
    foes = sorted(s for s, e in W["heroes"].items() if e.get("team") and not e["ally"] and e["visible"] and not e["dead"] and dist(e) <= 1500)
    allies = sorted(dist(e) for s, e in W["heroes"].items() if e["ally"] and s != me["hero"] and e["visible"] and not e["dead"])
    if hp < 50 and foes:
        S["low_since"], S["low_seen"] = S["low_since"] if S["low_since"] is not None else c, c
    elif hp >= 50 or c - S["low_seen"] > 3:
        S["low_since"] = None
    r10 = next((a for a in W["alerts"] if a["rule"] == "R10"), None)
    home = world.home_dist(*pos, team)
    S["dis_at"] = c if me.get("disabled") or me.get("silenced") else S.get("dis_at", -1e9)  # silenced: no blink / escape spell (31:01)
    S["ka"] = [k for k in S.get("ka") or [] if c - k[0] <= 10] + [(c, (me["k"] or 0) + (me["a"] or 0))]
    S["snap"] = {"at": c, "home": home, "fight_s": c - S["fight"]["start"] if S["fight"] and voice.S["fight"] else 0, "ka_gain": S["ka"][-1][1] - S["ka"][0][1], "disabled": c - S["dis_at"] <= 3, "foes": foes, "ally_d": round(allies[0]) if allies else None, "allies": sum(a <= 1500 for a in allies),
                 "low_for": c - S["low_since"] if S["low_since"] is not None else 0, "laning": W.get("phase") == "laning",
                 "outnumbered": bool(r10) and (r10.get("tier") or 3) < 3 and r10.get("zone") == world.zone(*pos, team)
                 and (r10.get("foes") or 0) > (r10.get("allies") or 0) + 1}  # T1/T2 only: the base is always defended

    def praise(cue, until):
        voice.queue(cue, "P", voice.P[cue], c, until, keys=[("praise", 180)], alive=True)
    # survived a high R1 by leaving: 3 s after it cleared (15 s came when they were already back from base), 800+ closer to home,
    # nobody within 1500
    hi = any(a["rule"] == "R1" and a["level"] == "high" for a in W["alerts"])
    r1 = S["r1"]
    if hi:
        S["r1"] = {"home": r1["home"] if r1 else home, "clear": None, "left": False}
    elif r1 and r1["clear"] is None:
        r1.update(clear=c, left=r1["home"] - home >= 800)
    elif r1 and c - r1["clear"] >= 3:
        if r1["left"] and not foes:
            praise("got_out", c + 6)
        S["r1"] = None
    # obeyed a "come back" / "back off" call within 8 s
    for x in voice.S["calls"][-3:]:
        if x["cue"] in ("too_deep", "back_off") and 0 < c - x["c"] <= 8 and x["c"] not in S["praised"] and x["home"] and x["home"] - home >= 600:
            S["praised"].add(x["c"])
            praise("obeyed", x["c"] + 26)  # the 20 s gap after the call itself still holds
    # won a fight, alive and part of it
    f, ka = S["fight"], (me["k"] or 0) + (me["a"] or 0)
    if voice.S["fight"]:
        S["fight"] = f = {**(f or {"score": (sc[my], sc[1 - my]), "ka": ka, "start": c}), "last": c}
        hist = W["_"].get("hist") or []
        steady = not hist or hp >= next((x[3] for x in hist if x[0] >= c - 3), hp) - 5
        clear = not any(dist(e) <= 1200 for s, e in W["heroes"].items() if e.get("team") and not e["ally"] and e["visible"] and not e["dead"])
        if not f.get("praised") and ka > f["ka"] and rules.winning(W, c) and steady and clear:  # won it, but not 4 s before dying (3:48, 8:19)
            f["praised"] = True
            praise("nice_fight", c + 4)
    elif f and c - f["last"] >= 4:
        if not f.get("praised") and sc[my] - f["score"][0] > sc[1 - my] - f["score"][1] and ka > f["ka"]:
            praise("nice_fight", c + 4)
        S["fight"] = None


def classify(x, c, called, score, streak):
    """Cause of a death from the snapshot of the last living tick `x`. called = the last retreat call {word, s_before, left}.
    ponytail: decided 1.5 s after the death, so a kill that lands later than that does not make it a good trade."""
    kills = sum(b[1] - a[1] for a, b in zip(score, score[1:]) if b[0] >= x["c"] - 8)
    died = sum(b[2] - a[2] for a, b in zip(score, score[1:]) if b[0] >= x["c"] - 8)  # mine included
    before = sum(b[2] - a[2] for a, b in zip(score, score[1:]) if x["c"] - 10 <= b[0] < x["c"] - 0.5)  # allies who died first
    if kills >= 1 and kills >= died and x.get("ka_gain", 0) >= 1:  # my kill/assist, even after a call: "a useful death, don't judge" (player)
        return "good_trade"
    if (x.get("allies", 0) >= 2 and (kills >= 1 or before >= 1)) or kills - died >= 2 or (x.get("fight_s", 0) >= 8 and not called):  # a won team fight (22:56), a real team fight: dying in it is not a mistake (player, 2026-11-12)
        return "fight"
    # "warned" only when the player could have left and didn't: not when they were leaving, stunned/hexed, or the call came too late
    # (player 2026-11-12: "I did leave on his call", "I was casting Black Hole", "I didn't have time to react")
    if x.get("burst") and not (called and called["s_before"] >= 6):
        return "burst"  # full HP 5 s before the death and bound: no time to obey (3:47)
    if called and 5 <= called["s_before"] <= 12 and not called["left"] and (not x.get("disabled") or called["s_before"] >= 10):
        return "warned"  # a call 23 s earlier for another fight is not this death's warning (22:37)  # 10+ s to leave before the Chrono caught them is still "warned" (11:24)
    if x.get("sent") and not called:
        return "coach_sent"  # my "Defend mid" sent a 37% Sniper onto Morphling (7:29): own it, not "too far from your team"
    if streak:
        return "streak"  # after the fair causes: "Reset" for a good death was wrong (player, 4:16)
    if called and 2 <= called["s_before"] <= 12:
        return "late_call"
    if x.get("low_for", 0) >= 8:
        return "stayed_low"  # lingering low after the fight (22:37), not only in lane  # I called it, and leaving wasn't enough: not "my miss", not "first in" (review 14:25, 21:28)
    if x.get("laning") and x.get("low_for", 0) >= 8:
        return "stayed_low"
    if x.get("outnumbered"):
        return "outnumbered"
    if x.get("laning") and len(x.get("foes") or ()) >= x.get("allies", 0) + 2:
        return "ganked"  # a 3-man lane gank is not "first in" (0:49)
    if len(x.get("foes") or ()) >= 2 and x.get("allies") and not before:
        return "first_in"
    if x.get("laning") and not x.get("allies") and x.get("foes"):
        return "lane_loss"  # a mid laner is always alone: "nobody was with you" said nothing (review 4:37, 7:04)
    if x.get("ally_d") is None or x["ally_d"] > 2500:
        return "alone"
    return "coach_miss"  # no call, a call under 5 s before, or a call the player obeyed and died anyway


def _debrief(W, c, tgt):
    """1.5 s after a death: classify it, publish W.debrief and queue at most two lines (cause, then buyback). The death count is
    on screen only: spoken after every death it was "very annoying"."""
    me, x = W["me"], S["dying"]
    S["dying"] = None
    call = next((k for k in voice.S["calls"] if k["word"] and 0 <= x["c"] - k["c"] <= 12), None)  # this death's call, not an older fight's  # the first retreat call of that half minute
    x["ka_gain"] = max(x.get("ka_gain", 0), (me["k"] or 0) + (me["a"] or 0) - (S["ka"][0][1] if S.get("ka") else 1e9))  # assists credited after death
    hist = [h for h in W["_"].get("hist") or [] if x["c"] - 6 <= h[0] <= x["c"] - 4]
    x["burst"] = bool(hist) and hist[0][3] >= 80
    x["sent"] = any(k["cue"] in ("defend", "join") and 0 <= x["c"] - k["c"] <= 30 for k in voice.S["calls"])
    called = call and {"word": call["word"], "s_before": round(x["c"] - call["c"]),
                       "left": bool(call["home"] and x.get("home")) and call["home"] - x["home"] >= 600}
    n = len(S["deaths"]) - S["streak_from"]
    streak = n >= 3 and x["c"] - S["deaths"][-3] <= 240
    if streak:
        S["streak_from"] = len(S["deaths"])  # the reset line restarts the count
    cause, left = classify(x, c, called, S["score"], streak), tgt - (me["d"] or 0)
    budget = f"{left} left in your budget." if left > 0 else "That was the last one. Zero left." if left == 0 else "Over budget."
    opts = [t for k, t in voice.D.items() if k == cause or k.startswith(cause + "~")]
    line = random.choice([t for t in opts if t != S.get("last_line")] or opts)  # the same lesson in different words each time
    say = line
    if cause == "lane_loss" and len(x.get("foes") or ()) == 1 and (nm := voice.hero_name(x["foes"][0])):
        line, say = voice.lane_line(nm), [voice.lane_line(nm), line]  # "Razor wins that trade..." (recorded at warm-up; else generic)
    S["last_line"] = line
    S["debrief"] = {"at": x["c"], "n": me["d"], "cause": cause, "line": line, "by": [f"npc_dota_hero_{s}" for s in x.get("foes") or []],
                    "called": called and {"word": called["word"], "s_before": called["s_before"]}, "budget": budget}
    lines = [(f"cause:{cause}", say, True)]  # every death gets its cause; repeats use other words (deaths without one, review 2026-11-12)
    if not streak:
        if c >= (900 if world.turbo() else 1800) and None not in (me["gold"], me["buyback_cost"]) and c - S.get("bb_at", -1e9) >= 300 \
                and ((me["role"] or 0) < 4 or me["gold"] >= me["buyback_cost"]):  # supports: only when it's affordable
            S["bb_at"] = c  # at most once per 5 min (review 21:31)
            lines.append(("buyback", voice.D["bb_up" if me["gold"] >= me["buyback_cost"] and not me["buyback_cd"] else "bb_no"], False))
    until = x["c"] + max(7, min(14, (me["respawn_s"] or 6) + 1))  # 7 s: room for both lines even on a short Turbo respawn
    for cue, t, is_cause in lines[:2]:
        voice.queue(cue, "D", t, c, until, cause=is_cause)
    S["last_cause"] = cause
    rec.event("debrief", **S["debrief"], lines=[t if isinstance(t, str) else t[0] for _, t, _ in lines[:2]], ally_d=x.get("ally_d"), low_for=round(x.get("low_for", 0), 1))
    log.info("coach %s: death %s: %s%s", world.mmss(x["c"]), me["d"], cause, f" (called {called['word']} {called['s_before']} s before)" if called else "")


def selfcheck():
    """[(name, ok, detail)]: goal from history, causes, budget lines, streak, brief, praise, quiet. Temp data dir, no audio."""
    import tempfile
    from pathlib import Path
    res, saved = [], (world.DATA, voice.DRY, voice.GEN, voice.find, dict(voice.S), dict(S), rec.event)
    world.set_data_dir(Path(tempfile.mkdtemp(prefix="coach-check-")))  # settings.json and the profile go to a temp dir
    voice.DRY, voice.GEN, voice.find = True, False, lambda text, tier=None: f"x/{voice.slug(text)}"
    evs = []
    rec.event = lambda kind, **kw: evs.append((kind, kw))  # noqa: E731
    try:
        t0 = target("9000000005")
        (world.DATA / "profile").mkdir(parents=True)
        (world.DATA / "profile" / "games.jsonl").write_text("".join(json.dumps(
            {"matchid": str(9000000000 + i), "kda": [1, d, 5], "deaths": [{"warned": i == 3}] * d, "result": "win", "hero": "lich", "mode": "turbo",
             "date": "2026-11-10", "goal": {"text": text(6), "met": d <= 6}}) + "\n" for i, d in ((3, 13), (1, 8), (2, 5))))
        hist = history()
        res.append(("coach: goal = 0.75 x average deaths of the last games, clamp 3-10; 6 with no history; history newest first",
                    t0[0] == 6 and target("9000000004")[0] == 7 and target("9000000002")[0] == 6 and target()[0] == 7
                    and [g["deaths"] for g in hist["games"]] == [13, 5, 8] and hist["games"][0]["met"] is False and hist["next_goal"] == text(7)
                    and hist["games"][0]["warned_died"] == 1.0, f"{t0} {target('9000000004')} {[g['deaths'] for g in hist['games']]}"))
        base = {"c": 600, "home": 9000, "foes": ["sven"], "ally_d": 900, "allies": 1, "low_for": 0, "laning": False, "outnumbered": False, "ka_gain": 1}
        call = lambda s, left=False: {"word": "BACK", "s_before": s, "left": left}  # noqa: E731
        sc = [(580, 0, 0), (600, 0, 1)]
        got = [classify(base, 601.5, None, sc, True), classify(base, 601.5, call(9), sc, False),
               classify(base, 601.5, None, [(580, 0, 0), (597, 1, 0), (600, 1, 1)], False),
               classify({**base, "allies": 2}, 601.5, None, [(580, 0, 0), (595, 0, 1), (600, 0, 2)], False), classify({**base, "laning": True, "low_for": 9}, 601.5, None, sc, False),
               classify({**base, "outnumbered": True}, 601.5, None, sc, False), classify({**base, "foes": ["sven", "lion"]}, 601.5, None, sc, False),
               classify({**base, "laning": True, "allies": 0, "foes": ["sven", "lion"]}, 601.5, None, sc, False),
               classify({**base, "laning": True, "allies": 0}, 601.5, None, sc, False),
               classify({**base, "ally_d": 4000, "allies": 0}, 601.5, None, sc, False), classify(base, 601.5, call(2), sc, False),
               classify(base, 601.5, call(9, True), sc, False), classify(base, 601.5, call(4), sc, False),
               classify({**base, "foes": ["sven", "lion"]}, 601.5, None, [(580, 0, 0), (595, 0, 1), (600, 0, 2)], False)]
        res.append(("coach: debrief causes from the world state at the moment of death (a call 5+ s before is 'warned' only if they "
                    "stayed and weren't stunned; a team fight is 'fight'; under 5 s is the coach's miss; an ally who died first = not first in)",
                    got == ["streak", "warned", "good_trade", "fight", "stayed_low", "outnumbered", "first_in", "ganked", "lane_loss", "alone",
                            "late_call", "late_call", "late_call", "coach_miss"]
                    and classify({**base, "disabled": True}, 601.5, call(9), sc, False) == "late_call"
                    and classify({**base, "fight_s": 10}, 601.5, None, sc, False) == "fight"
                    and classify({**base, "sent": True}, 601.5, None, sc, False) == "coach_sent"
                    and classify({**base, "ka_gain": 0}, 601.5, None, [(580, 0, 0), (597, 1, 0), (600, 1, 1)], False) == "coach_miss"
                    and classify(base, 601.5, call(9), [(580, 0, 0), (597, 1, 0), (600, 1, 1)], False) == "good_trade", str(got)))  # a useful death counts even after a call
        world.reset()
        W = world.W  # the real dict: tick() clears "quiet" through world.set_settings
        W["game"].update(state=voice.PRE_GAME, clock=-40, matchid="9000000004", paused=False)
        W["src_age"]["gsi_s"] = 0.5
        W["settings"] = {"voice": True}
        W["me"].update(hero="lich", team="radiant", alive=True, hp_pct=80, x=-3000, y=-3000, role=5, role_src="manual", gold=100, buyback_cost=500, d=0, k=0, a=0, respawn_s=0)
        W["heroes"] = {"sven": {"team": "dire", "ally": False, "visible": True, "dead": False, "x": -2500, "y": -3000},
                       "lina": {"team": "radiant", "ally": True, "visible": True, "dead": False, "x": 3000, "y": 3000}}
        said = []

        def step(c, **me):
            W["game"]["clock"] = c
            W["me"].update(me)
            tick(W)
            voice.tick(W)
            sp = voice.S["spoken"]
            said.append(sp["text"] if sp and sp["at_clock"] == c else None)
            return W
        voice._reset("9000000004")
        _reset("9000000004")
        W["talk"] = {"mode": "ptt"}  # the talk-key hint is for push-to-talk only
        alone = {t for k, t in voice.D.items() if k == "alone" or k.startswith("alone~")}  # any wording of the same lesson
        for c in (-40, -37, -34, -31):
            step(c)
        brief, n_brief = dict(W["brief"]), world._load(world.DATA / "profile" / "coach.json").get("briefs")
        W["game"]["state"] = voice.IN_PROGRESS
        step(100)
        step(101, alive=False, d=1, respawn_s=8)
        d0 = W["debrief"]
        step(101 + DEBRIEF_S + 0.1)
        d1 = dict(W["debrief"])
        step(106)
        step(110, alive=True)
        step(116)
        res.append(("coach: brief before the horn (goal, lane job, talk hint on the first game); a death is debriefed 1.5 s later: "
                    "the cause line (the death count is never spoken); W.debrief gone at the respawn",
                    said[:4] == [voice.B["goal_7"], voice.B["lane_support"], voice.B["talk_hint"], None] and n_brief == 1
                    and brief["goal"] == voice.B["goal_7"] and "mouse4" in brief["talk"] and W["brief"] is None and d0 is None
                    and d1["line"] in alone and {**d1, "line": None} == {"at": 101, "n": 1, "cause": "alone", "line": None, "by": ["npc_dota_hero_sven"],
                                                          "called": None, "budget": "6 left in your budget."}
                    and said[6] in alone and said[7] is None and W["debrief"] is None
                    and W["goal"]["now"] == 1 and W["goal"]["left"] == 6 and W["goal"]["met"] is None and [k for k, _ in evs].count("debrief") == 1, f"{said} {d1}"))
        del said[:]
        step(200, alive=False, d=2, respawn_s=8), step(200 + DEBRIEF_S + 0.1), step(205), step(210, alive=True)
        step(300, alive=False, d=3, respawn_s=8), step(300 + DEBRIEF_S + 0.1), step(305)
        streak = W["debrief"]["cause"]
        step(310, alive=True)
        for i, c in enumerate((400, 530, 660, 790, 920)):
            step(c, alive=False, d=4 + i, respawn_s=8), step(c + DEBRIEF_S + 0.1), step(c + 5), step(c + 9, alive=True)
        late = [s for s in said[8:] if s]
        res.append(("coach: the goal counter: the same cause again = the lesson in other words; 3 deaths in 4 min = the reset line alone; "
                    "last one; goal met turns false over the target",
                    said[1] in alone and said[1] != d1["line"] and streak == "streak" and said[5:7] == [voice.D["streak"], None]
                    and len(late) == 5 and set(late) <= alone and all(a != b for a, b in zip(late, late[1:]))
                    and W["goal"]["met"] is False and W["goal"]["left"] == -1 and W["coach"]["spoken"] == voice.S["n_nu"] >= 3, f"{said} {voice.S['n_nu']}"))
        del said[:]
        W["heroes"]["sven"]["visible"] = False
        voice.S["calls"].append({"c": 1000, "cue": "too_deep", "tier": "C", "rule": "R11", "word": "COME BACK", "why": "R11", "text": "x", "home": 5456})
        voice.S["last_nu"] = 1000
        step(1000, x=-3000, y=-3000), step(1003, x=-3600, y=-3600), step(1021), step(1025)
        W["settings"].update(quiet=True, quiet_match="9000000003")
        step(1030)
        res.append(("coach: praise when a 'come back' call is obeyed within 8 s (spoken after the 20 s gap); quiet is cleared at the next match",
                    said == [None, None, voice.P["obeyed"], None, None] and W["settings"]["quiet"] is False and W["coach"]["quiet"] is False, str(said)))
    finally:
        data_dir, voice.DRY, voice.GEN, voice.find, vs, s0, rec.event = saved
        voice.S.clear(), voice.S.update(vs), S.clear(), S.update(s0), H.clear()
        world.set_data_dir(data_dir)
    return res
