"""World state W: one dict guarded by `lock`, sent whole over SSE. Scratch state lives in W["_"] and is never sent."""
import json, logging, math, os, statistics, threading, time
from pathlib import Path

ROOT = Path(__file__).parent
DATA = Path(os.environ.get("COACH_DATA_DIR") or ROOT / "data")  # tests point this elsewhere (app.py --data-dir)
CALIB, SETTINGS = DATA / "calib.json", DATA / "settings.json"
TEAM = {2: "radiant", 3: "dire"}
GHOST = ("minimap_plaincircle", "minimap_heroimage")  # GSI minimap images that are not a live hero at that spot
# summon unit names that don't start with their hero's short name (npc_dota_<hero>_... covers the rest: plague/serpent/death wards, imps, bears...)
SUMMON_ALIAS = {"lesser_eidolon": "enigma", "eidolon": "enigma", "unit_tombstone": "undying", "unit_undying": "undying",
                "wraith_king_skeleton": "skeleton_king", "ignis_fatuus": "keeper_of_the_light"}
log = logging.getLogger("coach")
lock = threading.RLock()
W = {}


def num(v, default=None):
    """GSI numbers arrive as int, float, numeric string or not at all."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if not math.isfinite(f) else int(f) if f.is_integer() else f


def flag(v):
    return v is True or v in (1, "1", "true", "True")


def obj(x, k):
    v = x.get(k) if isinstance(x, dict) else None
    return v if isinstance(v, dict) else {}


def short(name):
    return name[14:] if isinstance(name, str) and name.startswith("npc_dota_hero_") and len(name) > 14 else None


def item(it):
    n = it.get("name") if isinstance(it, dict) else None
    return n[5:] if isinstance(n, str) and n.startswith("item_") else None


def mmss(c):
    s = int(c or 0)
    return f"{'-' if s < 0 else ''}{abs(s) // 60}:{abs(s) % 60:02d}"


def _load(p):
    try:
        v = json.loads(p.read_text())
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def set_data_dir(d):
    """Use `d` for calib.json/settings.json (tests never touch the real data/)."""
    global DATA, CALIB, SETTINGS
    DATA = Path(d)
    CALIB, SETTINGS = DATA / "calib.json", DATA / "settings.json"
    with lock:
        W.clear()
        reset()


def turbo():
    """Manual Turbo toggle wins for the match it was set in (or always, via --turbo); otherwise auto-detected."""
    s = W.get("settings") or {}
    if s.get("turbo_match") in ("always", (W.get("game") or {}).get("matchid")):
        return bool(s.get("turbo"))
    return bool((W.get("_") or {}).get("turbo_auto"))


WARDS = {"ward_observer", "ward_sentry", "ward_dispenser"}
SUPPORT_ITEMS = {"glimmer_cape", "force_staff", "ghost", "solar_crest", "guardian_greaves", "mekansm", "pipe", "spirit_vessel",
                 "urn_of_shadows", "pavise", "holy_locket", "lotus_orb", "aether_lens", "rod_of_atos", "boots_of_bearing", "vladmir"}


def turbo_guess(lh, cg, xpm, clock, tp_max=None):
    """Turbo doubles creep bounties (~65-105 g per last hit vs ~40-55 normal) and XP. True/False, or None = can't tell yet.
    First choice: the teleport scroll's full cooldown, 61 s then 41 s in all six recorded Turbo games, from the first second.
    ponytail: a normal game is assumed to read ~80 s (none recorded yet); if that is wrong, drop this test.
    Else from gold_from_creep_kills / last_hits once there are 6 LH (a Turbo Enigma read 62 g/LH at 8:00: converted creeps pay
    nothing), else (a support with few last hits) from XPM at 8:00."""
    if tp_max:
        return tp_max <= 70
    if lh and lh >= 6 and cg is not None:
        return cg / lh >= 65
    return xpm >= 620 if clock >= 480 and xpm is not None else None


def _auto_detect(p, me, clock):
    """No game-mode or position field in GSI, so infer both from behaviour.
    Turbo: turbo_guess(), re-checked on every payload until 8:00 (the first six last hits can mislead: one recorded Turbo game read
    51.7 g/LH at the first check and 76 at 8:00), then frozen. postgame.analyze uses the same test.
    Role (unless picked in the UI this match): from 5:00, every 60 s until 12:00, from laning zone + last hits + wards."""
    _ = W["_"]
    if clock > 0 and (clock <= 480 or _.get("turbo_auto") is None):
        t = turbo_guess(num(p.get("last_hits")), num(p.get("gold_from_creep_kills")), num(p.get("xpm")), clock, me.get("tp_max"))
        _["turbo_auto"] = _.get("turbo_auto") if t is None else t
    if me["x"] is not None and 60 <= clock <= (240 if turbo() else 480):
        z = _.setdefault("zones", {})
        zn = zone(me["x"], me["y"], me["team"])
        z[zn] = z.get(zn, 0) + 1
    if any((i or "").removeprefix("item_") in WARDS for i in me["items"] + me["backpack"] + me["stash"]):
        _["wards"] = True
    t0 = 150 if turbo() else 300
    late = clock > 2.4 * t0 and me.get("role_src") == "auto-play"  # settled; after a mid-game restart still decide once
    if W["settings"].get("role_match") == W["game"]["matchid"] or clock < t0 or late or clock - _.get("role_at", -1e9) < 60:
        return
    _["role_at"] = clock
    z = _.get("zones") or {}
    lane = max(("safe_lane", "mid", "off_lane"), key=lambda k: z.get(k, 0)) if z else None
    lhm = (me["lh"] or 0) / max(clock / 60, 1) / (2 if turbo() else 1)
    import data
    big = [i for i in me["items"] + me["backpack"] if i and i not in SUPPORT_ITEMS and num((data.S["items"].get(i) or {}).get("cost"), 0) >= 2000]
    core = lhm >= 2.5 or (lhm >= 1.5 and not _.get("wards")) or bool(big)  # a 2000g+ non-support item by ~8 min = core
    me["role"] = {"mid": 2, "safe_lane": 1 if core else 5, "off_lane": 3 if core else 4}.get(lane, me["role"]) if core or lane else me["role"]
    me["role_src"] = "auto-play"


def reset():
    s = W.get("settings") or {"role": None, "bracket": None, "turbo": False, "verbosity": "normal", "volume": 1.0, "quiet": False,
                              **_load(SETTINGS)}
    pits = {"top": [-2700, 2300], "bot": [2600, -2700],  # ponytail: approximate pit spots; /api/mark (hero demo) overrides
            **{k: v for k, v in (_load(CALIB).get("rosh_pits") or {}).items() if k in ("top", "bot") and isinstance(v, list) and len(v) == 2}}
    err = (W.get("cv") or {}).get("err")  # capture error outlives a new match
    W.clear()
    W.update(
        t=0.0, src_age={"gsi_s": None, "cv_s": None},
        game={"state": None, "clock": None, "daytime": True, "ns_night": False, "paused": False, "matchid": None,
              "map": None, "kind": None, "score": [0, 0], "win_team": None, "mode": "turbo" if s.get("turbo") else "normal"},
        me={k: None for k in "hero hero_id team level alive respawn_s hp_pct mp_pct gold buyback_cost buyback_cd "
            "k d a lh dn gpm xpm x y xy_t neutral tp_cd smoked scepter shard".split()}
        | {"role": s.get("role"), "items": [None] * 6, "backpack": [None] * 3, "stash": [None] * 6, "abilities": []},
        heroes={}, lineup={"locked": False, "heroes": [None] * 10, "scores": [0] * 10},
        obj={"rosh": {"state": "alive", "killed_at": None, "killer_team": None, "aegis_until": None, "pit": "top",
                      "last_seen_at": None, "pits": pits, "src": None, "respawn_in_s": None},
             "tormentor": None, "my_buildings": {}},
        events=[], alerts=[], timers=[], advice={}, enemy_report=[],
        goal=None, brief=None, debrief=None,  # coach.py fills these (plus coach, history, ready) every tick
        cv={"fps": None, "ms": None, "minimap_rect": None, "A": None, "size": None, "icon_px": None, "affine_ok": False,
            "gsi_vs_cv_err_u": None, "frame_ok": False, "err": err},
        llm={"enabled": False, "calls": 0, "in_flight": False, "last_at": None, "usd_est": 0.0, "ok": None},
        jev={"enabled": False, "calls": 0, "in_flight": False, "ok": None, "err": None, "last_at": None, "usd_est": 0.0, "latency_ms": None},
        decision=None,
        attention={"seq": 0, "id": None, "level": "none", "word": None, "icon": None, "text": None, "since_clock": None, "sticky": False,
                   "dir": None},
        glance={"next_item": None, "next_timer": None, "mia": 0, "danger": None, "action": None, "rosh": None},
        rec={"on": False, "dir": None, "mb": 0, "frames": 0, "dropped": 0, "marks": 0, "frames_paused": False},
        settings=s,
        _={"gsi_wall": None, "cv_wall": None, "clock": None, "vis": {}, "ev_seen": set(), "new_events": [],
           "rosh_seen": None, "cv_err": [], "cv_times": []},
    )


def _hero(s, team=None):
    e = W["heroes"].get(s)
    if e is None:
        e = W["heroes"][s] = {"team": None, "slot": None, "ally": False, "visible": False, "x": None, "y": None,
                              "src": None, "last_seen": None, "last_xy": None, "mia_s": None, "reach_u": 0,
                              "dead": False, "respawn_s": None, "dead_until": None, "conf": None, "vanished_at": None, "vis_since": None}
    if team in ("radiant", "dire") and e["team"] is None:  # a hero never changes team: GSI's ghost markers carry the wrong one
        e["team"], e["ally"] = team, team == W["me"]["team"]
    return e


def _vis(s, e, src, on, x=None, y=None, clock=None):
    """GSI sighting beats CV; visible = either source; visible (>= 5 s) -> unseen sets vanished_at (fog flicker doesn't)."""
    v = W["_"]["vis"].setdefault(s, {"gsi": False, "cv": False})
    if on and src == "cv" and e["dead"] and e.get("death_mark") and e.get("died_at") is not None:
        on = False  # GSI shows this hero's corpse and its kill event: a CV frame taken just before the death must not revive it
    v[src] = on
    if on and (src == "gsi" or not v["gsi"]):
        e.update(x=x, y=y, src=src, last_seen=clock, last_xy=[x, y], mia_s=0, reach_u=0, vanished_at=None, dead=False, dead_until=None)
        for k in ("resp_at", "died_at", "hint", "hint_at", "illusion"):
            e.pop(k, None)
    was, e["visible"] = e["visible"], v["gsi"] or v["cv"]
    if e["visible"] and not was:
        e["vis_since"] = clock
    if was and not e["visible"] and not e["dead"] and clock is not None and clock - (e["vis_since"] or clock) >= 2:  # 5 s missed short sightings
        e["vanished_at"] = clock


def summon_owner(unit, heroes):
    """npc_dota_venomancer_plague_ward -> 'venomancer' if venomancer is in `heroes`; heroes / creeps / wards -> None."""
    if not isinstance(unit, str) or not unit.startswith("npc_dota_") or unit.startswith("npc_dota_hero_"):
        return None
    n = unit[9:]
    a = next((h for k, h in SUMMON_ALIAS.items() if n.startswith(k)), None)
    return a if a in heroes else None if a else max((h for h in heroes if n.startswith(h + "_")), key=len, default=None)


def merge_gsi(body, now=None, demo=False):
    """Ingest one GSI payload. Returns True if accepted. Never raises."""
    try:
        with lock:
            return _merge_gsi(body, now or time.time(), demo)
    except Exception:
        log.exception("merge_gsi failed; payload dropped")
        return False


def _merge_gsi(body, now, demo):
    if not isinstance(body, dict):
        return False
    p, m, h = obj(body, "player"), obj(body, "map"), obj(body, "hero")
    team, custom = p.get("team_name"), m.get("customgamename") or ""
    clock = num(m.get("clock_time"))
    if p.get("activity") != "playing" or team not in ("radiant", "dire") or clock is None or (custom and not demo):
        return False
    mid = str(m.get("matchid") or "")
    if not W or W["game"]["matchid"] not in (None, mid) or (W["_"]["clock"] is not None and clock < W["_"]["clock"] - 30):
        reset()  # new match, or clock rolled back (replay restarted)
    _, g, me = W["_"], W["game"], W["me"]
    off = (num(m.get("game_time"), clock) or clock) - clock
    _.update(gsi_wall=now, clock=clock)
    g.update(state=m.get("game_state"), clock=clock, daytime=flag(m.get("daytime")), ns_night=flag(m.get("nightstalker_night")),
             paused=flag(m.get("paused")), matchid=mid, map=m.get("name"), win_team=m.get("win_team"),
             kind="demo" if m.get("name") == "hero_demo_main" else "custom" if custom else "normal",
             score=[num(m.get("radiant_score"), 0), num(m.get("dire_score"), 0)])
    if isinstance(m.get("roshan_state"), str):  # spectator-only per docs/research-gsi.md; mode-independent when present
        st = {"alive": "alive", "respawn_base": "dead", "respawn_variable": "maybe"}.get(m["roshan_state"])
        W["obj"]["rosh"].update(src="gsi", respawn_in_s=num(m.get("roshan_state_end_seconds")),  # ponytail: UNVERIFIED = seconds left
                                **({"state": st} if st else {}))
    if isinstance(m.get("tormentor_state"), str):
        W["obj"]["tormentor"] = {"state": m["tormentor_state"], "end_s": num(m.get("tormentor_state_end_seconds"))}

    x, y = num(h.get("xpos")), num(h.get("ypos"))
    x, y = (x, y) if x is not None and y is not None else (None, None)  # both or neither
    hero = short(h.get("name")) or me["hero"]
    hid = num(h.get("id"))
    if me["team"] != team:
        for e in W["heroes"].values():
            e["ally"] = e["team"] == team
    if W["settings"].get("role_match") == W["game"]["matchid"]:
        me["role"], me["role_src"] = W["settings"].get("role"), "manual"
    elif hero and me.get("role_src") != "auto-play":  # until play reveals it (_auto_detect), guess from the hero
        import data  # ponytail: Carry->1, Support->5, else 2
        roles = (data.S["heroes"].get(hero) or {}).get("roles") or []
        me["role"], me["role_src"] = (1 if "Carry" in roles and "Support" not in roles else 5 if "Support" in roles else 2), "auto-hero"
    me.update(team=team, hero=hero, hero_id=hid if hid and hid > 0 else me["hero_id"], level=num(h.get("level"), me["level"]),
              alive=flag(h["alive"]) if "alive" in h else me["alive"], respawn_s=num(h.get("respawn_seconds")),
              hp_pct=num(h.get("health_percent")), mp_pct=num(h.get("mana_percent")), gold=num(p.get("gold")),
              buyback_cost=num(h.get("buyback_cost")), buyback_cd=num(h.get("buyback_cooldown")),
              k=num(p.get("kills")), d=num(p.get("deaths")), a=num(p.get("assists")), lh=num(p.get("last_hits")),
              dn=num(p.get("denies")), gpm=num(p.get("gpm")), xpm=num(p.get("xpm")), xp=num(h.get("xp")), x=x, y=y, xy_t=now,
              smoked=flag(h.get("smoked")), disabled=bool(flag(h.get("stunned")) or flag(h.get("hexed"))), scepter=flag(h.get("aghanims_scepter")), shard=flag(h.get("aghanims_shard")))
    it = obj(body, "items")
    if it:
        me.update(items=[item(it.get(f"slot{i}")) for i in range(6)], backpack=[item(it.get(f"slot{i}")) for i in range(6, 9)],
                  stash=[item(it.get(f"stash{i}")) for i in range(6)], neutral=item(it.get("neutral0")),
                  tp_cd=num(obj(it, "teleport0").get("cooldown")), tp=item(it.get("teleport0")) is not None,
                  tp_max=num(obj(it, "teleport0").get("max_cooldown")) or me.get("tp_max"))
    ab = obj(body, "abilities")
    if ab:
        me["abilities"] = [{"name": a["name"], "level": num(a.get("level"), 0), "cd": num(a.get("cooldown"), 0), "ult": flag(a.get("ultimate"))}
                           for k, a in sorted(ab.items(), key=lambda kv: num(str(kv[0])[7:], 99))
                           if isinstance(a, dict) and isinstance(a.get("name"), str)]
    if h:
        me["talents"] = sum(bool(flag(h.get(f"talent_{i}"))) for i in range(1, 9))
        me["attributes_level"] = num(h.get("attributes_level"), 0) or 0
    me["account"] = str(p.get("accountid") or me.get("account") or "")
    _auto_detect(p, me, clock)
    for side in obj(body, "buildings").values():
        for name, b in (side if isinstance(side, dict) else {}).items():
            hp, mx = num(obj(side, name).get("health")), num(obj(side, name).get("max_health"))
            W["obj"]["my_buildings"][name] = round(100 * hp / mx, 1) if hp is not None and mx else None
    gl = num(obj(body, "map").get(f"{team}_glyph_cooldown"))
    W["obj"]["glyph_cd"] = gl if gl is not None else W["obj"].get("glyph_cd")

    if hero:
        e = _hero(hero, team)
        _vis(hero, e, "gsi", bool(me["alive"]) and me["x"] is not None, me["x"], me["y"], clock)
    try:  # its own guard: an event stays in GSI's array for ~29 s, and merge_gsi drops the payload on any raise
        _kill_events(body, team)
    except Exception:
        if not _.get("ek_err"):  # once per match
            log.exception("kill-event scan failed; payload kept")
        _["ek_err"] = True
    ek = _.setdefault("ek", [])  # [clock time, victim player id, used]
    mm = body.get("minimap")
    if isinstance(mm, dict) and mm:
        _["gsi_mm"] = True  # GSI shows heroes: it is the authority on who is in the game and who died (CV only fills gaps)
    if isinstance(mm, dict):
        seen, gone, summ = {}, {}, []  # gone: visionrange 0 = the static death marker Dota keeps at the death spot until respawn
        sight = _["sight"] = []  # my team's vision sources (x, y, visionrange) of this payload: fog.py's "lit" test
        foe = {"radiant": "dire", "dire": "radiant"}[team]
        for o in mm.values():
            if not isinstance(o, dict):
                continue
            x, y, u, img = num(o.get("xpos")), num(o.get("ypos")), o.get("unitname"), o.get("image")
            if x is not None and y is not None and (x, y) != (0, 0) and TEAM.get(num(o.get("team"))) == foe and isinstance(u, str) \
                    and not u.startswith("npc_dota_hero_"):
                summ.append((u, x, y))  # enemy summons show as minimap_plaincircle at their real spot
            if x is not None and y is not None and (x, y) != (0, 0) and max(abs(x), abs(y)) <= 12000 and TEAM.get(num(o.get("team"))) == team \
                    and (vr := num(o.get("visionrange"), 0)) > 0:
                sight.append((x, y, vr))  # before the skip below: wards, buildings and my illusions see too (as predict_eval's obs.vision())
            if x is None or y is None or (x == 0 and y == 0) or max(abs(x), abs(y)) > 12000 or img in GHOST or str(img).startswith("minimap_ping"):
                continue  # TP pings are where a hero is going, not where it is; heroimage = last-seen marker of an enemy (team = ours)
            if u == "npc_dota_roshan":
                _rosh_seen(x, y, clock)
            elif (s := short(u)) and s != hero and s not in seen:
                t = TEAM.get(num(o.get("team"))) or (team if str(img).startswith("minimap_herocircle") else
                                                     {"radiant": "dire", "dire": "radiant"}[team])
                if (k := (W["heroes"].get(s) or {}).get("team")) and t != k:
                    continue  # wrong-team copy (illusion / marker): it made enemy Juggernaut + Pudge count as allies in fights
                (gone if num(o.get("visionrange")) == 0 else seen)[s] = (x, y, t)
        # A fresh enemy summon (plague ward, serpent ward, imp...) marks where its hidden owner just was: wards land ~800 u from the
        # caster (median, 2026-11-15 Venomancer). Fresh = no same-name unit within 600 u in the last 3 s (GSI object ids reshuffle)
        # and none on the same spot in the last 60 s (an old ward coming back into vision is not news).
        foes = {s for s, e in W["heroes"].items() if e["team"] == foe}
        prev = [p for p in _.get("summ", []) if clock - p[3] <= 60]
        for u, x, y in summ:
            if not any(u == u2 and math.hypot(x - x2, y - y2) < (600 if clock - c2 <= 3 else 150) for u2, x2, y2, c2 in prev) \
                    and (s := summon_owner(u, foes)) and s not in seen and not (e := W["heroes"][s])["visible"] and not e["dead"]:
                # ponytail: owner within ~1000 u = 3 s of walking, so last_seen is backdated; per-spell cast ranges if this misleads
                e.update(last_xy=[x, y], last_seen=clock - 3, mia_s=3, hint=u[9:], hint_at=clock)
                e.pop("resp_at", None)
                log.info("hint %s: %s near %d,%d", mmss(clock), s, x, y)
        _["summ"] = [p for p in prev if all(p[0] != u or math.hypot(p[1] - x, p[2] - y) >= 150 for u, x, y in summ)] \
            + [(u, x, y, clock) for u, x, y in summ]  # one row per unit spot, newest clock
        for s, (x, y, t) in seen.items():
            _vis(s, _hero(s, t), "gsi", True, x, y, clock)
            W["heroes"][s]["death_mark"] = False
            W["heroes"][s].pop("mark_at", None), W["heroes"][s].pop("mark_fresh", None)
        # a hero that was in sight a moment ago and now shows a corpse marker is the real victim: it claims a kill event first
        for s, (x, y, t) in sorted(gone.items(), key=lambda kv: not _hero(kv[0], kv[1][2])["visible"]):
            _hero(s, t).pop("mark_gone", None)
            if s not in seen and not (e := _hero(s, t)).get("death_mark"):
                if not _.get("ek_on") or e["ally"]:  # no kill event seen yet this match, or an ally (its marker is its death): old behaviour
                    e.update(dead=True, death_mark=True, visible=False, vanished_at=None, last_xy=[x, y], dead_until=None, respawn_s=None)
                    continue
                was = e["visible"]
                first = e.setdefault("mark_at", clock)
                e.setdefault("mark_fresh", was)  # the icon turned into the marker in front of us
                _vis(s, e, "gsi", False, clock=clock)
                k = next((k for k in ek if not k[2] and first - 6 <= k[0] <= first + 2.5 and (e["mark_fresh"] or clock - k[0] >= 1.5)), None)
                e_pop = lambda: (e.pop("mark_at", None), e.pop("mark_fresh", None))
                if k:
                    k[2] = True
                    e.update(dead=True, death_mark=True, visible=False, vanished_at=None, last_xy=[x, y], died_at=k[0], dead_until=k[0] + respawn_est(k[0]), respawn_s=None)
                    e_pop()
                elif clock - first > 2.5:
                    e["death_mark"] = True  # an illusion's corpse (or an old one coming back into view): not a death
                    if e["mark_fresh"]:  # the copy we were following died in front of us with no kill event: an illusion, its spot says nothing about the hero
                        e["illusion"] = True
                    e_pop()
        for s, e in W["heroes"].items():
            if s == hero or s in seen or s in gone:
                continue
            e.pop("mark_at", None), e.pop("mark_fresh", None)  # no marker this payload: a later one starts a new claim
            if e.get("death_mark"):  # marker gone for 4 s = respawned in fountain, unseen (it flickers while vision changes)
                e.setdefault("mark_gone", clock)
                if clock - e["mark_gone"] < 4:
                    continue
                e.pop("mark_gone", None)
                e["death_mark"] = False
                # with kill events an enemy's respawn is the timer's job (tick). A death that has no timer (its marker came before the first kill event) still ends here
                if not _.get("ek_on") or e["ally"] or (e["dead"] and e["dead_until"] is None):
                    e.update(dead=False, visible=False, vanished_at=None, last_seen=clock,
                             last_xy=list(FOUNTAIN[e["team"]]) if e.get("team") in FOUNTAIN else e["last_xy"])
                    if not e["ally"]:
                        e["resp_at"] = clock  # an unseen respawn: last_xy is no sighting (fog.py shows a time, no mark)
            else:
                _vis(s, e, "gsi", False, clock=clock)
    for k in ek:
        if k[2] or clock - k[0] < 5:
            continue
        k[2] = True
        if clock - k[0] <= 10:
            gone_ = sorted((e for e in W["heroes"].values() if not e["ally"] and e.get("team") and not e["dead"] and not e["visible"]
                            and e["last_seen"] is not None and k[0] - 6 <= e["last_seen"] <= k[0] + 1
                            and e.get("resp_at") is None and e.get("hint_at") is None), key=lambda e: -e["last_seen"])  # their last_seen is no sighting
            for e in gone_[:1]:
                e.update(dead=True, vanished_at=None, died_at=k[0], dead_until=k[0] + respawn_est(k[0]))
    if not _.get("ek_on"):
        _kills(clock, team)

    evs = body.get("events")
    for ev in evs if isinstance(evs, list) else []:
        if not isinstance(ev, dict) or not isinstance(ev.get("event_type"), str):
            continue
        key = json.dumps(ev, sort_keys=True, default=str)  # dedupe: same type+time+ids repeat across payloads
        if key in _["ev_seen"]:
            continue
        _["ev_seen"].add(key)
        gt = num(ev.get("game_time"))
        e = {"type": ev["event_type"], "clock": gt - off if gt is not None else clock,
             **{k: v for k, v in ev.items() if k not in ("event_type", "game_time")}}
        W["events"] = (W["events"] + [e])[-30:]
        _["new_events"].append(e)
        r = W["obj"]["rosh"]
        if e["type"] == "roshan_killed":
            r.update(state="dead", killed_at=e["clock"], killer_team=ev.get("killed_by_team"))
        elif e["type"] == "aegis_picked_up":
            r["aegis_until"] = e["clock"] + (240 if turbo() else 300)  # turbo 240 s per docs/research-data.md
        elif e["type"] == "aegis_denied":
            r["aegis_until"] = None
    return True


def _kill_events(body, team):
    """CHAT_MESSAGE_HERO_KILL events -> W["_"]["ek"]: [game clock, victim player id, used] per enemy death (last 40; GSI repeats an
    event for ~29 s). The first one of either team sets ek_on: from then on kill events decide enemy deaths, not the kill score."""
    _ = W["_"]
    ek = _.setdefault("ek", [])
    for ev in body.get("events") if isinstance(body.get("events"), list) else []:
        d = ev.get("data") if isinstance(ev, dict) and ev.get("event_type") == "generic_event" else None
        if isinstance(d, str) and "CHAT_MESSAGE_HERO_KILL" in d:
            try:
                d = json.loads(d)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("type") != "CHAT_MESSAGE_HERO_KILL":
                continue
            _["ek_on"] = True
            v, t = num(d.get("playerid1")), num(d.get("time"))
            if v is not None and t is not None and (v >= 5) == (team == "radiant") and not any(k[0] == t and k[1] == v for k in ek):
                ek.append([t, v, False])
    del ek[:-40]


def respawn_est(t):
    """Enemy respawn time by game clock (no enemy level in GSI). Turbo: lower envelope of 'death -> first seen again' in 19 games.
    ponytail: normal mode has no recording; this is the level table's timer for the lowest enemy (about level 4 / 7 / 12 / 16 at
    5 / 10 / 20 / 30 min), kept low on purpose: early = a chip without a spot, late = a living enemy the coach calls dead. Refit from a game."""
    m = max(t, 0) / 60
    return min(60, 8 + 2.0 * m) if turbo() else min(100, 12 + 1.5 * m)


def _kills(clock, team):
    """GSI has no enemy death flag. When my team's kill score rises by k, the k enemies that dropped out of sight
    in the last 6 s most likely died (else they'd count as vanished/MIA). Pending kills wait up to 6 s for the vanish."""
    _, sc = W["_"], W["game"]["score"][0 if team == "radiant" else 1]
    prev, _["kills"] = _.get("kills"), sc
    if prev is not None and sc > prev:
        _["kill_pend"], _["kill_at"] = _.get("kill_pend", 0) + sc - prev, clock
    if not _.get("kill_pend") or clock - _["kill_at"] > 6:
        _["kill_pend"] = 0
        return
    gone = sorted((e for e in W["heroes"].values() if not e["ally"] and not e["dead"] and not e["visible"]
                   and e["last_seen"] is not None and clock - e["last_seen"] <= 6), key=lambda e: -e["last_seen"])
    k = 0.75 if turbo() else 1  # ponytail: turbo respawn x0.75 is a guess
    for e in gone[:_["kill_pend"]]:  # ponytail: respawn ~6 s + 2.5 s/min (no enemy level in GSI); a sighting or CV ends it early
        e.update(dead=True, vanished_at=None, dead_until=clock + k * min(100, 6 + 2.5 * max(clock, 0) / 60))
        _["kill_pend"] -= 1


def _rosh_seen(x, y, clock):
    pit = "top" if x - y < 0 else "bot"  # pits sit either side of the mid diagonal
    r = W["obj"]["rosh"]
    r.update(state="alive", last_seen_at=clock)
    r["pits"].setdefault(pit, [x, y])  # ponytail: learned from first sighting (may be mid-walk), /api/mark overrides
    W["_"]["rosh_seen"] = (pit, int(clock // 300))


def merge_cv(cv, now=None):
    """Merge a vision.process() result. Never raises."""
    try:
        with lock:
            _merge_cv(cv, now or time.time())
    except Exception:
        log.exception("merge_cv failed")


def _merge_cv(cv, now):
    if not isinstance(cv, dict) or not W:
        return
    _, c = W["_"], W["cv"]
    _["cv_wall"] = now
    _["cv_times"] = (_["cv_times"] + [now])[-10:]
    ts = _["cv_times"]
    c.update({k: cv.get(k) for k in ("ms", "minimap_rect", "icon_px", "affine_ok", "frame_ok", "A", "size")},
             fps=round((len(ts) - 1) / (ts[-1] - ts[0]), 1) if len(ts) > 1 and ts[-1] > ts[0] else None)
    if not cv.get("frame_ok"):
        return
    me, clock = W["me"]["hero"], W["game"]["clock"]
    lu = cv.get("lineup")
    if isinstance(lu, dict) and isinstance(lu.get("heroes"), list) and len(lu["heroes"]) == 10:
        heroes = [x if isinstance(x, str) and x else None for x in lu["heroes"]]
        scores = lu.get("scores") if isinstance(lu.get("scores"), list) and len(lu["scores"]) == 10 else [0] * 10
        W["lineup"] = {"locked": bool(lu.get("locked")), "heroes": heroes, "scores": scores}
    sure = [s if s and (num(c) or 0) >= 0.75 else None for s, c in zip(W["lineup"]["heroes"], W["lineup"]["scores"])]  # vision.T_SLOT
    if W["_"].get("gsi_mm"):  # a misread top-bar slot must not invent heroes or kill real ones
        sure = [s if s in W["heroes"] else None for s in sure]
    for i, s in enumerate(sure):
        if s:
            e = _hero(s, "radiant" if i < 5 else "dire")
            e["slot"], e["conf"] = i, num(W["lineup"]["scores"][i])
    dead = cv.get("dead")
    if isinstance(dead, list) and len(dead) == 10:
        for i, s in enumerate(sure):
            if s and s != me and s in W["heroes"] and not W["_"].get("gsi_mm"):  # GSI death markers decide when present
                e = W["heroes"][s]
                if dead[i] and not e["dead"]:
                    e["visible"], e["vanished_at"] = False, None
                e["dead"] = bool(dead[i]) or bool(e.get("death_mark")) or (e["dead_until"] or -1e9) > (clock or 0)  # CV "alive" doesn't undo a GSI death
    if clock is None:
        return
    dets = {}
    for d in cv.get("minimap") or []:
        if isinstance(d, dict) and d.get("hero") in W["heroes"] and d["hero"] != me \
                and num(d.get("x")) is not None and num(d.get("y")) is not None:
            dets.setdefault(d["hero"], d)
    for s, d in dets.items():
        e = W["heroes"][s]
        if _["vis"].get(s, {}).get("gsi") and e["x"] is not None:
            _["cv_err"] = (_["cv_err"] + [math.dist((num(d["x"]), num(d["y"])), (e["x"], e["y"]))])[-50:]
        _vis(s, e, "cv", True, num(d["x"]), num(d["y"]), clock)
    for s, e in W["heroes"].items():
        if s != me and s not in dets:
            _vis(s, e, "cv", False, clock=clock)
    if _["cv_err"]:
        c["gsi_vs_cv_err_u"] = round(statistics.median(_["cv_err"]))


def tick(now=None):
    """Advance clock estimate, MIA timers, Roshan state and pit. Call every 0.25 s."""
    now = now or time.time()
    with lock:
        if not W:
            reset()
        _, g = W["_"], W["game"]
        W["t"] = now
        g["mode"] = "turbo" if turbo() else "normal"
        s_ = W["settings"]
        g["mode_src"] = "manual" if s_.get("turbo_match") in ("always", g["matchid"]) else "auto" if _.get("turbo_auto") is not None else "detecting"
        W["src_age"] = {k: round(now - _[w], 1) if _[w] else None for k, w in (("gsi_s", "gsi_wall"), ("cv_s", "cv_wall"))}
        lu = W["lineup"]["heroes"]  # top-bar CV missing a slot? fill it from heroes GSI has shown us (slot order is a guess)
        for team, rng in (("radiant", range(5)), ("dire", range(5, 10))):
            spare = iter(sorted(h for h, e in W["heroes"].items() if e.get("team") == team and h not in lu))
            for i in rng:
                if lu[i] is None:
                    lu[i] = next(spare, None)
        if _["clock"] is None:
            return
        clock = _["clock"] + (0 if g["paused"] else min(now - _["gsi_wall"], 2.0))  # GSI is ~1 Hz; interpolate a little
        g["clock"] = round(clock, 2)
        for e in W["heroes"].values():
            if e["dead_until"] is not None:
                e["respawn_s"] = round(max(0, e["dead_until"] - clock))
                if clock >= e["dead_until"]:
                    e.update(dead=False, dead_until=None, respawn_s=None)
                    if not e["ally"]:
                        e["resp_at"] = clock  # an unseen respawn, whichever rule called the death: last_xy is no sighting (fog.py)
                        if e.get("died_at") is not None:  # a kill event's death: the respawn timer puts it on its fountain
                            e.update(last_seen=clock, vanished_at=None, last_xy=list(FOUNTAIN[e["team"]]) if e.get("team") in FOUNTAIN else e["last_xy"])
            if e["visible"]:
                e.update(last_seen=clock, last_xy=[e["x"], e["y"]], mia_s=0, reach_u=0)
            elif not e["ally"] and not e["dead"] and e["last_seen"] is not None:
                e["mia_s"] = round(clock - e["last_seen"], 1)
                e["reach_u"] = min(330 * e["mia_s"], 6000)  # ponytail: flat 330 u/s, no pathing/TP
        r = W["obj"]["rosh"]
        if r["killed_at"] is not None and r["state"] != "alive" and r["src"] != "gsi":  # GSI roshan_state wins
            dt = clock - r["killed_at"]
            r["state"] = "dead" if dt < 480 else "maybe" if dt < 660 else "alive"
        seen = _["rosh_seen"]
        # ponytail: 7.41 pit rule is inferred (top until 15:00, then night=bot/day=top); a sighting wins for its phase
        r["pit"] = seen[0] if seen and seen[1] == int(clock // 300) else "top" if clock < 900 or g["daytime"] else "bot"


FOUNTAIN = {"radiant": (-7100, -6600), "dire": (7000, 6400)}  # ponytail: approximate fountain positions


def home_dist(x, y, team):
    """Distance to my own fountain: falling = retreating (used for 'did the player leave on the call')."""
    return math.dist((x, y), FOUNTAIN[team]) if x is not None and y is not None and team in FOUNTAIN else None


def zone(x, y, team):
    """Coarse map zone from `team`'s perspective; values match llm.SCHEMA 'where'."""
    if x is None or y is None:
        return "base"
    if any(math.dist((x, y), p) < 1200 for p in W.get("obj", {}).get("rosh", {}).get("pits", {}).values()):
        return "roshan"
    if team == "dire":
        x, y = -x, -y  # mirror so "own" side is always bottom-left
    # ponytail: rectangle/diagonal bands, good to ~500 u; swap for a polygon map if zones matter more
    if (x < -4800 and y < -4300) or (x > 4500 and y > 4000):
        return "base"
    if y < -5200 or x > 5200:  # lanes before river: the river diagonal (x+y=0) ends in the top/bot lane corners
        return "safe_lane"
    if x < -5200 or y > 5200:
        return "off_lane"
    if abs(x + y) < 1400:
        return "river"
    if abs(x - y) < 1600:
        return "mid"
    return "own_jungle" if x + y < 0 else "enemy_jungle"


def mark_pit(pit):
    """Store my current GSI position as a Roshan pit ('top'|'bot'), persisted in data/calib.json."""
    with lock:
        x, y = W["me"]["x"], W["me"]["y"]
        if x is None or pit not in ("top", "bot"):
            return False
        W["obj"]["rosh"]["pits"][pit] = [x, y]
        c = _load(CALIB)
        c.setdefault("rosh_pits", {})[pit] = [x, y]
        CALIB.parent.mkdir(parents=True, exist_ok=True)
        CALIB.write_text(json.dumps(c, indent=1))
        return True


def set_settings(**kw):
    """Update only the given keys (role, bracket, turbo, voice, mic, verbosity, commentary, volume, quiet)."""
    with lock:
        if "role" in kw and "role_match" not in kw:
            kw["role_match"] = W["game"]["matchid"]
            W["me"]["role_src"] = "manual"
        if "turbo" in kw and "turbo_match" not in kw:
            kw["turbo_match"] = W["game"]["matchid"]
        if "quiet" in kw:
            kw["quiet_match"] = W["game"]["matchid"]  # quiet lasts until the next match (coach.tick clears it)
        W["settings"] = W["settings"] | kw
        if "role" in kw:  # only a role change touches the role: a volume or quiet change must not undo the auto-detected one
            W["me"]["role"] = W["settings"].get("role")
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(W["settings"]))


def dumps():
    with lock:
        return json.dumps({k: v for k, v in W.items() if k != "_"}, separators=(",", ":"), default=str)


def snapshot():
    return json.loads(dumps())


def _bb(me):
    """Spell buyback out so the LLM can't misread gold vs cost."""
    g, c, cd = me.get("gold"), me.get("buyback_cost"), me.get("buyback_cd")
    if g is None or c is None:
        return "?"
    return f"on cooldown {int(cd)}s" if cd else "AFFORDABLE" if g >= c else f"CANNOT afford, {int(c - g)} short"


def _seen_text(clock):
    """Enemy items the player saw by clicking them (inspector.py), freshest first, dropped after 10 min."""
    seen = (W.get("inspect") or {}).get("seen") or {}
    rows = sorted(((clock or 0) - (v.get("clock") or 0), h, v) for h, v in seen.items())
    out = [f"{h}({int(age // 60)}:{int(age % 60):02d} ago): {','.join(i for i in v.get('items') or [] if i) or '-'}"
           for age, h, v in rows if age <= 600]
    return f" | seen items: {'; '.join(out)}" if out else ""


def _talk_text():
    """What the player told the coach by voice (listen.py): facts, focus, mood. Read by the LLM, Jev, builds and commentary."""
    t = W.get("talk") or {}
    notes = "; ".join(f"{mmss(n.get('clock'))} {n.get('text')}" for n in W.get("player_notes") or [])
    return ((f" | player says: {notes}" if notes else "") + (f" | player wants: {t['focus']}" if t.get("focus") else "")
            + (f" | player mood: {t['mood']}" if t.get("mood") not in (None, "calm") else ""))


def to_text(decision=True):
    """Compact (~400 token) snapshot for the LLM / Jev state. decision=False leaves out Jev's own last answer."""
    with lock:
        g, me, r = W["game"], W["me"], W["obj"]["rosh"]
        team, en = me["team"], []
        for s, e in W["heroes"].items():
            if e["ally"]:
                continue
            st = ("dead" if e["dead"] else f"vis,{zone(e['x'], e['y'], team)}" if e["visible"]
                  else f"MIA{e['mia_s']:.0f}s,last {zone(*e['last_xy'], team)}" if e["mia_s"] is not None and e["last_xy"] else "unseen")
            en.append(f"{s}({st})")
        al = "; ".join(f"{a['rule']} {a['text']}" for a in W["alerts"][:4]) or "-"
        dead = f" DEAD({me['respawn_s']}s)" if me["alive"] is False else ""
        d, jv = W.get("decision"), ""
        if decision and d and (g["clock"] or 0) - d["at_clock"] <= 15:
            jv = (f" | jev: {d['action']['choice']}({d['action']['p']:.2f}) @{d['where']['choice']} danger{d['danger']['score']:.1f}"
                  f" fight{d['fight']['p']:.2f} rosh_taken{d['rosh_taken']['p']:.2f}")
        tm = W["obj"].get("tormentor")
        return (f"t={mmss(g['clock'])} mode: {g.get('mode') or 'normal'} {'day' if g['daytime'] else 'night'} role={me['role'] or '?'} me={me['hero']} L{me['level']} "
                f"hp{me['hp_pct']} mp{me['mp_pct']} g{me['gold']} bb{me['buyback_cost']}({_bb(me)}) kda{me['k']}/{me['d']}/{me['a']} lh{me['lh']}{dead}"
                f" | items:{','.join(i for i in me['items'] if i) or '-'} | pos:{zone(me['x'], me['y'], team)}"
                f" | enemies: {' '.join(en) or '-'} | alerts: {al} | rosh:{r['state']} pit={r['pit']}"
                f"{' aegis_until=' + mmss(r['aegis_until']) if r['aegis_until'] else ''}{' tormentor:' + tm['state'] if tm else ''}"
                f" | score {g['score'][0]}-{g['score'][1]} | weak: {','.join(x['hero'] for x in W['enemy_report'][:2]) or '-'}{jv}"
                + _seen_text(g["clock"]) + _talk_text())


reset()
