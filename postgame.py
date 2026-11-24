"""Post-game report + cross-game player profile.
analyze(dirs) -> report dict (one match, possibly split over several session dirs by server restarts); writes report.json/.md
into the last dir and upserts a compact record into <data>/profile/games.jsonl, then rebuilds profile.json.
profile_hints(hero, role) -> <= 400 chars of personal tendencies for LLM/Jev prompts; rule_tuning() -> per-player knobs.
`uv run tools.py postgame [DIR ...]` (default: the latest recorded match)."""
import bisect, gzip, json, math, re, shutil, statistics, time
from collections import Counter
from pathlib import Path
import world
from tools import _jsonl, _meta, _open, _pct  # crash-safe JSONL (or .gz) reader, meta.json, percentile

CONSUMABLE = {"tango", "clarity", "flask", "ward_observer", "ward_sentry", "ward_dispenser", "smoke_of_deceit", "dust", "tpscroll",
              "enchanted_mango", "faerie_fire", "blood_grenade", "tome_of_knowledge", "cheese", "aegis", "refresher_shard", "famango"}
DANGER_RULES = ("R1", "R10", "R11", "R13", "R15")  # "was I warned" before a death (R2/R3 are amber notes now, not warnings)
SCORED_RULES = ("R1", "R2", "R3", "R4", "R10", "R11", "R13", "R15")  # usefulness scored (the precision gate reads it)
LH10 = {1: 45, 2: 50, 3: 35, 4: 12, 5: 8}  # ponytail: rough LH@10 benchmarks per role (normal); OpenDota benchmarks if it matters
FOUNTAIN = world.FOUNTAIN
GOLD = ("gold_from_hero_kills", "gold_from_creep_kills", "gold_from_income", "gold_from_shared", "gold_from_summon_kills")


def matchid_of(d):
    """meta.json, else the <ts>_<matchid> dir name, else the first map.matchid in gsi.jsonl (pre-match dirs)."""
    d = Path(d)
    mid = str(_meta(d).get("matchid") or (d.name.split("_")[1].rstrip("x") if "_" in d.name else ""))
    if not mid or mid == "0":
        try:
            with open(d / "gsi.jsonl", errors="replace") as f:
                for line in f:
                    if (m := re.search(r'"matchid":\s*"(\d+)"', line)) and m.group(1) != "0":
                        return m.group(1)
        except OSError:
            pass
        return None
    return mid


def matches(root):
    """{matchid: [session dirs, oldest first]} for every recording under root."""
    out = {}
    for d in sorted(p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith(".")) if Path(root).is_dir() else []:
        if mid := matchid_of(d):
            out.setdefault(mid, []).append(d)
    return out


def _lane(x, y):
    """Absolute lane (top/mid/bot) of a position; None in jungle/base."""
    if x is None:
        return None
    if x < -5200 or y > 5200:
        return "top"
    if y < -5200 or x > 5200:
        return "bot"
    return "mid" if abs(x - y) < 1600 else None


def _phase(c, turbo=False):
    k = 0.5 if turbo else 1
    return "early" if c < 600 * k else "mid" if c < 1500 * k else "late"


def _rows(dirs, mid):
    """Compact per-payload records from every gsi.jsonl of the match, in order."""
    R, seen = [], set()
    for d in dirs:
        for row in _jsonl(Path(d) / "gsi.jsonl"):
            b = row.get("body") or {}
            m, p, h = world.obj(b, "map"), world.obj(b, "player"), world.obj(b, "hero")
            c = world.num(m.get("clock_time"))
            if c is None or str(m.get("matchid")) != mid or p.get("team_name") not in ("radiant", "dire"):
                continue
            team = p["team_name"]
            off = (world.num(m.get("game_time"), c) or c) - c
            en, al, dead = [], [], 0
            for o in world.obj(b, "minimap").values():
                s = world.short(o.get("unitname")) if isinstance(o, dict) else None
                if not s or s == world.short(h.get("name")):
                    continue
                mine = world.TEAM.get(world.num(o.get("team"))) == team
                if world.num(o.get("visionrange")) == 0:
                    dead += not mine
                else:
                    (al if mine else en).append((s, world.num(o.get("xpos")), world.num(o.get("ypos"))))
            it = world.obj(b, "items")
            items = {n for k in [f"slot{i}" for i in range(9)] + [f"stash{i}" for i in range(6)] if (n := world.item(it.get(k)))}
            ev = []
            for e in b.get("events") if isinstance(b.get("events"), list) else []:
                key = json.dumps(e, sort_keys=True, default=str)
                if isinstance(e, dict) and key not in seen:
                    seen.add(key)
                    gt = world.num(e.get("game_time"))
                    ev.append({**e, "clock": gt - off if gt is not None else c})
            bl = world.obj(world.obj(b, "buildings"), team)
            R.append({"c": c, "t": row.get("t"), "w": world.num(world.obj(b, "provider").get("timestamp")), "dir": Path(d).name,
                      "acct": p.get("accountid"), "team": team, "state": m.get("game_state"), "win": m.get("win_team"),
                      "day": world.flag(m.get("daytime")), "hero": world.short(h.get("name")), "x": world.num(h.get("xpos")),
                      "y": world.num(h.get("ypos")), "alive": world.flag(h.get("alive")) if "alive" in h else None,
                      "lvl": world.num(h.get("level")), "bb": world.num(h.get("buyback_cost")), "bb_cd": world.num(h.get("buyback_cooldown")), "pid": (world.num(p.get("team_slot"), 0) or 0) + (5 if team == "dire" else 0),
                      **{k: world.num(p.get(k)) for k in ("kills", "deaths", "assists", "last_hits", "denies", "gpm", "xpm", "gold",
                                                          "gold_from_income", "gold_from_creep_kills")},
                      "earned": sum(world.num(p.get(k), 0) or 0 for k in GOLD) if any(k in p for k in GOLD) else None,
                      "tp_max": world.num(world.obj(it, "teleport0").get("max_cooldown")) if it else None,
                      "items": items if it else None, "bld": {n: world.num(v.get("health"), 0) for n, v in bl.items() if isinstance(v, dict)} or None,
                      "en": en, "al": al, "en_dead": dead, "ev": ev})
    return R


def _at(R, c):
    """Last record at or before clock c (None if c is before the first)."""
    best = None
    for r in R:
        if r["c"] > c:
            break
        best = r
    return best


def _intervals(evs):
    """Alert episodes from events.jsonl: [{id, rule, level, text, start, end, escalated}] (end = clear or session end)."""
    out, live, last = [], {}, None
    for e in evs:
        c = e.get("clock")
        if not isinstance(c, (int, float)):
            continue
        last = c
        if e.get("kind") != "alert":
            continue
        if e.get("op") == "new":
            if e["id"] in live:  # re-fired without a clear (e.g. server restart): close the old episode
                live[e["id"]]["end"] = c
            live[e["id"]] = {"id": e["id"], "rule": e.get("rule"), "level": e.get("level"), "text": e.get("text"), "start": c, "end": None,
                             "escalated": False}
            out.append(live[e["id"]])
        elif e.get("op") == "escalate" and e.get("id") in live:
            live[e["id"]].update(level=e.get("level"), escalated=True)
        elif e.get("op") == "clear" and e.get("id") in live:
            live.pop(e["id"])["end"] = c
    for a in live.values():
        a["end"] = last
    return out


def _dist_fountain(r):
    return math.dist((r["x"], r["y"]), FOUNTAIN[r["team"]]) if r and r["x"] is not None else None


def analyze(dirs, llm_call=True, write=True):
    """dirs: session dirs of ONE match (any order). Returns the report dict."""
    dirs = sorted(Path(d) for d in dirs)
    mid = next((m for d in dirs if (m := matchid_of(d))), None)
    R = _rows(dirs, mid) if mid else []
    if not R:
        return {"matchid": mid, "error": "no GSI for this match", "dirs": [d.name for d in dirs]}
    last, team, pid = R[-1], R[-1]["team"], R[-1]["pid"]
    metas = [_meta(d) for d in dirs]
    evs, wrows, cvrows, cover = [], [], [], []
    for d in dirs:
        e, w = _jsonl(d / "events.jsonl"), _jsonl(d / "world.jsonl")
        evs += e
        wrows += w
        cvrows += _jsonl(d / "cv.jsonl")
        cl = [c for x in w if isinstance(c := (x.get("game") or {}).get("clock"), (int, float))]
        if cl:
            cover.append((min(cl), max(cl)))  # coach online (recording) for this clock span
    online = lambda c: any(a - 5 <= c <= b + 5 for a, b in cover)
    win = next((r["win"] for r in reversed(R) if r["win"] not in (None, "none", "")), None)
    result = "unknown" if not win else "win" if win == team else "loss"
    dur = last["c"]

    # mode / role (GSI has neither). Mode = what the live game used: the player's manual toggle for this match, else the same test
    # as world._auto_detect at 8:00 (the old passive-income test called every recorded Turbo game "normal": Turbo doesn't raise it)
    man = next((bool(s.get("turbo")) for m in reversed(metas) if (s := m.get("settings") or {}).get("turbo_match") in (mid, "always")), None)
    r8 = _at(R, 480)
    tp_max = next((r["tp_max"] for r in R if r.get("tp_max")), None)
    guess = world.turbo_guess(r8["last_hits"], r8["gold_from_creep_kills"], r8["xpm"], r8["c"], tp_max) if r8 else None
    turbo = man if man is not None else guess if guess is not None else any(m.get("mode") == "turbo" for m in metas)
    mode = "turbo" if turbo else "normal"
    roles = [(me.get("role"), me.get("role_src")) for x in wrows if (me := x.get("me") or {}).get("role")]
    manual = [r for r, s in roles if s == "manual"]
    lane_c = Counter(world.zone(r["x"], r["y"], team) for r in R if r["x"] is not None and 60 <= r["c"] <= (240 if turbo else 480))
    lane = max(("safe_lane", "mid", "off_lane"), key=lambda k: lane_c.get(k, 0)) if lane_c else None

    def at_min(m, k):
        r = _at(R, m * 60 * (0.5 if turbo else 1))
        return r and r[k]
    lh10 = at_min(10, "last_hits")
    lhm = (lh10 or 0) / 10
    core = lhm >= 2.5 or any(i not in world.SUPPORT_ITEMS and i in ("black_king_bar", "manta", "battle_fury", "radiance", "desolator")
                             for i in (_at(R, 600) or {}).get("items") or ())
    role = manual[-1] if manual else {"mid": 2, "safe_lane": 1 if core else 5, "off_lane": 3 if core else 4}.get(lane) or \
        (roles[-1][0] if roles else None)

    # curves, levels, items
    curve, lv, items, have = [], {}, [], set()
    for m in range(0, int(dur // 60) + 1):
        if (r := _at(R, m * 60)) and r["c"] > m * 60 - 60:
            curve.append({"min": m, "earned": r["earned"], "gold": r["gold"], "lvl": r["lvl"], "lh": r["last_hits"]})
    for r in R:
        for k in (6, 12, 18, 25):
            if (r["lvl"] or 0) >= k and k not in lv:
                lv[k] = r["c"]
        if r["items"] is not None:
            for n in sorted(r["items"] - have):
                if n not in CONSUMABLE and not n.startswith("recipe"):
                    items.append({"item": n, "clock": r["c"]})
            have |= r["items"]

    # GSI events: kills, roshan, buildings
    kills, rosh = [], []
    for r in R:
        for e in r["ev"]:
            t = e.get("event_type")
            if t == "generic_event":
                try:
                    g = json.loads(e.get("data") or "{}")
                except ValueError:
                    continue
                if g.get("type") == "CHAT_MESSAGE_HERO_KILL":
                    kills.append({"clock": e["clock"], "victim": g.get("playerid1"), "killer": g.get("playerid2")})
            elif t in ("roshan_killed", "aegis_picked_up", "aegis_denied", "aegis_stolen"):
                rosh.append({"clock": round(e["clock"]), "event": t, **{k: e[k] for k in ("killed_by_team", "player_id") if k in e}})
    mine = range(5, 10) if team == "dire" else range(5)
    ally_deaths = [k["clock"] for k in kills if k["victim"] in mine and k["victim"] != pid]
    alerts = _intervals(evs)
    blost, prev = [], None
    for r in R:
        if r["bld"] is None:
            continue
        up = {n for n, hp in r["bld"].items() if hp > 0}
        for n in sorted((prev or set()) - up):
            b = {"name": n, "clock": r["c"], "w": r["w"], "my_zone": world.zone(r["x"], r["y"], team), "my_lane": _lane(r["x"], r["y"]),
                 "lane": next((l for l in ("top", "mid", "bot") if l in n), "base"), "alive": r["alive"]}
            b["warned"] = any(a["rule"] == "R10" and a["start"] <= r["c"] + 2 and a["end"] >= r["c"] - 30 for a in alerts) \
                if online(r["c"]) else None
            blost.append(b)
        prev = up

    jresp = [e for e in evs if e.get("kind") == "jev_resp" and e.get("ok") and isinstance(e.get("clock"), (int, float))]

    # deaths
    deaths, pd = [], None
    for i, r in enumerate(R):
        if r["deaths"] is not None and pd is not None and r["deaths"] > pd:
            before = [x for x in R[max(0, i - 8):i] if x["c"] >= r["c"] - 5 and x["alive"] is not False and x["x"] is not None] or [R[max(0, i - 1)]]
            b = before[-1]
            near = sorted({s for x in before for s, ex, ey in x["en"] if ex is not None and b["x"] is not None
                           and math.dist((ex, ey), (b["x"], b["y"])) <= 1500})
            seen = {s for x in before for s, _, _ in x["en"]}
            ally_d = min((math.dist((ax, ay), (b["x"], b["y"])) for _, ax, ay in b["al"] if ax is not None and b["x"] is not None), default=None)
            c = r["c"]
            w = [a for a in alerts if a["rule"] in DANGER_RULES and a["level"] != "info" and a["start"] <= c and a["end"] >= c - 20]
            jw = [j for j in jresp if c - 20 <= j["clock"] <= c and (((j.get("decision") or {}).get("danger") or {}).get("score", 0) >= 3
                                                                 or ((j.get("decision") or {}).get("action") or {}).get("choice") in ("retreat", "heal_base"))]
            why = [f"{a['rule']} {a['text']}" for a in w] + (["jev " + ("danger %.1f" % jw[-1]["decision"]["danger"]["score"]
                                                                   if jw[-1]["decision"]["danger"]["score"] >= 3 else "BACK")] if jw else [])
            gap = c - R[i - 1]["c"] > 10
            db = next((e for e in evs if e.get("kind") == "debrief" and abs((e.get("at") or -1e9) - c) <= 3), {})  # coach.py's live verdict
            deaths.append({"clock": c, "w": r["w"], "zone": world.zone(b["x"], b["y"], team), "lane": _lane(b["x"], b["y"]), "phase": _phase(c, turbo),
                           "cause": db.get("cause"), "called": db.get("called"),
                           "night": not b["day"], "near": near, "buyback_ok": (b["gold"] or 0) >= (b["bb"] or 1e9) and not b["bb_cd"], "visible": len(seen), "missing": max(0, 5 - len(seen) - b["en_dead"]),
                           "ally_dist": round(ally_d) if ally_d is not None else None,
                           "warned": (bool(why) if online(c) else None), "warning": "; ".join(dict.fromkeys(why))[:160] or None,
                           "killers": [k["killer"] for k in kills if k["victim"] == pid and abs(k["clock"] - c) <= 6], "gap": gap})
        pd = r["deaths"] if r["deaths"] is not None else pd

    # scorecard
    fb = [e for e in evs if e.get("kind") == "feedback"]
    thumbs = {}
    for e in fb:
        ref = str(e.get("ref") or ((e.get("alerts") or [{}])[0].get("id")) or "")
        rule = ref.split(":")[0] if ref else None
        if rule and e.get("fb") in ("good", "bad"):
            thumbs.setdefault(rule, Counter())["up" if e["fb"] == "good" else "down"] += 1
    rules_out = {}
    my_kda = [(r["c"], (r["kills"] or 0) + (r["deaths"] or 0) + (r["assists"] or 0)) for r in R]

    def kda_change(a, b):
        v = [k for c, k in my_kda if a <= c <= b]
        return bool(v) and max(v) > min(v)
    ep_prev = {}
    for a in alerts:
        rule = a["rule"]
        s = rules_out.setdefault(rule, {"fired": 0, "episodes": 0, "useful": 0, "false": 0, "unclear": 0, "responded": 0, "ignored": 0,
                                        "up": 0, "down": 0, "examples": []})
        s["fired"] += 1
        if rule not in SCORED_RULES:
            continue
        flap, ep_prev[rule] = a["start"] - ep_prev.get(rule, -1e9) < 15, a["start"]
        if flap:  # re-fires within 15 s of the previous one = the same episode
            continue
        s["episodes"] += 1
        c, win_s = a["start"], 90 if rule == "R4" else 30
        died = any(c <= d["clock"] <= c + win_s for d in deaths)
        hit = died or kda_change(c, c + win_s) or any(c <= x <= c + win_s for x in ally_deaths) \
            or any(c <= b["clock"] <= c + win_s for b in blost) \
            or (rule == "R4" and any(c <= x["clock"] <= c + win_s and x["event"] == "roshan_killed" and x.get("killed_by_team") != team for x in rosh))
        r0, r1 = _at(R, c), _at(R, c + 10)
        d0, d1 = _dist_fountain(r0), _dist_fountain(r1)
        resp = None if rule == "R4" or d0 is None or d1 is None else (d0 - d1 >= 600 or d1 < 2500) and not (r1 and r1["alive"] is False)
        s["responded" if resp else "ignored"] += resp is not None
        contra = False
        if rule == "R3" and (m := re.search(r"([a-z_]+(?:\+[a-z_]+)*) vanished", a["text"] or "")):
            names = set(m.group(1).split("+"))
            contra = any(n in {e[0] for e in r["en"]} for r in R if c < r["c"] <= c + 10 for n in names)
        verdict = "useful" if hit and not contra else "false" if contra or (not hit and not resp) else "unclear"
        s[verdict] += 1
        if len(s["examples"]) < 6:
            s["examples"].append({"clock": round(c), "text": a["text"], "verdict": verdict, "responded": resp, "died": died,
                                  "contradicted": contra or None})
    for rule, t in thumbs.items():
        rules_out.setdefault(rule, {"fired": 0, "episodes": 0, "useful": 0, "false": 0, "unclear": 0, "responded": 0, "ignored": 0,
                                    "up": 0, "down": 0, "examples": []}).update(up=t["up"], down=t["down"])
    misses = [d for d in deaths if d["warned"] is False]
    miss_kinds = Counter(f"{d['zone']} {d['phase']} {'night' if d['night'] else 'day'}" + (", 3+ missing" if d["missing"] >= 3 else "")
                         + (f", {len(d['near'])} visible near" if d["near"] else ", no enemy visible") for d in misses)

    def calls(name):
        req = [e for e in evs if e.get("kind") == f"{name}_req"]
        resp = [e for e in evs if e.get("kind") == f"{name}_resp"]
        lat = [e["ms"] for e in resp if isinstance(e.get("ms"), (int, float))]
        return {"calls": len(req), "ok": sum(1 for e in resp if e.get("ok")), "late": sum(1 for e in resp if e.get("late")),
                "p50_ms": _pct(lat, .5), "p95_ms": _pct(lat, .95), "usd": round(sum(e.get("usd") or 0 for e in resp), 5)}
    lock = next((x for x in cvrows if (x.get("lineup") or {}).get("locked")), None)
    errs = [x for w in wrows if (x := (w.get("cv") or {}).get("gsi_vs_cv_err_u")) is not None]
    cvgaps = [b["w"] - a["w"] for a, b in zip(cvrows, cvrows[1:]) if b.get("w") and a.get("w") and b["w"] - a["w"] > 2]
    gsigaps = [{"from": a["c"], "to": b["c"]} for a, b in zip(R, R[1:]) if b["c"] - a["c"] > 10]
    late_s = 600 if turbo else 1200
    late = [r for m in range(int(late_s // 60), int(dur // 60) + 1) if (r := _at(R, m * 60)) and r["alive"] and r["x"] is not None]
    alone = [r for r in late if world.zone(r["x"], r["y"], team) != "base"
             and min((math.dist((ax, ay), (r["x"], r["y"])) for _, ax, ay in r["al"] if ax is not None), default=1e9) > 3000]

    out = dirs[-1]
    old = _load(out / "report.json")
    src = metas[-1].get("compacted_from") or [d.name for d in dirs]  # a compacted match is one dir
    rep = {"matchid": mid, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "dirs": src,
           "date": str(next((m["start_iso"] for m in metas if m.get("start_iso")), time.strftime("%Y-%m-%d")))[:10], "account_id": last["acct"], "result": result,
           "win_team": win, "team": team, "hero": last["hero"], "role": role, "lane": lane, "mode": mode, "duration": dur,
           "kda": [last["kills"], last["deaths"], last["assists"]], "lh": last["last_hits"], "dn": last["denies"], "gpm": last["gpm"],
           "xpm": last["xpm"], "level": last["lvl"], "lh5": at_min(5, "last_hits"), "lh10": lh10, "levels": lv, "curve": curve, "items": items,
           "deaths": deaths, "buildings_lost": blost, "roshan": rosh, "alone_late_min": len(alone), "late_min": len(late),
           "scorecard": {"rules": rules_out, "misses": [{"clock": d["clock"], "zone": d["zone"], "missing": d["missing"], "near": d["near"]}
                                                        for d in misses],
                         "top_missed": [f"{n}x {k}" for k, n in miss_kinds.most_common(4)],
                         "unwarned_buildings": sum(1 for b in blost if b["warned"] is False),
                         "offline_deaths": sum(1 for d in deaths if d["warned"] is None),
                         "jev": calls("jev"), "llm": calls("llm"),
                         "talk_usd": round(sum(e.get("usd") or 0 for e in evs if e.get("kind") in ("voice_cmd", "voice")), 5),
                         "cv": {"lineup_lock": lock.get("clock") if lock else None, "gsi_vs_cv_err_u": statistics.median(errs) if errs else None,
                                "capture_gaps": len(cvgaps), "capture_gap_s": round(sum(cvgaps)), "frames": len(cvrows)},
                         "restarts": max(0, len(src) - 1), "gsi_gaps": gsigaps,
                         "crashes": sum(1 for e in evs if e.get("kind") == "log" and "crash" in str(e.get("msg")))},
           "feedback": [{"clock": e.get("clock"), "fb": e.get("fb"), "text": e.get("text"), "ref": e.get("ref")} for e in fb]}
    rep["highlights"], rep["lessons"] = _insights(rep)
    import coach
    tgt, why = coach.target(mid)
    nd = last["deaths"] if last["deaths"] is not None else len(deaths)
    rep["goal"] = {"text": coach.text(tgt), "target": tgt, "now": nd, "met": nd <= tgt if win or nd > tgt else None, "why": why}
    rep["one_lesson"], rep["one_praise"], rep["coach_misses"] = _one(rep, evs)
    if not cvrows and old.get("scorecard"):
        rep["scorecard"]["cv"], rep["storage"] = old["scorecard"].get("cv"), old.get("storage")  # compact() dropped cv.jsonl
    if old.get("matchid") == mid and old.get("debrief"):
        rep.update(debrief=old["debrief"], goals=old.get("goals"), llm_model=old.get("llm_model"))  # cached: one LLM call per match
    elif llm_call:
        rep.update(_debrief(rep))
    if write and dur >= 600:  # games shorter than 10 min (aborted / still running) don't go into the profile
        record(rep)
    rep["next_goal"] = coach.text(coach.target()[0])  # from the profile, this game included
    if write:
        (out / "report.json").write_text(json.dumps(rep, indent=1, default=str))
        (out / "report.md").write_text(to_md(rep))
    rep["path"] = str(out / "report.md")
    return rep


LESSON = {"warned": "came after I called it and you stayed. Leave on the call.", "alone": "had no teammate within reach. Stay near a teammate.",
          "first_in": "came as the first one into the fight. Arrive second.", "outnumbered": "came defending a tower against more of them. Let it go.",
          "stayed_low": "came in lane after staying under half health. Go heal at half health."}


def _n(k, cap=True):
    import voice
    w = voice.NUM[k] if 0 <= k < len(voice.NUM) else str(k)
    return w if cap else w.lower()


def _one(r, evs):
    """(one lesson, one thing done well, the coach's own misses): the top of the report, in plain sentences without rule ids.
    Uses the live debrief verdicts when the match has them; older recordings fall back to the rule-based lessons."""
    D, n, g = r["deaths"], len(r["deaths"]), r["goal"]
    k, d, a = (x or 0 for x in r["kda"])
    live = [x for x in D if x.get("cause")]
    bad = Counter(x["cause"] for x in live if x["cause"] in LESSON).most_common(1)
    if bad and bad[0][1] >= 2:
        lesson = f"{_n(bad[0][1])} of your {n} deaths {LESSON[bad[0][0]]}"
    elif bad:
        lesson = f"One death {LESSON[bad[0][0]]}"
    else:
        lesson = r["lessons"][0] if r["lessons"] else "No pattern in your deaths this game. Keep playing the same way."
    trades = sum(x["cause"] == "good_trade" for x in live)
    praised = sum(1 for e in evs if e.get("kind") == "voice" and e.get("op") == "hit" and e.get("tier") == "P" and e.get("cue") in ("got_out", "obeyed"))
    if g["met"]:
        praise = f"You kept it to {d} death{'s' if d != 1 else ''}: goal met."
    elif praised:
        praise = "You left on the call once and lived." if praised == 1 else f"You left on the call {_n(praised, False)} times and lived each time."
    elif trades:
        praise = f"{_n(trades)} of your deaths {'were good trades' if trades > 1 else 'was a good trade'}: an enemy went down with you."
    elif a >= 8:
        praise = f"{a} assists: you were in most of your team's kills."
    else:
        praise = r["highlights"][1] if len(r["highlights"]) > 1 else f"You finished {k}/{d}/{a}."
    if live:  # late = under 5 s: too short to act on
        none, late = sum(not x["called"] for x in live), sum(bool(x["called"]) and x["called"]["s_before"] < 5 for x in live)
        miss = ("I called your one death at least 5 seconds ahead." if len(live) == 1 and not none + late else
                f"I called every one of your {len(live)} deaths at least 5 seconds ahead." if not none + late else
                " and ".join(p for p in (none and f"{_n(none, False)} of your {len(live)} deaths had no call from me",
                                         late and f"{_n(late, False)} had a call less than 5 seconds ahead") if p).capitalize() + ". Those are on me.")
    else:
        none = sum(x["warned"] is False for x in D)
        miss = f"{_n(none)} of your {n} deaths had no warning from me." if none else "I warned before every death I saw." if n else "No deaths to call."
    return lesson, praise, miss


def _load(p):
    try:
        v = json.loads(Path(p).read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _insights(r):
    """Rule-based highlights (what happened) and lessons (what to change). 3-5 each."""
    mm, D, H, L = world.mmss, r["deaths"], [], []
    k, d, a = (x or 0 for x in r["kda"])
    H.append(f"{r['result'].upper()} as {r['hero']} ({r['mode']}, role {r['role'] or '?'}) in {mm(r['duration'])}: {k}/{d}/{a}, "
             f"{r['gpm']} GPM / {r['xpm']} XPM, {r['lh']} LH")
    if a >= 2 * max(k, 1) and a >= 8:
        H.append(f"{a} assists: you were in most of your team's kills")
    lost = r["buildings_lost"]
    if lost:
        t3 = [b for b in lost if any(s in b["name"] for s in ("tower3", "rax", "tower4", "fort"))]
        H.append(f"{len(lost)} buildings lost" + (f"; base (T3/rax/T4) fell {mm(t3[0]['clock'])}-{mm(t3[-1]['clock'])}" if t3 else ""))
        away = [b for b in t3 if b["alive"] and b["my_lane"] and b["lane"] != "base" and b["my_lane"] != b["lane"]]
        if len(away) >= 2:
            L.append(f"{len(away)} of {len(t3)} base buildings fell while you were in another lane: once T3s are hit, stay home and defend")
        dl = [b for b in t3 if b["alive"] is False]
        if len(dl) >= 2:
            dd = [x for x in D if x["clock"] <= dl[0]["clock"]][-1:]
            L.append(f"{len(dl)} of {len(t3)} base buildings fell while you were dead" + (f" (died {mm(dd[0]['clock'])} in {dd[0]['zone'].replace('_', ' ')}"
                     + (", buyback was affordable" if dd[0]["buyback_ok"] else "") + ")" if dd else "") + ": don't die when they are sieging high ground")
    ok = [x for x in r["roshan"] if x["event"] == "roshan_killed"]
    if ok:
        H.append("Roshan: " + ", ".join(f"{mm(x['clock'])} by {x.get('killed_by_team')}" for x in ok))
    if D:
        miss3 = [x for x in D if x["missing"] >= 3]
        night = [x for x in D if x["night"]]
        zc = Counter(x["zone"] for x in D).most_common(1)[0]
        if len(miss3) >= max(2, len(D) / 3):
            L.append(f"{len(miss3)} of {len(D)} deaths came with 3+ enemies missing from the map: when 3 are missing, stand near tower/allies")
        if zc[1] >= max(2, len(D) / 3):
            L.append(f"{zc[1]} of {len(D)} deaths in {zc[0].replace('_', ' ')}" + (f" ({sum(1 for x in D if x['zone'] == zc[0] and x['night'])} at night)"
                                                                                  if night else "") + ": go there with wards or allies only")
        alone = [x for x in D if (x["ally_dist"] or 1e9) > 2500]
        if len(alone) >= max(2, len(D) / 3):
            L.append(f"{len(alone)} deaths with no ally within 2500: as {'a support' if (r['role'] or 0) >= 4 else 'a core'}, move with the team")
        ign = [x for x in D if x["warned"]]
        if ign:
            L.append(f"{len(ign)} deaths came within 20 s of a coach warning ({', '.join(mm(x['clock']) for x in ign[:4])}): leave on the call")
    bm = LH10.get(r["role"] or 0)
    if bm and r["lh10"] is not None:
        if r["lh10"] < 0.6 * bm:
            L.append(f"LH@10 = {r['lh10']} vs ~{bm} benchmark for role {r['role']}")
        elif r["lh10"] >= bm:
            H.append(f"LH@10 = {r['lh10']} (benchmark ~{bm})")
    if r["late_min"] >= 3 and r["alone_late_min"] >= r["late_min"] / 2:
        L.append(f"{r['alone_late_min']} of {r['late_min']} late-game minutes alone (>3000 from allies, outside base): group up")
    lv = r["levels"]
    if 6 in lv:
        H.append("Level 6 at " + mm(lv[6]) + (f", 12 at {mm(lv[12])}" if 12 in lv else "") + (f", 18 at {mm(lv[18])}" if 18 in lv else ""))
    if len(L) < 3 and D:
        L.append(f"{len(D)} deaths: aim for {max(len(D) // 2, 2)} or fewer next game (buy/hold a TP, check the minimap every few seconds)")
    return H[:5], L[:5]


DEBRIEF = {"type": "object", "additionalProperties": False, "required": ["debrief", "goals"],
           "properties": {"debrief": {"type": "array", "items": {"type": "string"}}, "goals": {"type": "array", "items": {"type": "string"}}}}


def _debrief(rep):
    """One cheap LLM call: 5-bullet debrief + 3 goals. {} when the LLM is off/unconfigured or the call fails."""
    import llm
    if llm.DISABLED or not llm.TARGETS:
        return {}
    facts = {k: rep[k] for k in ("result", "hero", "role", "mode", "duration", "kda", "lh", "dn", "gpm", "xpm", "lh5", "lh10", "levels",
                                  "alone_late_min", "late_min", "highlights", "lessons")}
    facts["deaths"] = [{k: d[k] for k in ("clock", "zone", "night", "near", "missing", "ally_dist", "warned")} for d in rep["deaths"]]
    facts["items"] = [f"{i['item']}@{world.mmss(i['clock'])}" for i in rep["items"]][:20]
    facts["buildings_lost"] = [f"{b['name']}@{world.mmss(b['clock'])} (me: {b['my_zone']})" for b in rep["buildings_lost"]]
    system = ("You are a Dota 2 coach writing a post-game debrief for one player from the stats below (clock in seconds). "
              "Output JSON: debrief = exactly 5 bullets (<= 140 chars each, concrete, cite numbers/times), "
              "goals = exactly 3 measurable goals for the next game (<= 90 chars each). No chat/ping advice.")
    for t in llm.TARGETS:
        try:
            out, usd, _ = llm._post(t, system, json.dumps(facts, default=str), max_tokens=3000, schema=DEBRIEF, timeout=30)
            if not llm.validate(out, DEBRIEF):
                return {"debrief": out["debrief"][:5], "goals": out["goals"][:3], "llm_model": t["model"], "llm_usd": round(usd, 5)}
        except Exception as e:  # ponytail: same blind failover as llm.ask
            world.log.warning("postgame llm %s: %s", t["model"], llm.redact(repr(e))[:200])
    return {}


def to_md(r):
    """report.md: the goal, one lesson, one thing done well, the coach's misses and the next goal first, in plain words;
    everything with rule ids and dev numbers is under "Details" at the bottom."""
    mm, s = world.mmss, r["scorecard"]
    k, d, a = r["kda"]
    g = r.get("goal") or {}
    L = [f"# Post-game: {r['hero']} - {r['result'].upper()} ({mm(r['duration'])}, {r['mode']})", ""]
    if g:
        L += [f"**Goal: {g['text']}: {'met' if g['met'] else 'missed' if g['met'] is False else 'open'} ({g['now']} death{'s' if g['now'] != 1 else ''}).**", ""]
    L += [f"- One lesson: {r.get('one_lesson') or '-'}", f"- Done well: {r.get('one_praise') or '-'}", f"- The coach's misses: {r.get('coach_misses') or '-'}",
          f"- Next game's goal: {r.get('next_goal') or '-'}", "",
          f"KDA {k}/{d}/{a}, LH/DN {r['lh']}/{r['dn']}, GPM {r['gpm']}, XPM {r['xpm']}, level {r['level']}.", ""]
    if r.get("debrief"):
        L += ["## Coach debrief", *[f"- {x}" for x in r["debrief"]], ""]
    L += [f"## Deaths ({len(r['deaths'])})", "| clock | zone | day/night | enemies near (1500) | missing | nearest ally | why | my call |", "|---|---|---|---|---|---|---|---|"]
    for x in r["deaths"]:
        c = x.get("called")
        call = f"{c['word']} {c['s_before']} s before" if c else "none" if x.get("cause") else "offline" if x["warned"] is None else \
            "warned" if x["warned"] else "**none**"
        L.append(f"| {mm(x['clock'])}{' (gap)' if x['gap'] else ''} | {x['zone']} | {'night' if x['night'] else 'day'} | {', '.join(x['near']) or '-'} | "
                 f"{x['missing']} | {x['ally_dist'] if x['ally_dist'] is not None else '-'} | {(x.get('cause') or '-').replace('_', ' ')} | {call} |")
    L += ["", "## Highlights", *[f"- {x}" for x in r["highlights"]], "", f"## Buildings lost ({len(r['buildings_lost'])})"]
    L += [f"- {mm(b['clock'])} {b['name']} (you: {b['my_zone']}{'' if b['alive'] else ', dead'}; "
          f"{'offline' if b['warned'] is None else 'warned' if b['warned'] else 'no warning'})" for b in r["buildings_lost"]]
    L += ["", "## Items", "- " + (", ".join(f"{i['item']} {mm(i['clock'])}" for i in r["items"]) or "-")]
    if r["roshan"]:
        L += ["", "## Roshan", *[f"- {mm(x['clock'])} {x['event']} {x.get('killed_by_team') or ''}" for x in r["roshan"]]]
    L += ["", "## Details (for development)",
          f"- match {r['matchid']}, {r['team']}, {r['mode']}, role {r['role'] or '?'} ({r['lane'] or '?'}); sessions: {', '.join(r['dirs'])}",
          f"- LH@5 {r['lh5']}, LH@10 {r['lh10']}; levels: " + (", ".join(f"{n} @ {mm(c)}" for n, c in sorted(r["levels"].items(), key=lambda x: int(x[0]))) or "-"),
          "- earned gold/min: " + " ".join(f"{c['min']}:{c['earned']}" for c in r["curve"] if c["min"] % 5 == 0 and c["earned"] is not None),
          f"- goal: {g.get('text')} ({g.get('why')})", "- all lessons: " + (" | ".join(r["lessons"]) or "-"),
          "- goals from the debrief call: " + (" | ".join(r.get("goals") or []) or "-"), "",
          "### Coach scorecard", "| rule | fired | episodes | useful | likely false | unclear | responded | ignored | 👍/👎 |", "|---|---|---|---|---|---|---|---|---|"]
    for rule, x in sorted(s["rules"].items()):
        if rule != "T":
            L.append(f"| {rule} | {x['fired']} | {x['episodes']} | {x['useful']} | {x['false']} | {x['unclear']} | {x['responded']} | {x['ignored']} | {x['up']}/{x['down']} |")
    L += [f"- timer reminders (T): {s['rules'].get('T', {}).get('fired', 0)}",
          "- alerts before each death: " + ("; ".join(f"{mm(x['clock'])} {x['warning']}" for x in r["deaths"] if x.get("warning")) or "-"),
          f"- deaths with NO warning (coach online): {len(s['misses'])}" + (": " + "; ".join(s["top_missed"]) if s["top_missed"] else ""),
          f"- deaths while the coach was offline: {s['offline_deaths']}; buildings lost with no DEFEND warning: {s['unwarned_buildings']}",
          f"- Jev: {s['jev']['calls']} calls, p50 {s['jev']['p50_ms']} ms, ${s['jev']['usd']:.4f}; "
          f"LLM: {s['llm']['calls']} calls ({s['llm']['late']} late), p50 {s['llm']['p50_ms']} ms, ${s['llm']['usd']:.4f}",
          f"- CV: lineup locked {mm(s['cv']['lineup_lock']) if s['cv']['lineup_lock'] is not None else 'never'}, gsi_vs_cv_err_u {s['cv']['gsi_vs_cv_err_u']}, "
          f"{s['cv']['capture_gaps']} capture gaps ({s['cv']['capture_gap_s']} s), {s['cv']['frames']} frames",
          f"- server restarts: {s['restarts']}, GSI blind spots: " + (", ".join(f"{mm(g_['from'])}-{mm(g_['to'])}" for g_ in s["gsi_gaps"]) or "none")
          + f", vision crashes logged: {s['crashes']}"]
    if st := r.get("storage"):
        L += [f"- storage: compacted {st['at']}: {st['before_mb']} MB -> {st['after_mb']} MB ({st['sessions']} sessions merged, "
              f"{st['frames_kept']} frames kept, {st['frames_deleted']} deleted, cv.jsonl dropped, world.jsonl at 0.2 Hz)"]
    ex = [(rule, e) for rule, x in sorted(s["rules"].items()) for e in x["examples"] if e["verdict"] != "useful"][:8]
    if ex:
        L += ["", "Likely-false / unclear examples:"] + [f"- {mm(e['clock'])} {rule} {e['text']} -> {e['verdict']}"
                                                         f"{' (heroes re-seen within 10 s)' if e.get('contradicted') else ''}" for rule, e in ex]
    return "\n".join(L) + "\n"


def summary(r):
    """Small W["postgame"] for the dashboard."""
    if r.get("error"):
        return {"matchid": r.get("matchid"), "error": r["error"]}
    s = r["scorecard"]
    return {"matchid": r["matchid"], "result": r["result"], "win_team": r["win_team"], "team": r["team"], "hero": r["hero"], "role": r["role"], "mode": r["mode"], "duration": r["duration"],
            "kda": r["kda"], "gpm": r["gpm"], "xpm": r["xpm"], "lh": r["lh"], "dn": r["dn"], "lh10": r["lh10"],
            "deaths": [{"clock": d["clock"], "zone": d["zone"], "warned": d["warned"], "cause": d.get("cause"), "called": d.get("called")}
                       for d in r["deaths"]],
            "buildings_lost": len(r["buildings_lost"]),
            "scorecard": {"alerts": sum(x["episodes"] for x in s["rules"].values()), "useful": sum(x["useful"] for x in s["rules"].values()),
                          "false": sum(x["false"] for x in s["rules"].values()), "missed_deaths": len(s["misses"]),
                          "jev_calls": s["jev"]["calls"], "llm_calls": s["llm"]["calls"], "usd": round(s["jev"]["usd"] + s["llm"]["usd"] + s.get("talk_usd", 0) + (r.get("review_usd") or 0), 4)},
            "highlights": r["highlights"], "lessons": r["lessons"], "debrief": r.get("debrief"), "goals": r.get("goals"), "path": r.get("path"),
            "goal": r.get("goal") and {k: r["goal"][k] for k in ("text", "met", "now", "target")}, "one_lesson": r.get("one_lesson"),
            "one_praise": r.get("one_praise"), "coach_misses": r.get("coach_misses"), "next_goal": r.get("next_goal")}


# ---- compaction ---------------------------------------------------------------------------------------------------
KEEP_S, MM_EVERY, WORLD_EVERY, LIVE_S = 10, 30, 5, 120


def _size(d):
    return sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file())


def live(d):
    """Being written: the newest session dir under its root, or anything in it (but our report.*) modified in the last 2 minutes."""
    d = Path(d)
    newest = max((p.name for p in d.parent.iterdir() if p.is_dir() and not p.name.startswith(".")), default=None)
    return d.name == newest or any(time.time() - f.stat().st_mtime < LIVE_S for f in d.rglob("*") if f.is_file() and f.stem != "report")


def compact(dirs, dry=False):
    """After analyze(): merge one match's session dirs into <root>/<first-ts>_<matchid>/ with meta.json, report.*, gsi/events.jsonl.gz
    (gsi "t" kept monotonic for replay), world.jsonl.gz at 0.2 Hz, no cv.jsonl (its summary is in report.json), and only full_*.jpg
    within +-10 s of deaths / high alerts / feedback marks / building losses plus one mm_ crop per 30 s.
    Refuses live sessions and matches without a good report.json + games.jsonl entry. dry=True only reports what it would do."""
    dirs = sorted(Path(d) for d in dirs)
    mid, rep = matchid_of(dirs[-1]), _load(dirs[-1] / "report.json")
    root = dirs[0].parent
    target = root / f"{dirs[0].name.split('_')[0]}_{mid}"
    ok = rep.get("matchid") == mid and not rep.get("error") and mid in {g.get("matchid") for g in _jsonl(_pdir() / "games.jsonl")}
    if any(live(d) for d in dirs):
        return {"skip": "live session (newest dir or written < 2 min ago)"}
    if rep.get("storage") and dirs == [target]:
        return {"skip": "already compacted"}
    if not ok and not dry:
        return {"skip": "not analyzed (no report.json / games.jsonl entry): run tools.py postgame first"}
    if target.exists() and target not in dirs:
        return {"skip": f"{target.name} exists and is not part of this match"}
    evs = [e for d in dirs for e in _jsonl(d / "events.jsonl")]
    t = sorted([x["w"] for x in rep.get("deaths", []) + rep.get("buildings_lost", []) if x.get("w")] +
               [e["w"] for e in evs if e.get("w") and (e.get("kind") == "feedback" or (e.get("kind") == "alert" and e.get("level") == "high"))])
    keep, drop, mm = [], [], set()
    for d in dirs:
        for f in sorted((d / "frames").glob("*")) if (d / "frames").is_dir() else []:
            kind, _, ms = f.stem.partition("_")
            w = int(ms) / 1000 if ms.isdigit() else None
            if not ms and re.fullmatch(r"\d+\.\d+", kind):  # old recorder: full frames named <epoch s>.png
                kind, w = "full", float(kind)
            i = bisect.bisect_left(t, (w or 0) - KEEP_S)
            hit = w is not None and kind == "full" and i < len(t) and t[i] <= w + KEEP_S
            if w is not None and kind == "mm" and int(w // MM_EVERY) not in mm:
                mm.add(int(w // MM_EVERY))
                hit = True
            (keep if hit else drop).append(f)
    before = sum(_size(d) for d in dirs)
    info = {"target": target.name, "sessions": len(dirs), "before_mb": round(before / 1e6, 1), "frames_kept": len(keep),
            "frames_deleted": len(drop), "frames_deleted_mb": round(sum(f.stat().st_size for f in drop) / 1e6, 1),
            "cv_jsonl_mb": round(sum((d / "cv.jsonl").stat().st_size for d in dirs if (d / "cv.jsonl").exists()) / 1e6, 1), "analyzed": ok}
    if dry:
        return info
    tmp = root / f".{target.name}.tmp"  # built completely before any source is deleted; matches() skips dot dirs
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "frames").mkdir(parents=True)
    n_in = n_out = 0
    with gzip.open(tmp / "gsi.jsonl.gz", "wt") as g:
        base, last = 0.0, None
        for d in dirs:
            rows = _jsonl(d / "gsi.jsonl")
            n_in += len(rows)
            if rows and last is not None and (rows[0].get("t") or 0) + base <= last:
                base = last + 1 - (rows[0].get("t") or 0)  # next session restarts t at 0
            for r in rows:
                r["t"] = last = round((r.get("t") or 0) + base, 3)
                g.write(json.dumps(r, separators=(",", ":")) + "\n")
                n_out += 1
    with gzip.open(tmp / "events.jsonl.gz", "wt") as g:
        g.writelines(json.dumps(e, separators=(",", ":")) + "\n" for e in evs)
    with gzip.open(tmp / "world.jsonl.gz", "wt") as g:
        lw = -1e18
        for d in dirs:
            for x in _jsonl(d / "world.jsonl"):
                if (x.get("w") or 0) - lw >= WORLD_EVERY:
                    lw = x.get("w") or 0
                    g.write(json.dumps(x, separators=(",", ":")) + "\n")
    for f in keep:
        shutil.copy2(f, tmp / "frames" / f.name)
    metas = [_meta(d) for d in dirs]
    m = next((x for x in reversed(metas) if x), {})
    first = next((x for x in metas if x.get("start")), m)
    m |= {"dir": target.name, "matchid": mid, "start": first.get("start"), "start_iso": first.get("start_iso"), "compacted_from": [d.name for d in dirs],
          "sessions": [{"dir": d.name, **{k: x.get(k) for k in ("start_iso", "end", "counts", "app")}} for d, x in zip(dirs, metas)]}
    (tmp / "meta.json").write_text(json.dumps(m, indent=1, default=str))
    if n_in != n_out or len(_jsonl(tmp / "gsi.jsonl.gz")) != n_in:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"skip": f"gsi copy check failed ({n_in} in, {n_out} out); nothing deleted"}
    info |= {"after_mb": round((_size(tmp) + 20000) / 1e6, 1), "at": time.strftime("%Y-%m-%d %H:%M")}
    rep["storage"] = info
    (tmp / "report.json").write_text(json.dumps(rep, indent=1, default=str))
    (tmp / "report.md").write_text(to_md(rep))
    for n in ("review.json", "review.md"):  # the learning review survives compaction
        if src := next((d / n for d in reversed(dirs) if (d / n).exists()), None):
            shutil.copy2(src, tmp / n)
    for d in dirs:
        shutil.rmtree(d)
    tmp.rename(target)
    return info


# ---- profile ------------------------------------------------------------------------------------------------------
def _pdir():
    return world.DATA / "profile"


def record(r):
    """Upsert the compact game record (by matchid) into games.jsonl, then rebuild profile.json."""
    g = {"matchid": r["matchid"], "date": r.get("date") or r["generated"][:10], "hero": r["hero"], "role": r["role"], "mode": r["mode"], "result": r["result"],
         "duration": r["duration"], "kda": r["kda"], "lh10": r["lh10"], "gpm": r["gpm"], "xpm": r["xpm"],
         "deaths": [{k: d.get(k) for k in ("clock", "zone", "phase", "night", "missing", "ally_dist", "warned", "cause", "called")} for d in r["deaths"]],
         "goal": r.get("goal") and {k: r["goal"][k] for k in ("text", "target", "met")},  # W.history reads it back (coach.history)
         "items": [[i["item"], i["clock"]] for i in r["items"] if _built(i["item"])], "alone_late_min": r["alone_late_min"], "late_min": r["late_min"],
         "alerts": {k: {f: v[f] for f in ("episodes", "useful", "false", "responded", "ignored", "up", "down")}
                    for k, v in r["scorecard"]["rules"].items() if k in SCORED_RULES or v["up"] or v["down"]}}
    p = _pdir() / "games.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    games = [x for x in _jsonl(p) if x.get("matchid") != g["matchid"]] + [g]
    p.write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in games))
    rebuild()


def _built(n):
    """Upgraded item (has components) or Shard; everything when item data isn't loaded."""
    import data
    it = data.S["items"]
    return not it or n == "aghanims_shard" or bool((it.get(n) or {}).get("components"))


def rebuild():
    G = _jsonl(_pdir() / "games.jsonl")
    D = [d for g in G for d in g["deaths"]]
    n = len(D) or 1
    al = {}
    for g in G:
        for k, v in g["alerts"].items():
            a = al.setdefault(k, Counter())
            a.update(v)
    it = {}
    for g in G:
        for name, c in g["items"]:
            it.setdefault(g["hero"], {}).setdefault(name, []).append(c)
    prof = {"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "games": len(G), "wins": sum(g["result"] == "win" for g in G),
            "losses": sum(g["result"] == "loss" for g in G), "heroes": dict(Counter(g["hero"] for g in G).most_common()),
            "roles": dict(Counter(str(g["role"]) for g in G).most_common()), "avg_deaths": round(len(D) / max(len(G), 1), 1),
            "avg_lh10": round(statistics.fmean(x) if (x := [g["lh10"] for g in G if g["lh10"] is not None]) else 0, 1),
            "hotspots": [{"spot": k, "deaths": v} for k, v in Counter(f"{d['zone']}|{d['phase']}|{'night' if d['night'] else 'day'}" for d in D).most_common(5)],
            "death_zones": dict(Counter(d["zone"] for d in D).most_common()),
            "mistakes": {"deaths_3plus_missing": round(sum(d["missing"] >= 3 for d in D) / n, 2),
                         "deaths_alone": round(sum((d["ally_dist"] or 1e9) > 2500 for d in D) / n, 2),
                         "deaths_after_warning": round(sum(d["warned"] is True for d in D) / n, 2),
                         "deaths_unwarned": round(sum(d["warned"] is False for d in D) / n, 2),
                         "late_alone_share": round(sum(g["alone_late_min"] for g in G) / max(sum(g["late_min"] for g in G), 1), 2)},
            "item_timings": {h: {k: round(statistics.median(v)) for k, v in sorted(x.items(), key=lambda kv: statistics.median(kv[1]))}
                             for h, x in it.items()},
            "alerts": {k: {**dict(v), "response_rate": round(v["responded"] / max(v["responded"] + v["ignored"], 1), 2),
                           "false_rate": round(v["false"] / max(v["episodes"], 1), 2)} for k, v in sorted(al.items())}}
    (_pdir() / "profile.json").write_text(json.dumps(prof, indent=1))
    return prof


def profile():
    return _load(_pdir() / "profile.json")


def profile_hints(hero=None, role=None):
    """<= 400 chars of this player's tendencies, most relevant first ('' with no history)."""
    p = profile()
    if not p.get("games"):
        return ""
    m, H = p["mistakes"], [f"Player history: {p['games']} games ({p['wins']}W-{p['losses']}L), avg {p['avg_deaths']} deaths."]
    if p["hotspots"] and p["hotspots"][0]["deaths"] >= 2:
        z, ph, dn = p["hotspots"][0]["spot"].split("|")
        H.append(f"Dies most in {z.replace('_', ' ')} ({ph} game, {dn}).")
    if m["deaths_3plus_missing"] >= 0.4:
        H.append(f"{m['deaths_3plus_missing']:.0%} of deaths with 3+ enemies missing.")
    if m["deaths_alone"] >= 0.4:
        H.append(f"{m['deaths_alone']:.0%} of deaths far from allies.")
    ign = [k for k, v in p["alerts"].items() if v.get("responded", 0) + v.get("ignored", 0) >= 3 and v["response_rate"] < 0.4]
    if ign:
        H.append(f"Often ignores {'/'.join(ign)} warnings: be firm.")
    if m["late_alone_share"] >= 0.5:
        H.append("Farms alone late; push to group.")
    t = (p["item_timings"].get(hero) or {}) if hero else {}
    if t:
        H.append(f"Usual {hero} timings: " + ", ".join(f"{k} {world.mmss(v)}" for k, v in list(t.items())[:4]) + ".")
    out = ""
    for h in H:
        if len(out) + len(h) + 1 > 400:
            break
        out += (" " if out else "") + h
    return out


def rule_tuning():
    """Per-player rule knobs from profile.json (rules.py may read them; nothing here changes rules):
    R1_threshold_scale: multiply R1's danger threshold (<1 = warn earlier): 0.85 when >= 40% of deaths had no warning or came with
        3+ missing, 1.15 when R1's false rate > 0.6, else 1.0.
    danger_zones: zones with >= 2 deaths and >= 25% of all deaths (warn earlier / louder there).
    escalate: rules the player ignores (>= 3 measured, response < 40%) -> show at a higher level.
    mute_candidates: rules with >= 5 episodes and false rate > 0.7.
    demote: the precision gate rules.evaluate applies: rules with >= 10 recorded episodes of which < 50% were useful are shown amber
        and not spoken. They keep being scored (alerts are recorded at every level), so a rule comes back when it recovers.
    games: number of games behind these numbers (treat < 3 as weak evidence)."""
    p = profile()
    if not p.get("games"):
        return {"games": 0}
    m, al, nd = p["mistakes"], p["alerts"], sum(p["death_zones"].values()) or 1
    r1 = al.get("R1", {})
    scale = 0.85 if max(m["deaths_unwarned"], m["deaths_3plus_missing"]) >= 0.4 else 1.15 if r1.get("false_rate", 0) > 0.6 else 1.0
    return {"games": p["games"], "R1_threshold_scale": scale,
            "demote": [k for k, v in al.items() if v.get("episodes", 0) >= 10 and v.get("useful", 0) / v["episodes"] < 0.5],
            "danger_zones": [z for z, n in p["death_zones"].items() if n >= 2 and n / nd >= 0.25],
            "escalate": [k for k, v in al.items() if v.get("responded", 0) + v.get("ignored", 0) >= 3 and v["response_rate"] < 0.4],
            "mute_candidates": [k for k, v in al.items() if v.get("episodes", 0) >= 5 and v["false_rate"] > 0.7]}
