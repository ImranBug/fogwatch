"""Dev/setup commands: install-gsi, sim, replay, synth, snap, selfcheck, sessions, review, postgame, compact, fetch-match.  `uv run tools.py <cmd> -h`."""
import argparse, gzip, json, math, os, secrets, statistics, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request
from pathlib import Path
import data

ROOT = Path(__file__).parent
TOKEN_FILE = ROOT / "data/token"
CFG = """"coach"
{
    "uri"       "http://127.0.0.1:%(port)d/gsi"
    "timeout"   "5.0"
    "buffer"    "0.1"
    "throttle"  "0.1"
    "heartbeat" "2.0"
    "auth"      { "token" "%(token)s" }
    "data"
    {
        "provider" "1" "map" "1" "player" "1" "hero" "1" "abilities" "1" "items" "1"
        "events" "1" "buildings" "1" "minimap" "1" "draft" "1" "roshan" "1"
        "couriers" "1" "neutralitems" "1" "league" "1" "wearables" "0"
    }
}
"""


def token():
    try:
        return TOKEN_FILE.read_text().strip() or None
    except OSError:
        return None


def install_gsi(a):
    d = Path(a.cfg_dir) if a.cfg_dir else data.DOTA / "cfg/gamestate_integration"
    tok = token() or secrets.token_hex(16)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok)
    TOKEN_FILE.chmod(0o600)
    d.mkdir(parents=True, exist_ok=True)
    (d / "gamestate_integration_coach.cfg").write_text(CFG % {"port": a.port, "token": tok})
    print(f"wrote {d / 'gamestate_integration_coach.cfg'} (token in {TOKEN_FILE})\n"
          "Steam -> Dota 2 -> Properties -> Launch Options: -gamestateintegration, then restart Dota")


# ---- scripted GSI timeline (spec section 8) --------------------------------------------------------------------
FOUNTAIN, TOP_PIT = (-7000, -6500), (-2700, 2300)


def _blink(a, b, p0, p1, phase=0):
    """Visible 25 s of every 50 s, wandering between p0 and p1 (laning in and out of fog)."""
    return [(t, t + 25, p0, p1) for t in range(a + phase, b, 50)]


ENEMY = {  # hero -> visible segments (from, to, start_xy, end_xy); hidden otherwise
    "lion": [(30, 660, (2400, -5900), (3400, -5000)), (705, 725, (2400, -5600), (1500, -6200)),
             (990, 1055, (-1200, 4400), (-2000, 3000)), (1090, 1501, (-2400, 2800), (300, 300))],
    "phantom_assassin": [(30, 661, (2800, -6100), (3600, -5100)), (707, 740, (2600, -5800), (1700, -6200)),
                         (780, 1501, (3500, -6000), (1500, -6200))],
    "earthshaker": _blink(0, 960, (600, 700), (200, 300)) + [(990, 1040, (-900, 3900), (-1800, 2800)), (1090, 1501, (-2200, 2600), (-300, -200))],
    "lina": _blink(0, 960, (-6200, 3500), (-6000, 4500), 10) + [(990, 1045, (-1500, 4100), (-2300, 2900)), (1090, 1501, (-2600, 2400), (-6000, 4000))],
    "chaos_knight": _blink(0, 960, (-5900, 3800), (-6300, 4800), 35) + [(990, 1050, (-1700, 4300), (-2500, 3100)), (1090, 1501, (-2300, 2200), (-5800, 3800))],
}
BOT_PIT = (2600, -2700)
for _i, _h in enumerate(list(ENEMY)):  # after 25:00: farm blinks, 3-hero fights every 4 min from 31:40, Roshan again at 30:00
    _farm = ((5500 - 900 * _i, 5800), (6200, 3000 - 600 * _i))
    ENEMY[_h] += ([(1700, 1745, (4500 + 300 * _i, -500 - 400 * _i), (BOT_PIT[0] + 250 * (_i % 2), BOT_PIT[1] + 250 * (_i // 2)))]
                  if _i < 4 else [])  # 4 walk to the (night) bot pit and vanish 29:05; Roshan dies 30:00
    ENEMY[_h] += [(f, f + 20, (1500 + 300 * _i, -1500), (300 + 200 * _i, -3000)) for f in range(1900, 3601, 240) if _i < 3]
    ENEMY[_h] += _blink(1501, 1700 if _i < 4 else 3601, *_farm, 10 * _i) + (_blink(1830, 3601, *_farm, 10 * _i) if _i < 4 else [])
DEATHS = [(720, 760), (2150, 2200)]
ALLY = {"crystal_maiden": lambda c, me: (me[0] - 600, me[1] + 300) if c < 630 else (-1600, 3400) if 990 <= c < 1005 else (-1500, -1500) if c < 990 else (-4500, -4500),
        "sniper": lambda c, me: (-1200 + 2 * (c % 300), -1200 + 2 * (c % 300)),
        "mars": lambda c, me: (-6000, 2000 + 3 * (c % 400)),
        "lich": lambda c, me: (-6200, 1500 + 3 * (c % 400))}


def _at(segs, c):
    for a, b, p0, p1 in segs:
        if a <= c < b:
            f = (c - a) / max(b - a, 1)
            return round(p0[0] + (p1[0] - p0[0]) * f), round(p0[1] + (p1[1] - p0[1]) * f)
    return None


def _dead(c):
    return next((b for a, b in DEATHS if a <= c < b), None)


def _me(c):
    if c < 0 or _dead(c) or 2200 <= c < 2230:
        return FOUNTAIN
    if c < 30:
        return _at([(0, 30, FOUNTAIN, (1200, -6300))], c)
    if c < 720:
        return round(1200 + 900 * math.sin(c / 23)), round(-6300 + 150 * math.cos(c / 17))
    if c < 790:
        return _at([(760, 790, FOUNTAIN, (0, -3500))], c)
    return round(600 * math.sin(c / 31)), round(-3500 + 400 * math.cos(c / 19))


def timeline(minutes=25, turbo=False):
    """Yield {"t": seconds, "body": GSI payload}: Juggernaut (Radiant) from strategy time to `minutes` at 1 Hz.
    Smoke at 11:00 (lion+PA vanish), death at 12:00, 4 enemies MIA near the top pit 17:20-17:35, Roshan dies 18:00.
    Longer games: fights every 4 min from 31:40, 4 MIA near the bot pit 29:05, Roshan dies 30:00, death 35:50.
    turbo: double gpm/gold/net worth, faster levels (the app still needs --turbo: GSI has no mode field)."""
    yield {"t": 0, "body": {"provider": {"name": "Dota 2", "appid": 570, "version": 47}, "player": {"activity": "menu"}}}
    k2 = 2 if turbo else 1
    for i, c in enumerate(range(-100, minutes * 60 + 1)):
        t = i + 1
        state = "STRATEGY_TIME" if c < -90 else "PRE_GAME" if c < 0 else "GAME_IN_PROGRESS"
        cl = max(c, -90)
        dead = _dead(c) is not None
        x, y = _me(c)
        lvl = 1 if c < 0 else min(30, 1 + c * k2 // 60)
        hp = 0 if dead else 60 + (c * 7) % 40
        nw = 600 + 7 * k2 * max(c, 0)
        mm, k = {}, 1 + (c % 7) * 20  # oN keys are not stable ids
        def put(**o):
            nonlocal k
            mm[f"o{k}"] = o
            k += 1
        if state == "STRATEGY_TIME":
            for h in ["juggernaut", *ALLY, *ENEMY]:
                put(xpos=0, ypos=0, image="minimap_plaincircle", team=2, yaw=0, unitname=f"npc_dota_hero_{h}", visionrange=0)
        else:
            if not dead:
                put(xpos=x, ypos=y, image="minimap_herocircle_self", team=2, yaw=(c * 13) % 360, unitname="npc_dota_hero_juggernaut", visionrange=1200)
            for h, f in ALLY.items():
                ax, ay = f(c, (x, y))
                put(xpos=ax, ypos=ay, image="minimap_herocircle", team=2, yaw=90, unitname=f"npc_dota_hero_{h}", visionrange=1200)
            for h, segs in ENEMY.items():
                if p := _at(segs, c):
                    put(xpos=p[0], ypos=p[1], image="minimap_enemyicon", team=3, yaw=200, unitname=f"npc_dota_hero_{h}", visionrange=1800)  # real: 1800 day/800 night; 0 = death marker
            if 995 <= c < 1005 or 1795 <= c < 1800:
                px, py = TOP_PIT if c < 1500 else BOT_PIT
                put(xpos=px, ypos=py, image="minimap_creep", team=4, yaw=347, unitname="npc_dota_roshan", visionrange=1400)
            put(xpos=4900, ypos=-6100, image="minimap_tower90", team=2, yaw=0, unitname="npc_dota_goodguys_tower1_bot", visionrange=1900)
            put(xpos=x + 300, ypos=y, image="minimap_creep", team=2, yaw=0, unitname="npc_dota_creep_goodguys_melee", visionrange=0)
        ev = []
        if 2 <= c < 5:
            ev.append({"game_time": 182, "event_type": "bounty_rune_pickup", "player_id": 1, "team": "radiant", "bounty_value": 40, "team_gold": 200})
        for kc in (1080, 1800):
            if kc <= c < kc + 4:
                ev.append({"game_time": kc + 180, "event_type": "roshan_killed", "killed_by_team": "dire", "killer_player_id": 7})
            if kc + 5 <= c < kc + 8:
                ev.append({"game_time": kc + 185, "event_type": "aegis_picked_up", "player_id": 7, "snatched": False})
        inv = (["tango", "flask", "quelling_blade", "branches"] if c < 300 else ["phase_boots", "quelling_blade", "wraith_band", "magic_wand"]
               if c < 900 else ["phase_boots", "bfury", "wraith_band", "magic_wand", "yasha"] if c < 1800
               else ["phase_boots", "bfury", "manta", "magic_wand", "black_king_bar"])
        items = {f"slot{j}": {"name": f"item_{n}", "purchaser": 1, "can_cast": True, "cooldown": 0, "passive": False}
                 for j, n in enumerate(inv)}
        items |= {f"slot{j}": {"name": "empty"} for j in range(len(inv), 9)} | {f"stash{j}": {"name": "empty"} for j in range(6)}
        items |= {"teleport0": {"name": "item_tpscroll", "purchaser": 1, "can_cast": True, "cooldown": 0, "charges": 1},
                  "neutral0": {"name": "item_keen_optic" if c >= 420 else "empty", "purchaser": -1, "passive": True}, "neutral1": {"name": "empty"}}
        body = {
            "provider": {"name": "Dota 2", "appid": 570, "version": 47, "timestamp": 1794243000 + t},
            "map": {"name": "start", "matchid": "8512345678", "game_time": cl + 180, "clock_time": cl, "daytime": c < 0 or (c // 300) % 2 == 0,
                    "nightstalker_night": False, "radiant_score": c // 120 if c > 0 else 0, "dire_score": c // 100 if c > 0 else 0,
                    "game_state": f"DOTA_GAMERULES_STATE_{state}", "paused": False, "win_team": "none", "customgamename": "",
                    "ward_purchase_cooldown": 0},
            "player": {"steamid": "76561190000000001", "accountid": "100000001", "name": "sim", "activity": "playing",
                       "kills": c // 200 if c > 0 else 0, "deaths": sum(c >= a for a, _ in DEATHS), "assists": c // 150 if c > 0 else 0,
                       "last_hits": max(0, c * 6 // 60 - 2), "denies": max(0, c // 90), "kill_streak": 0, "commands_issued": 10 * t,
                       "kill_list": {}, "team_name": "radiant", "player_slot": 1, "team_slot": 1, "gold": int(600 + max(c, 0) * 1.3 * k2) % (2400 * k2) + 150,
                       "gold_reliable": 200, "gold_unreliable": 400, "gpm": 450 * k2, "xpm": 420 * k2,
                       "gold_from_income": round(3 * max(c, 0)), "gold_from_creep_kills": 45 * k2 * max(0, c * 6 // 60 - 2)},  # ~45 g/LH, x2 Turbo  # real: ~3 g/s passive, x2 in Turbo (auto-detect)
            "hero": {"facet": 2, "xpos": x, "ypos": y, "id": 8, "name": "npc_dota_hero_juggernaut", "level": lvl, "xp": 300 * lvl,
                     "alive": not dead, "respawn_seconds": _dead(c) - c if dead else 0, "buyback_cost": 200 + nw // 13, "buyback_cooldown": 0,
                     "health": hp * 17, "max_health": 1700, "health_percent": hp, "mana": 300, "max_mana": 500, "mana_percent": 60,
                     "silenced": False, "stunned": False, "disarmed": False, "magicimmune": False, "hexed": False, "muted": False,
                     "break": False, "aghanims_scepter": False, "aghanims_shard": False, "smoked": False, "permanent_buffs": {},
                     "has_debuff": False, **{f"talent_{j}": False for j in range(1, 9)}, "attributes_level": 0},
            "abilities": {f"ability{j}": {"name": n, "level": min(4, lvl // 2) if j < 3 else lvl // 6, "can_cast": True, "passive": j == 2,
                                          "ability_active": True, "cooldown": 0, "ultimate": j == 3}
                          for j, n in enumerate(["juggernaut_blade_fury", "juggernaut_healing_ward", "juggernaut_blade_dance", "juggernaut_omni_slash"])},
            "items": items,
            "buildings": {"radiant": {"dota_goodguys_tower1_bot": {"health": 1800 - min(c, 900), "max_health": 1800},
                                      "dota_goodguys_tower1_top": {"health": 1800, "max_health": 1800}}},
            "minimap": mm, "draft": {}, "events": ev,
            "previously": {"map": {"clock_time": cl - 1}, "hero": {"health_percent": hp + 1}} if c > -90 else {},
        }
        yield {"t": t, "body": body}


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).close()


def play(rows, speed, send):
    """Call send(body) for each {"t", "body"} row, keeping original timing / speed."""
    t0, first = time.time(), None
    for row in rows:
        first = row["t"] if first is None else first
        if (wait := t0 + (row["t"] - first) / speed - time.time()) > 0:
            time.sleep(wait)
        send(row["body"])


def _poster(port):
    url, tok = f"http://127.0.0.1:{port}/gsi", token()

    def send(body):
        try:
            post(url, body | {"auth": {"token": tok}} if tok else body)
        except OSError as e:
            raise SystemExit(f"app not reachable at {url}: {e}")
    return send


def sim(a):
    rows = list(timeline(a.minutes, a.turbo))
    out = ROOT / ("fixtures/sim.jsonl" if a.minutes == 25 and not a.turbo else f"fixtures/sim_{a.minutes}m{'_turbo' if a.turbo else ''}.jsonl")
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    print(f"wrote {out} ({len(rows)} payloads, {rows[-1]['t'] / a.speed:.0f} s at --speed {a.speed:g})")
    if not a.write_only:
        play(rows, a.speed, _poster(a.port))


def _open(p):
    """Text reader for X.jsonl or its compacted X.jsonl.gz (postgame.compact); either path may be given."""
    p = Path(p)
    if not p.exists() and p.with_name(p.name + ".gz").exists():
        p = p.with_name(p.name + ".gz")
    return gzip.open(p, "rt", encoding="utf-8", errors="replace") if p.suffix == ".gz" else open(p, encoding="utf-8", errors="replace")


def rows_from(path):
    with _open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def replay(a):
    play(rows_from(a.file), a.speed, _poster(a.port))


def _vision():
    sys.path.insert(0, str(ROOT))
    import vision
    return vision


# ---- recordings: sessions / review / fetch-match ----------------------------------------------------------------
REC = ROOT / "recordings"


def _jsonl(p):
    """Rows of a JSONL file (or its .gz); a torn or junk line is skipped (crash-safe readers)."""
    out = []
    try:
        with _open(p) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except (OSError, EOFError):  # EOFError: truncated .gz
        pass
    return out


def _meta(d):
    try:
        return json.loads((Path(d) / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _du(d):
    return sum(f.stat().st_size for f in Path(d).rglob("*") if f.is_file()) / 1e6


def sessions(a):
    root = Path(a.dir)
    rows = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    print(f"{'session':<34} {'dur':>6} {'hero':<18} {'mode':<6} {'MB':>7} {'frames':>6} {'marks':>5}")
    for d in rows:
        m = _meta(d)
        dur = (m.get("end") or m.get("updated") or 0) - (m.get("start") or 0) if m.get("start") else None
        fr = sum(1 for _ in (d / "frames").glob("*")) if (d / "frames").is_dir() else 0
        print(f"{d.name:<34} {(f'{dur / 60:.0f}m' if dur else '?'):>6} {str(m.get('hero') or '?'):<18} {str(m.get('mode') or '?'):<6} "
              f"{_du(d):>7.1f} {fr:>6} {(m.get('counts') or {}).get('marks', 0):>5}")


def _pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(q * len(v)))] if v else None


def review(a):
    """Session diagnostic -> DIR/review.md (and stdout); the match report is postgame's report.md."""
    import world
    d = Path(a.dir)
    m, ev, wr, cv = _meta(d), _jsonl(d / "events.jsonl"), _jsonl(d / "world.jsonl"), _jsonl(d / "cv.jsonl")
    gsi_n = len(_jsonl(d / "gsi.jsonl"))
    mm = world.mmss
    clk = lambda c: mm(c) if isinstance(c, (int, float)) else "  ?  "
    dur = (m.get("end") or m.get("updated") or 0) - (m.get("start") or 0) if m.get("start") else None
    L = [f"# Session {d.name}", "",
         f"- match {m.get('matchid') or '?'}, {m.get('hero') or '?'} ({m.get('team') or '?'}), mode {m.get('mode') or '?'}, "
         f"role {(m.get('settings') or {}).get('role') or '?'}",
         f"- app {(m.get('app') or {}).get('version')} src {(m.get('app') or {}).get('src_hash')}, started {m.get('start_iso')}, "
         f"wall {f'{dur / 60:.1f} min' if dur else '?'}{'' if m.get('end') else ' (no clean exit: crash or still running)'}",
         f"- rows: gsi {gsi_n}, world {len(wr)}, cv {len(cv)}, events {len(ev)}; {_du(d):.0f} MB on disk; "
         f"dropped {(m.get('counts') or {}).get('dropped', 0)}; frames paused: {(m.get('disk') or {}).get('frames_paused') or 'no'}", ""]
    # timeline: alerts (no timer reminders), warn/high attention bumps, my deaths, feedback marks
    T, timers = [], 0
    for e in ev:
        k = e.get("kind")
        if k == "alert" and e.get("op") in ("new", "escalate"):
            if e.get("rule") == "T":
                timers += 1
                continue
            T.append((e.get("clock"), f"{e['level']:<4} {e.get('rule'):<5} {e.get('text')}{' (escalated)' if e['op'] == 'escalate' else ''}"))
        elif k == "attention" and e.get("level") in ("warn", "high") and e.get("sticky"):
            T.append((e.get("clock"), f"ATTN #{e.get('seq')} {e.get('word')} ({e.get('level')}): {e.get('text')}"))
        elif k == "feedback":
            T.append((e.get("clock"), f"MARK {e.get('fb')} #{e.get('n')}: {e.get('text') or ''}"))
        elif k in ("capture", "frames_paused") or (k == "log" and e.get("level") in ("ERROR", "CRITICAL")):
            T.append((e.get("clock"), f"{k.upper()} {e.get('op') or e.get('why') or e.get('msg')}"))
    alive = None
    for r in wr:
        me = r.get("me") or {}
        if alive is not None and me.get("alive") is not None and me["alive"] != alive:
            T.append(((r.get("game") or {}).get("clock"), "ME   died" if not me["alive"] else "ME   respawned"))
        alive = me.get("alive") if me.get("alive") is not None else alive
    T.sort(key=lambda x: x[0] if isinstance(x[0], (int, float)) else -1e9)
    L += [f"## Timeline ({len(T)} entries; {timers} timer reminders not listed)", "```"] + [f"{clk(c)}  {t}" for c, t in T] + ["```", ""]
    marks = [e for e in ev if e.get("kind") == "feedback"]
    L.append(f"## Feedback marks ({len(marks)})")
    for e in marks:
        at, dc = e.get("attention") or {}, e.get("decision") or {}
        act = dc.get("action") or {}
        L.append(f"- {clk(e.get('clock'))} **{e.get('fb')}** {e.get('text') or ''} | attention {at.get('word')} ({at.get('level')}) | "
                 f"jev {act.get('choice')} {act.get('p')} | alerts: {'; '.join(x.get('text', '') for x in e.get('alerts') or []) or '-'}")
    # CV
    L += ["", "## CV"]
    if cv:
        ms = [r["ms"] for r in cv if isinstance(r.get("ms"), (int, float))]
        lock = next((r for r in cv if (r.get("lineup") or {}).get("locked")), None)
        dets = [len(r.get("minimap") or []) for r in cv if r.get("frame_ok")]
        gaps = [(b.get("clock"), b["w"] - a_["w"]) for a_, b in zip(cv, cv[1:]) if b.get("w") and a_.get("w") and b["w"] - a_["w"] > 2]
        span = (cv[-1].get("w") or 0) - (cv[0].get("w") or 0)
        sc = (lock or {}).get("lineup", {}).get("scores") or []
        errs = [x for r in wr if (x := (r.get("cv") or {}).get("gsi_vs_cv_err_u")) is not None]
        L += [f"- frames {len(cv)} over {span / 60:.1f} min ({len(cv) / span if span else 0:.1f} fps), frame_ok {len(dets) / len(cv):.0%}, "
              f"ms p50 {_pct(ms, .5)} p95 {_pct(ms, .95)}",
              f"- lineup locked at {clk(lock.get('clock')) if lock else 'never'}" + (f", scores min {min(sc):.2f} mean {statistics.fmean(sc):.2f}" if sc else ""),
              f"- detections/frame mean {statistics.fmean(dets) if dets else 0:.2f}, max {max(dets) if dets else 0}",
              f"- gsi_vs_cv_err_u: last {errs[-1] if errs else None}, median {statistics.median(errs) if errs else None}",
              f"- capture gaps > 2 s: {len(gaps)}, total {sum(g for _, g in gaps):.0f} s" +
              "".join(f"; {clk(c)} {g:.0f}s" for c, g in sorted(gaps, key=lambda x: -x[1])[:5])]
    else:
        L.append("- no cv.jsonl rows (--no-cv, no Screen Recording permission, or no Dota window)")
    # Jev / LLM
    L += ["", "## Jev / LLM"]
    for name in ("jev", "llm"):
        req = [e for e in ev if e.get("kind") == f"{name}_req"]
        resp = [e for e in ev if e.get("kind") == f"{name}_resp"]
        lat = [e["ms"] for e in resp if isinstance(e.get("ms"), (int, float))]
        L.append(f"- {name}: {len(req)} requests, {sum(1 for e in resp if e.get('ok'))} ok, {sum(1 for e in resp if not e.get('ok'))} failed, "
                 f"{sum(1 for e in resp if e.get('late'))} late; latency p50 {_pct(lat, .5)} p95 {_pct(lat, .95)} ms; "
                 f"${sum(e.get('usd') or 0 for e in resp):.4f}")
    logs = [e for e in ev if e.get("kind") == "log"]
    L += ["", f"## Warnings/errors ({len(logs)})"] + [f"- {x}" for x in list(dict.fromkeys(f"{e.get('level')} {e.get('msg')}" for e in logs))[:15]]
    fr = {}
    for f in (d / "frames").glob("*"):
        k = f.name.split("_")[0]
        n, b = fr.get(k, (0, 0))
        fr[k] = (n + 1, b + f.stat().st_size)
    L += ["", "## Frames"] + [f"- {k}: {n} files, {b / 1e6:.1f} MB" for k, (n, b) in sorted(fr.items())]
    out = "\n".join(L) + "\n"
    (d / "review.md").write_text(out)
    print(out)


def fetch_match(a):
    """OpenDota match JSON -> DIR/opendota_match.json; asks OpenDota to parse it if it isn't yet."""
    import data
    d = Path(a.dir)
    mid = str(_meta(d).get("matchid") or (d.name.split("_")[1] if "_" in d.name else ""))
    if not mid.isdigit() or mid == "0":
        raise SystemExit(f"no matchid for {d} (meta.json or <ts>_<matchid> dir name)")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{data.OD}/matches/{mid}", headers={"User-Agent": "live-coach"}), timeout=20) as r:
            m = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"OpenDota GET match {mid}: HTTP {e.code} (too fresh? try again in a few minutes)")
    (d / "opendota_match.json").write_text(json.dumps(m))
    parsed = m.get("version") is not None
    print(f"wrote {d / 'opendota_match.json'} (duration {m.get('duration')} s, game_mode {m.get('game_mode')}, parsed {parsed})")
    if not parsed:
        req = urllib.request.Request(f"{data.OD}/request/{mid}", b"", {"User-Agent": "live-coach"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"requested parse: {r.read()[:200].decode(errors='replace')} (re-run fetch-match later for the parsed match)")


def postgame_cmd(a):
    import postgame
    data.load_static()  # hero roles / item costs (cached)
    g = postgame.matches(a.root)
    if a.dirs:
        dirs = sorted({x for d in a.dirs for x in postgame.matches(Path(d).parent).get(postgame.matchid_of(d), [Path(d)])})
    elif g:
        dirs = max(g.values(), key=lambda v: v[-1].name)
    else:
        raise SystemExit(f"no recorded matches under {a.root}")
    r = postgame.analyze(dirs, llm_call=not a.no_llm)
    c = postgame.compact(dirs) if r.get("path") and not a.no_compact else {"skip": "--no-compact" if r.get("path") else "analysis failed"}
    out = Path(dirs[0]).parent / c["target"] / "report.md" if c.get("target") else Path(r["path"]) if r.get("path") else None
    print(out.read_text() if out else r)
    print(f"compact: {c}")


def compact_cmd(a):
    """Compact analyzed matches (deletes files): dry run unless --yes. Unanalyzed matches are analyzed first (no LLM) with --yes."""
    import postgame
    data.load_static()
    g = postgame.matches(a.root)
    if a.dirs:
        g = {m: g[m] for m in {postgame.matchid_of(d) for d in a.dirs} if m in g}
    elif not a.all:
        done = [(m, v) for m, v in g.items() if not any(postgame.live(d) for d in v)]
        g = dict([max(done, key=lambda kv: kv[1][-1].name)]) if done else {}
    for mid, dirs in g.items():
        if a.yes and not any(postgame.live(d) for d in dirs) and postgame._load(dirs[-1] / "report.json").get("matchid") != mid:
            postgame.analyze(dirs, llm_call=False)
        print(f"{mid}: {'' if a.yes else '(dry run) '}{postgame.compact(dirs, dry=not a.yes)}")
    if not a.yes:
        print("nothing deleted; re-run with --yes to compact")


# ---- selfcheck --------------------------------------------------------------------------------------------------
CRASH = """import sys, time
sys.path.insert(0, sys.argv[2])
import numpy as np, rec
print(rec.start(sys.argv[1], {"test": "crash"}), flush=True)
big = {"pad": "x" * 20000}
for i in range(10 ** 9):
    rec.gsi({"i": i, **(big if i % 7 == 0 else {})})
    rec.event("tick", i=i)
    if i % 20 == 0:
        rec.frame("mm", np.zeros((40, 40, 3), np.uint8))
    time.sleep(0.0005)
"""


def _mock_jev():
    """Local stand-in for api.typesafe.ai: answers every question deterministically, records the last request."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.update(body=body, auth=self.headers.get("Authorization"))
            ans = {}
            for n, q in body["questions"].items():
                if q["type"] == "noul":
                    ans[n] = {"type": "noul", "noul": 0.7 if n == "fight" else 0.1}
                elif q["type"] == "score":
                    ans[n] = {"type": "score", "score": 1.2, "confidence": 0.5, "legend": {}, "probabilities": {}}
                else:
                    k = "retreat" if n == "action" else "base" if n == "where" else next(iter(q["criteria"]))
                    ans[n] = {"type": "choice", "choice": k, "confidence": 0.8, "probabilities": {k: 0.8}}
            out = json.dumps({"model": "jev-mock", "answers": ans, "usage": {"input_tokens": 1000, "output_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _v2_checks(check):
    """Coach v2 on synthetic worlds: the new rules, the card word, Turbo re-check, the precision gate, and the whole pipeline
    (rules -> coach -> voice) on the sim timeline: budget, fight mode, debrief, goal. No audio, no network."""
    import coach, postgame, rec, rules, voice, world  # noqa: F401
    IP = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"

    def scene(c=900, hp=90, pos=(0, 0), turbo=False, **heroes):
        """A quiet mid-game world; heroes: name=(ally?, x, y) or (ally?, x, y, 'dead' | 'mia')."""
        world.reset()
        W = world.W
        W["settings"] = {"role": 5, "bracket": None, "turbo": turbo, "turbo_match": "always"}
        world.tick(1e9)
        W["game"].update(clock=c, state=IP, matchid="t", daytime=True)
        W["me"].update(hero="lich", team="radiant", alive=True, hp_pct=hp, x=pos[0], y=pos[1], role=5)
        for s_, (ally, x, y, *st) in heroes.items():
            e = world._hero(s_, "radiant" if ally else "dire")
            e.update(visible=not st, x=x, y=y, last_xy=[x, y], last_seen=c, mia_s=20 if st == ["mia"] else 0, dead=st == ["dead"], reach_u=6000,
                     vanished_at=c - 20 if st == ["mia"] else None)
        W["hud"] = {"threats": [], "fight": None, "mia": []}
        rules._history(W, c)
        return W

    def has(W, rule):
        return next((a for a in W["alerts"] if a["rule"] == rule), None)

    def later(W, c, **me):
        W["game"]["clock"] = c
        W["me"].update(me)
        rules._history(W, c)

    lane = (2000, -6300)  # radiant safe lane
    W = scene(200, 80, lane, lion=(False, 2600, -6300), weaver=(False, 2700, -6200), lina=(True, 1900, -6300))
    later(W, 204, hp_pct=58)
    rules.pressure(W, 204)
    a, d0 = has(W, "R13"), rules.danger(W, 204)
    W = scene(900, 80, lane, lion=(False, 2600, -6300))
    later(W, 904, hp_pct=58)
    rules.pressure(W, 904)
    b = has(W, "R13")
    W = scene(200, 45, lane, lion=(False, 2600, -6300), weaver=(False, 2700, -6200))
    rules.pressure(W, 200)
    check("R13 laning pressure: 60% HP and dropping fast, or 50% and outnumbered; laning only; lane opponents are not a gank",
          a and a["level"] == "warn" and not b and has(W, "R13") and d0 == 0, f"{a and a['text']} | mid game: {b} | D {d0}")
    W = scene(200, 90, lane, lion=(False, 2600, -6300), weaver=(False, 2700, -6200), sven=(False, 2500, -5600), lina=(True, 1900, -6300))
    d3 = rules.danger(W, 200)
    dirs = []
    for foes in (((-1000, -1000), (-900, -1200)), ((1000, -1000), (1200, -900)), ((-200, 1800), (-100, 1900))):
        W = scene(900, 90, (0, 0), lion=(False, *foes[0]), sven=(False, *foes[1]))
        rules.danger(W, 900)
        dirs.append((has(W, "R1") or {}).get("dir"))
    check("R1: a third enemy in my lane is a gank; the alert carries where the threat is (behind / river / jungle)",
          d3 >= 1.5 and dirs == ["behind", "river", "jungle"], f"D {d3}, dirs {dirs}")
    W = scene(898, 90, (-300, 0), lina=(True, 3000, 0), lion=(False, 3300, 0), sven=(False, 3400, 200), axe=(False, 2800, 300))
    later(W, 900, x=0)
    rules.chain_feed(W, 900)
    r15 = has(W, "R15")
    rules.join_fight(W, 900)
    r12a = has(W, "R12")
    got = []
    for c, hp, extra in ((900, 90, {}), (200, 90, {}), (900, 55, {}), (900, 90, {"zeus": (False, 2700, 200)}), (900, 90, {"retreat": 1}), (900, 90, {"r1": 1})):
        hs = {k: v for k, v in extra.items() if isinstance(v, tuple)}
        W = scene(c, hp, (0, 0), lina=(True, 2500, 0), tiny=(True, 2600, 100), lion=(False, 3000, 200), sven=(False, 2900, -200), **hs)
        if "retreat" in extra:
            W["decision"] = {"at_clock": c, "action": {"choice": "retreat", "p": 0.7}}
        if "r1" in extra:
            rules.alert(W, "R1", "warn", "x", 20)
        rules.join_fight(W, c)
        got.append(bool(has(W, "R12")))
    check("R15 chain-feed guard (walking into a lost fight) and no JOIN next to it; R12 only mid/late, HP >= 60%, allies >= enemies, "
          "no retreat alert or decision", r15 and r15["level"] == "high" and not r12a and got == [True, False, False, False, False, False],
          f"{r15 and r15['text']} | R12 {got}")
    W = scene(900, 90, (0, 0), lion=(False, 0, 0, "dead"), sven=(False, 0, 0, "dead"), axe=(False, 0, 0, "dead"), lina=(True, 500, 0))  # R14 needs an ally near
    W["_"]["sc"] = [(870, 0, 0), (899, 3, 0)]  # the kills just happened (R14 only right after them)
    rules.objective(W, 900)
    r14 = has(W, "R14")
    W = scene(1200, 90, (3000, 3000), lion=(False, 3200, 3100))  # this game's feedback: one enemy is not a gank; a respawn is not "missing";
    W["game"]["daytime"] = False                                 # no TP -> one call; parts of an item I hold -> that item first
    rules.danger(W, 1200)
    one = has(W, "R1")
    win = []  # [(clock, mine, theirs)] kill-score history -> winning() at 1200
    for h in ([(1180, 5, 5), (1192, 7, 5)], [(1180, 5, 5), (1192, 6, 5), (1195, 6, 6)], [(1170, 5, 5), (1180, 6, 5)]):
        W["_"]["sc"] = h
        win.append(rules.winning(W, 1200))
    W["_"]["sc"] = [(1180, 5, 5), (1192, 7, 5)]
    rules.alert(W, "R1", "high", "x", 20)
    W["game"]["score"], W["me"]["team"] = [7, 5], "radiant"
    rules.danger(W, 1200)
    check("winning a fight (more kills than losses in 15 s) stops the retreat call; an even trade or an old win does not",
          win == [True, False, False] and not has(W, "R1"), str(win))
    world.reset()  # GSI minimap (game 9087074099): an enemy's last-seen marker comes with OUR team; TP pings are destinations
    mm = lambda img, t, x, u="juggernaut": {"xpos": x, "ypos": 0, "image": img, "team": t, "unitname": f"npc_dota_hero_{u}"}  # noqa: E731
    for i, m in enumerate(({"a": mm("minimap_enemyicon", 3, 100)}, {"a": mm("minimap_heroimage", 2, 900), "b": mm("minimap_enemyicon", 3, 200),
                                                                     "c": mm("minimap_ping_teleporting", 2, 5000, "lina")})):
        world.merge_gsi({"player": {"activity": "playing", "team_name": "radiant"}, "hero": {"name": "npc_dota_hero_lich", "xpos": 0, "ypos": 0},
                         "map": {"clock_time": 600 + i, "matchid": "1"}, "minimap": m})
    j = world.W["heroes"]["juggernaut"]
    check("GSI minimap: a wrong-team marker never turns an enemy into an ally (Juggernaut + Pudge counted as allies in fights); "
          "a TP ping is not a sighting", j["ally"] is False and j["x"] == 200 and "lina" not in world.W["heroes"], f"{j['ally']} {j['x']}")
    W = scene(1200, 90, (0, 0), lion=(False, 500, 0, "mia"), sven=(False, 500, 0, "mia"))
    world._hero("sven", "dire").update(vanished_at=None)  # sven respawned (never seen walking into fog)
    rules.danger(W, 1200)
    marks, n_miss = [m["hero"] for m in W["hud"]["mia"]], rules._missing(W)
    W["me"].update(tp=False, gold=500, items=["point_booster", "staff_of_wizardry", "boots", None, None, None])
    rules.no_tp(W, 1200), rules.no_tp(W, 1244), rules.no_tp(W, 1245)  # 45 s: habits.grace("tp") with no history
    voice._reset("t")
    old, data.S["items"] = data.S["items"], {"ultimate_scepter": {"cost": 4200, "components": ["point_booster", "staff_of_wizardry", "ogre_axe"]},
                                             "point_booster": {"cost": 1200}, "staff_of_wizardry": {"cost": 1000}, "boots": {"cost": 500},
                                             "tranquil_boots": {"cost": 900, "components": ["boots", "wind_lace"]}}
    tp, tp_cue, wip = has(W, "R16"), "tp" in [x["cue"] for x in voice.cues(W, 1245)], rules._building(W)
    data.S["items"] = old
    W["me"].update(tp=True)
    rules.no_tp(W, 1250)
    W["me"].update(x=world.FOUNTAIN["radiant"][0], y=world.FOUNTAIN["radiant"][1])  # "You can buy" is said only in base (or dead)
    W["skill"] = {"points": 1, "src": "rule", "next": {"name": "lich_frost_nova", "label": __import__("skills").label("lich_frost_nova", "lich"), "slot": "Q"}}
    W["advice"] = {"next_items": [{"name": "Glimmer Cape", "key": "glimmer_cape", "cost": 2150, "affordable": True}]}
    guess = [x["cue"] for x in voice.cues(W, 1231)]  # the fallback rule's basic-spell guess is not spoken; the can-afford hint is
    W["skill"]["src"] = "llm"
    early = "skill" in [x["cue"] for x in voice.cues(W, 1232)]  # habits.grace("skill"): time to level it alone first
    hints = {x["cue"]: x["texts"][0] for x in voice.cues(W, 1232 + __import__("habits").grace("skill")) if x["cue"] in voice.LIGHT}
    check("hints: 'Level up. Take <spell>.' only from the hero's build order (or the ultimate) and only after the habit grace, 'You can buy <item> now.' when affordable",
          "skill" not in guess and "buy" in guess and not early and hints == {"skill": "Level up. Take Frost Nova.", "buy": "You can buy Glimmer Cape now."},
          f"{guess} {hints}")
    check("one enemy near me is not a gank; only a hero seen walking into fog is missing / marked; no TP for 45 s -> one spoken call; "
          "the item I hold parts of comes first", not one and marks == ["lion"] and n_miss == 1 and bool(tp) and tp["level"] == "info"
          and tp_cue and not has(W, "R16") and wip[:1] == ["ultimate_scepter"], f"{one} {marks} {n_miss} {tp} {tp_cue} {wip}")
    W = scene(900, 90, (2000, 2000), lion=(False, 0, 0, "mia"), sven=(False, 0, 0, "mia"), axe=(False, 0, 0, "mia"))
    rules.missing(W, 900)
    r2 = has(W, "R2")
    W["_"]["soft"] = W["_"]["eased_now"] = {"R11"}  # eased by "bad call": even a forced R11 stays quiet
    W["me"].update(x=4500, y=3500)
    rules.overextend(W, 900)
    r11 = has(W, "R11")
    voice._reset("t")
    cues = [x["cue"] for x in voice.cues(W, 900)]
    (world.DATA / "profile").mkdir(parents=True, exist_ok=True)
    (world.DATA / "profile" / "profile.json").write_text(json.dumps({"games": 3, "death_zones": {}, "mistakes": {"deaths_unwarned": 0, "deaths_3plus_missing": 0}, "alerts": {
        "R1": {"episodes": 12, "useful": 9, "false_rate": 0.1, "response_rate": 0.5}, "R10": {"episodes": 12, "useful": 4, "false_rate": 0.5, "response_rate": 0.5},
        "R4": {"episodes": 3, "useful": 0, "false_rate": 1, "response_rate": 0}}}))
    demote = postgame.rule_tuning().get("demote")
    (world.DATA / "profile" / "profile.json").unlink()
    check("R14 objective window on 3 dead; R2 missing is info (amber); a rule eased by voice or demoted by the precision gate "
          "(>= 10 episodes, < 50% useful) is info and has no voice cue",
          r14 and r14["level"] == "warn" and r2 and r2["level"] == "info" and r11 and r11["level"] == "info" and r11.get("soft")
          and "too_deep" not in cues and demote == ["R10"], f"R2 {r2 and r2['level']}, R11 {r11 and r11['level']}, cues {cues}, demote {demote}")
    W = scene(900, 90, (0, 0))
    words = []
    for p_ in (0.5, 0.7):
        W["decision"] = {"at_clock": 900, "action": {"choice": "ward", "p": p_, "probs": {}}, "where": {"choice": "river", "p": 0.9},
                         "danger": {"score": 1, "conf": 0.5}, "rosh_taken": {"p": 0.1}, "fight": {"p": 0.2}, "next_item": None, "target": None}
        W["alerts"] = []
        rules.advice(W, 900, 0.0, [])
        rules.attention(W, 900)
        rules.glance(W, 900, 0.0)
        words.append((W["glance"]["action"]["word"], W["glance"]["action"]["src"], W["attention"]["word"], W["attention"]["text"], W["advice"]["do_now"]))
    txt = json.dumps(words)
    check("card word: the decision model's only at confidence >= 0.6, else the phase default; no model name or percentage on screen",
          words[0][:3] == ("GROUP", "phase", None) and words[1][:3] == ("WARD", "model", "WARD") and "Jev" not in txt and "%" not in txt, txt[:200])
    world.reset()
    W, me, got = world.W, world.W["me"], []
    for clock, lh, cg in ((200, 6, 310), (470, 16, 1222), (500, 20, 1000)):  # the recorded game the one-shot check got wrong
        world._auto_detect({"last_hits": lh, "gold_from_creep_kills": cg, "xpm": 400}, me, clock)
        got.append(W["_"]["turbo_auto"])
    got.append((world.turbo_guess(29, 1784, 528, 480, 41), world.turbo_guess(29, 1784, 528, 480), world.turbo_guess(29, 1784, 528, 480, 80)))
    names = []
    for tb in (False, True):
        W = scene(800, turbo=tb)
        rules.timers(W, 100)
        tor = next(t["at_clock"] for t in W["timers"] if t["name"] == "Tormentor")
        rules.timers(W, 800)
        names.append(("Stack" in {t["name"] for t in W["timers"]}, tor))
    check("Turbo: auto-detect re-checks until 8:00, then holds; Stack cut-off and Tormentor follow the mode",
          got == [False, True, True, (True, False, False)] and names == [(True, 900), (False, 450)], f"{got} {names}")

    # the whole pipeline on the sim: every bank line has a clip (steady state), dry
    saved = (voice.DRY, voice.GEN, voice.find, voice.secs, rec.event, voice._comm_enabled)
    evs, fails, seen = [], [], set()
    voice.DRY, voice.GEN, voice.find, voice.secs = True, False, (lambda text, tier=None: f"sim/{voice.slug(text)}"), (lambda key: 1.6)
    voice._comm_enabled = lambda: False
    rec.event = lambda kind, **kw: evs.append({"kind": kind, "clock": world.W["game"]["clock"], **kw})
    try:
        world.reset()
        world.W["settings"] = {"role": 1, "bracket": None, "turbo": False}
        for row in timeline():
            for q in (0, 0.5):  # 2 ticks per GSI second
                now = 1e9 + row["t"] + q
                if not q:
                    world.merge_gsi(row["body"], now=now)
                world.tick(now)
                try:
                    rules.evaluate(world.W, now)
                    coach.tick(world.W)
                    voice.tick(world.W)
                    seen |= {k for k in ("goal", "brief", "debrief") if world.W.get(k)}
                    json.loads(world.dumps())
                except Exception as e:
                    fails.append(repr(e))
        W = world.W
        hits = [e for e in evs if e["kind"] == "voice" and e["op"].endswith("hit")]
        nu = [e for e in hits if e["tier"] in ("C", "P", "K") and e["clock"] > 0]
        gaps = [b["clock"] - a["clock"] for a, b in zip(nu, nu[1:])]
        perm = max((sum(1 for x in hits if e["clock"] - 60 < x["clock"] <= e["clock"] and x["tier"] != "U") for e in hits), default=0)
        dbs = [e for e in evs if e["kind"] == "debrief"]
        check("sim through rules + coach + voice: no tick failure, pre-horn brief, no non-urgent line in fight mode, >= 20 s between "
              "calls, cap respected, at most 4 lines a minute, the death is debriefed, goal counted",
              not fails and [e["tier"] for e in hits[:2]] == ["B", "B"] and not [e for e in hits if e["fight"] and e["tier"] != "U"]
              and min(gaps, default=99) >= 20 and len(nu) <= voice.CAP["normal"] and perm <= 4 and len(dbs) == 1 and dbs[0]["cause"] in coach.CAUSES
              and any(e["tier"] == "D" and e["cue"].startswith("cause:") for e in hits) and seen == {"goal", "brief", "debrief"}
              and W["goal"]["now"] == 1 and W["goal"]["target"] == 6 and W["coach"]["spoken"] == voice.S["n_nu"] and W["history"]["games"] == []
              and set(W["ready"]) == {"gsi", "screen", "voice", "mic", "overlay"},
              f"{len(hits)} lines ({dict((t, sum(e['tier'] == t for e in hits)) for t in 'UCBDPK')}), min gap {min(gaps, default=None)}, "
              f"debrief {dbs and dbs[0]['cause']}, fails {fails[:2]}")
    finally:
        voice.DRY, voice.GEN, voice.find, voice.secs, rec.event, voice._comm_enabled = saved
        voice._reset()


def _fog_checks(check):
    """hud.fog (fog.py) and the enemy death rules (world.py) on the sim timeline and on hand-made payloads; the failure guards.
    The sim player is Radiant, so enemy player ids are 5-9. No audio, no network."""
    import copy, logging, fog, rules, world
    KEYS = {"hero", "slot", "age", "x", "y", "conf", "src", "p10", "tier", "ttl"}
    base = next(r["body"] for r in timeline() if (r["body"].get("map") or {}).get("clock_time") == 100)
    foe = lambda h, x, y, vr=1800: {"xpos": x, "ypos": y, "image": "minimap_enemyicon", "team": 3, "unitname": f"npc_dota_hero_{h}", "visionrange": vr}  # noqa: E731 (vr 0 = corpse marker)
    kill = lambda t, pid, data=None: {"event_type": "generic_event", "game_time": t + 180,  # noqa: E731
                                      "data": data or json.dumps({"type": "CHAT_MESSAGE_HERO_KILL", "time": t, "playerid1": pid, "playerid2": 1})}

    def step(c, mm=(), events=(), alive=True):
        """One payload at game clock c with me at (0, 0) and `mm` as the whole minimap, then tick + evaluate -> (accepted, {hero: fog entry})."""
        b = copy.deepcopy(base)
        b["map"] |= {"clock_time": c, "game_time": c + 180}
        b["hero"] |= {"xpos": 0, "ypos": 0, "alive": alive}
        b["minimap"], b["events"] = {f"o{i}": o for i, o in enumerate(mm)}, list(events)
        ok = world.merge_gsi(b, now=1e9 + c)
        world.tick(1e9 + c)
        rules.evaluate(world.W, 1e9 + c)
        return ok, {o["hero"]: o for o in world.W["hud"]["fog"]}

    def sim(raise_in_fog=False):
        """The 25-minute sim: contract violations, first alert times, fog.build cost, largest hud.fog."""
        world.reset()
        world.W["settings"] = {"role": 1, "bracket": None}
        bad, first, t, n, size, real = [], {}, 0.0, 0, 0, fog.build

        def build(*a):
            nonlocal t, n
            if raise_in_fog:
                raise RuntimeError("selfcheck: forced")
            t0 = time.perf_counter()
            out = real(*a)
            t, n = t + time.perf_counter() - t0, n + 1
            return out
        fog.build = build
        logging.disable(logging.CRITICAL)  # the forced raise logs a traceback once per match: not on the selfcheck's screen
        try:
            for row in timeline():
                world.merge_gsi(row["body"], now=1e9 + row["t"])
                world.tick(1e9 + row["t"])
                rules.evaluate(world.W, 1e9 + row["t"])
                F, H = (world.W.get("hud") or {}).get("fog") or [], world.W["heroes"]  # no hud before the first accepted payload
                size = max(size, len(json.dumps(F)))
                for al in world.W["alerts"]:
                    first.setdefault(al["id"], al["at"])
                if len(F) > 5 or sum(o["tier"] > 0 for o in F) > 2 or any(
                        set(o) != KEYS or H[o["hero"]]["visible"] or H[o["hero"]]["dead"] or H[o["hero"]]["ally"] or (o["x"] is None and o["tier"])
                        or (o["tier"] and o["age"] < 2) or (o["src"] != "respawn" and o["x"] is None) for o in F):
                    bad.append((world.W["game"]["clock"], F))
        finally:
            fog.build = real
            logging.disable(logging.NOTSET)
        return bad, first, 1e3 * t / max(n, 1), size, n

    try:
        fog.demo()
        ok = True
    except AssertionError as e:
        ok = repr(e)
    check("fog: fog.demo() passes", ok is True, "" if ok is True else ok)
    bad, first, ms, size, n = sim()
    check("fog: sim timeline, every hud.fog entry obeys the contract (10 keys, <= 5 entries, <= 2 cues, no seen / dead / allied hero, no cue without a spot or under 2 s)",
          n > 1000 and not bad, f"{n} builds, first violation {bad[0] if bad else None}")
    check("fog: fog.build <= 0.3 ms mean, hud.fog <= 700 bytes", ms <= 0.3 and 0 < size <= 700, f"{ms:.3f} ms, {size} bytes")
    bad2, first2, _, size2, _ = sim(raise_in_fog=True)
    check("fog: a raise inside fog.build leaves hud.fog empty and every alert of the match as it was", size2 == 2 and first2 == first and len(first) > 5
          and world.W["_"].get("fog_err") is True, f"{len(first)} alerts vs {len(first2)}, differ: {sorted(set(first.items()) ^ set(first2.items()))[:4]}")

    world.reset()  # 3. seen 3 s at 1000 u, then gone for 40 s
    for c in range(200, 204):
        step(c, [foe("lion", 1000, 0)])
    seq = [step(c)[1].get("lion") for c in range(204, 244)]  # seq[i]: unseen i + 1 s
    cue = [o["tier"] > 0 for o in seq]
    check("fog: seen 3 s at 1000 u then gone: nothing under 2 s, solid ghost + close cue at 2 s, ghost gone by 9 s, conf never rises, one 15 s cue per stretch",
          all(seq) and seq[0]["tier"] == 0 and seq[0]["age"] == 1 and seq[1]["age"] == 2 and seq[1]["tier"] == 2 and seq[1]["conf"] >= 0.8
          and seq[8]["conf"] < fog.GHOST <= seq[6]["conf"] and all(a["conf"] >= b["conf"] for a, b in zip(seq, seq[1:]))
          and cue[1:16] == [True] * 15 and not any(cue[16:]) and all(a["ttl"] > b["ttl"] for a, b in zip(seq[1:15], seq[2:16])),
          f"2 s: {seq[1]}, 9 s conf {seq[8] and seq[8]['conf']}, cue seconds {[i + 1 for i, x in enumerate(cue) if x]}")
    world.reset()  # 4. a one-second sighting is a sighting; an icon dropping out for 1.5 s four times is nothing
    step(300, [foe("lion", 700, 0)])
    one = [step(c)[1].get("lion") for c in (301, 302, 303, 304)]
    flick = []
    for k in range(4):
        step(400 + 2 * k, [foe("sven", 700, 0)])
        flick += [step(400 + 2 * k + d)[1].get("sven") for d in (0.5, 1.0, 1.5)]
    check("fog: a 1 s sighting earns a ghost (conf > 0, no cue under 2 s, cue after); four 1.5 s dropouts in a row never cue",
          all(one) and one[0]["conf"] > 0 and one[0]["tier"] == 0 and one[0]["age"] < 2 and one[3]["tier"] > 0
          and len(flick) == 12 and all(o and o["tier"] == 0 and o["age"] < 2 for o in flick), f"{one[0]} | {one[3]} | flicker tiers {[o and o['tier'] for o in flick]}")

    world.reset()  # 5. kill event + corpse marker = dead; the respawn timer brings it back without a position
    for c in (500, 501, 502):
        step(c, [foe("lion", 1500, 0)])
    ev = [kill(503, 5)]
    _, f = step(503, [foe("lion", 1500, 0, 0)], ev)
    e = world.W["heroes"]["lion"]
    dead, du = e["dead"] and "lion" not in f and e.get("died_at") == 503, e["dead_until"] or 0
    early = late = None
    for c in range(504, int(du) + 3):  # GSI repeats the event for ~29 s; the marker stays 6 s
        _, f = step(c, [foe("lion", 1500, 0, 0)] if c < 510 else (), ev if c < 532 else ())
        early = early or (c < du and ("lion" in f or not e["dead"]))
        late = f.get("lion") if c >= du else late
    check("fog: kill event + corpse marker = dead, no entry (one death, although the event repeats); at dead_until a 'respawn' entry with no spot, conf 0, no cue",
          dead and 503 + 16 < du < 503 + 120 and not early and bool(late) and (late["src"], late["x"], late["y"], late["conf"], late["tier"]) == ("respawn", None, None, 0, 0)
          and e["last_xy"] == list(world.FOUNTAIN["dire"]) and e.get("resp_at") is not None, f"dead {dead}, dead_until {du}, entry {late}, last_xy {e['last_xy']}")
    world.reset()  # ... and the path before any kill event: marker = dead, marker gone for 4 s = respawned, also without a position
    for c in (500, 501, 502):
        step(c, [foe("sven", 1500, 0)])
    for c in range(503, 506):
        _, f = step(c, [foe("sven", 1500, 0, 0)])
    dead = world.W["heroes"]["sven"]["dead"] and "sven" not in f and not world.W["_"].get("ek_on")
    for c in range(506, 512):
        _, f = step(c)
    check("fog: before the first kill event a corpse marker still kills; its end (marker gone 4 s) is a 'respawn' entry too",
          dead and bool(f.get("sven")) and f["sven"]["src"] == "respawn" and f["sven"]["x"] is None and f["sven"]["tier"] == 0, f"dead {dead}, entry {f.get('sven')}")

    world.reset()  # 6. a corpse marker with no kill event (an illusion's) is not a death
    step(599, [foe("lion", 3000, 0)], [kill(590, 2)])  # an ally died earlier: kill events are flowing
    for c in (600, 601, 602):
        step(c, [foe("lion", 3000, 0)])
    fs = [step(c, [foe("lion", 3300, 200, 0)])[1] for c in range(603, 612)]
    e = world.W["heroes"]["lion"]
    check("fog: a corpse marker without a kill event is not a death but an illusion's: the entry stays, with no spot (the copy we followed was not the hero)",
          world.W["_"].get("ek_on") is True and not e["dead"] and e.get("illusion")
          and all("lion" in f for f in fs) and (fs[-1]["lion"]["src"], fs[-1]["lion"]["x"], fs[-1]["lion"]["tier"]) == ("illusion", None, 0), f"dead {e['dead']}, last_xy {e['last_xy']}, {fs[-1].get('lion')}")

    world.reset()  # 7. the kill goes to the hero whose marker it is, not to the one that walked off 2 s earlier
    for c in (700, 701):
        step(c, [foe("lion", 1500, 0), foe("sven", 2500, 0)])
    for c in (702, 703):
        step(c, [foe("lion", 1500, 0)])
    for c in range(704, 714):
        _, f = step(c, [foe("lion", 1500, 0, 0)], [kill(704, 5)])
    H = world.W["heroes"]
    check("fog: kill event + marker of hero A while hero B left sight 2 s earlier: A is dead, B is not", H["lion"]["dead"] and not H["sven"]["dead"]
          and "sven" in f and "lion" not in f, f"lion dead {H['lion']['dead']}, sven dead {H['sven']['dead']}")
    world.reset()  # an ally's corpse marker is its death, kill events or not, and it leaves the enemy's kill event alone
    ally = lambda vr: {"xpos": 1400, "ypos": 100, "image": "minimap_herocircle", "team": 2, "unitname": "npc_dota_hero_sniper", "visionrange": vr}  # noqa: E731
    for c in (700, 701, 702):
        step(c, [foe("lion", 1500, 0), ally(1800)], [kill(690, 2)])
    for c in range(703, 706):
        step(c, [foe("lion", 1500, 0, 0), ally(0)], [kill(703, 5)])
    H = world.W["heroes"]
    check("death rules: an ally's corpse marker still means dead, and does not take the enemy's kill event", H["sniper"]["ally"] and H["sniper"]["dead"]
          and H["lion"]["dead"] and H["lion"].get("died_at") == 703 and "died_at" not in H["sniper"], f"sniper dead {H['sniper']['dead']}, lion dead {H['lion']['dead']}")

    world.reset()  # minimap CV runs beside GSI: a frame grabbed just before the death arrives just after the payload that reports it
    for c in (700, 701, 702):
        step(c, [foe("lion", 1500, 0)])
    step(703, [foe("lion", 1500, 0, 0)], [kill(703, 5)])
    world.merge_cv({"frame_ok": True, "minimap": [{"hero": "lion", "x": 1490, "y": 10}], "size": [2560, 1440]}, now=1e9 + 703.05)
    _, f = step(704, [foe("lion", 1500, 0, 0)], [kill(703, 5)])
    e = world.W["heroes"]["lion"]
    check("death rules: a minimap-CV frame from just before the death does not bring the hero back (kill event + corpse marker stand)",
          e["dead"] and not e["visible"] and e.get("died_at") == 703 and "lion" not in f, f"dead {e['dead']}, visible {e['visible']}, entry {f.get('lion')}")
    world.reset()  # the marker shows one payload before the match's first kill event (old rule: dead, no timer): its end must still be a respawn
    for c in (500, 501, 502):
        step(c, [foe("lion", 1500, 0)])
    step(503, [foe("lion", 1500, 0, 0)])
    for c in range(504, 520):
        _, f = step(c, [foe("lion", 1500, 0, 0)] if c < 510 else (), [kill(503, 5)])
    e = world.W["heroes"]["lion"]
    check("death rules: a death called before the first kill event (no timer) still ends when its marker has been gone 4 s", world.W["_"].get("ek_on") is True
          and not e["dead"] and bool(f.get("lion")) and f["lion"]["src"] == "respawn", f"dead {e['dead']}, dead_until {e['dead_until']}, entry {f.get('lion')}")
    world.reset()  # a kill nobody saw (no corpse marker) goes to the enemy that left sight just before it, after 5 s ...
    for c in (600, 601):
        step(c, [foe("lion", 1500, 0), foe("sven", 5000, 5000)])
    for c in (602, 603):
        step(c, [foe("sven", 5000, 5000)])
    at = [step(c, [foe("sven", 5000, 5000)], [kill(604, 5)]) and world.W["heroes"]["lion"]["dead"] for c in range(604, 611)]
    du = world.W["heroes"]["lion"]["dead_until"] or 640  # (640: keep the clock moving forward if the rule is broken)
    for c in range(611, int(du) + 2):  # ... and never to one whose last_seen is the respawn timer's (it "left sight" at its respawn)
        step(c, [foe("sven", 5000, 5000)] if c < du else (), [kill(604, 5)] if c < 633 else ())
    for c in range(int(du) + 2, int(du) + 10):
        _, f = step(c, (), [kill(int(du) + 2, 6)])
    H = world.W["heroes"]
    check("death rules: an unseen kill goes to the enemy that left sight 2 s before it, once the event is 5 s old; never to a hero the timer just respawned",
          at == [False] * 5 + [True] * 2 and du != 640 and not H["lion"]["dead"] and H["lion"].get("resp_at") is not None and f.get("lion", {}).get("src") == "respawn",
          f"lion dead by second {at}, after sven's unseen death: lion dead {H['lion']['dead']}, entry {f.get('lion')}")
    world.reset()  # a corpse marker of a hero that was NOT in sight waits until the kill event is 1.5 s old; an old event is nobody's
    step(699, [foe("lion", 1500, 0), foe("sven", 2500, 0)], [kill(690, 2)])
    for c in (700, 701, 702):
        step(c, [foe("sven", 2500, 0)])
    wait = [step(c, [foe("lion", 1500, 0, 0), foe("sven", 2500, 0)], [kill(703, 5)]) and world.W["heroes"]["lion"]["dead"] for c in (703, 704, 705)]
    for c in (760, 761):
        step(c, [foe("sven", 2500, 0)])
    step(762, [foe("sven", 2500, 0, 0)], [kill(703, 6)])  # sven's corpse in front of me, and only a 59 s old event (GSI back after a gap)
    _, f = step(763, [foe("sven", 2500, 0, 0)], [kill(703, 6)])
    check("death rules: a marker of a hero out of sight claims a kill event only once it is 1.5 s old; a marker never claims an old event",
          wait == [False, False, True] and f.get("sven", {}).get("src") == "illusion" and "died_at" not in world.W["heroes"]["sven"], f"lion dead at 0 / 1 / 2 s {wait}, sven {f.get('sven')}")

    world.reset()  # 8. a fresh enemy summon: a hint entry, never solid; a sighting ends the hint
    ward = {"xpos": 1000, "ypos": 1000, "image": "minimap_plaincircle", "team": 3, "unitname": "npc_dota_venomancer_plague_ward", "visionrange": 800}
    step(800, [foe("venomancer", -5000, -5000)])
    for c in range(801, 806):
        step(c)
    _, f = step(806, [ward])
    h, had = f.get("venomancer"), "hint_at" in world.W["heroes"]["venomancer"]
    step(808, [foe("venomancer", 900, 900), ward])
    gone = "hint_at" not in world.W["heroes"]["venomancer"]
    _, f = step(809, [ward])
    check("fog: a fresh plague ward gives a 'hint' entry at the ward (0 < conf <= 0.45); a sighting removes hint_at", bool(h) and h["src"] == "hint" and (h["x"], h["y"]) == (1000, 1000)
          and 0 < h["conf"] <= fog.HINT_CAP and had and gone and f["venomancer"]["src"] == "seen", f"{h}, hint_at {had} -> gone {gone}, after {f.get('venomancer')}")

    world.reset()  # 9. I am dead: marks stay, no cue
    for c in (900, 901):
        step(c, [foe("lion", 1000, 0)])
    step(902)
    _, up = step(903)
    _, f = step(904, alive=False)
    check("fog: while I am dead the entries stay, without cue or odds", up["lion"]["tier"] == 2 and "lion" in f and f["lion"]["tier"] == 0 and f["lion"]["p10"] == 0
          and f["lion"]["conf"] > 0, f"alive {up.get('lion')} | dead {f.get('lion')}")

    world.reset()  # three enemies leave sight 900 u from me: two cues, and the same two on every tick
    for c in (950, 951):
        step(c, [foe(h, 900, 100 * i) for i, h in enumerate(("lion", "sven", "zuus"))])
    cues = [sorted(h for h, o in step(c)[1].items() if o["tier"]) for c in range(952, 960)]
    check("fog: three enemies unseen near me: exactly two cues, the same two on every tick", cues[0] == [] and len(cues[1]) == 2 and all(x == cues[1] for x in cues[1:]), str(cues[:3]))
    world.reset()  # a ghost that has faded stays gone when my vision reaches its spot later
    for c in (960, 961):
        step(c, [foe("lion", 6000, 0)])
    late = [step(c, [{"xpos": 6000, "ypos": 300, "image": "minimap_herocircle", "team": 2, "unitname": "npc_dota_hero_sniper", "visionrange": 1800}] if c >= 972 else ())[1]["lion"]["conf"]
            for c in range(962, 976)]
    check("fog: conf never rises within one unseen stretch, also when the spot turns lit later", late[9] < fog.GHOST and all(a >= b for a, b in zip(late, late[1:]))
          and len(world.W["_"]["sight"]) == 1, str(late))

    conf = []  # 10. unseen 20 s on a spot 500 u inside my team's vision (my illusion: an object the hero loop skips), and on the same spot dark
    for lit in ([{"xpos": 6000, "ypos": 300, "image": "minimap_heroimage", "team": 2, "unitname": "npc_dota_hero_juggernaut", "visionrange": 800}], []):
        world.reset()
        for c in (1000, 1001):
            step(c, [foe("lion", 6000, 0)])
        for c in range(1002, 1022):
            _, f = step(c, lit)
        conf.append((f["lion"]["age"], f["lion"]["conf"], len(world.W["_"]["sight"])))
    check("fog: 20 s unseen, last spot 500 u inside my vision: ghost kept (conf >= 0.30); the same spot dark: gone", conf[0][0] == conf[1][0] == 20
          and conf[0][1] >= fog.GHOST > conf[1][1] and (conf[0][2], conf[1][2]) == (1, 0), str(conf))

    world.reset()  # a broken kill event never costs the payload (GSI repeats an event for ~29 s)
    oks = [step(1100 + i, [foe("lion", 1500, 0)], [kill(1100, 5, d)])[0] for i, d in enumerate(('{"type":"CHAT_MESSAGE_HERO_KILL",', '["CHAT_MESSAGE_HERO_KILL"]', '"CHAT_MESSAGE_HERO_KILL"'))]
    quiet, real = not world.W["_"].get("ek_on") and not world.W["_"].get("ek"), world._kill_events
    world._kill_events = lambda body, team: 1 / 0
    logging.disable(logging.CRITICAL)
    try:
        oks.append(step(1104, [foe("lion", 1500, 0)], [kill(1104, 5)])[0])
    finally:
        world._kill_events = real
        logging.disable(logging.NOTSET)
    check("death rules: a kill event that is not JSON, or not an object, is skipped; a raise in the scan does not drop the payload",
          oks == [True] * 4 and quiet and world.W["heroes"]["lion"]["visible"] and world.W["game"]["clock"] == 1104, f"accepted {oks}, ek_on before the raise {not quiet}")


def selfcheck(a):
    import jev, llm, rec, rules, world
    res = []
    tmp = Path(tempfile.mkdtemp(prefix="coach-check-"))
    world.set_data_dir(tmp / "data")  # never read/write the real data/settings.json or calib.json

    def check(name, ok, detail=""):
        res.append((name, bool(ok), detail))
    # 4. replay the sim timeline through merge_gsi + tick + evaluate (no HTTP)
    world.reset()
    world.W["settings"] = {"role": 1, "bracket": None}
    first, rosh_1801, dead_1210, bad = {}, None, None, []
    for row in timeline():
        now = 1e9 + row["t"]
        world.merge_gsi(row["body"], now=now)
        world.tick(now)
        rules.evaluate(world.W, now)
        c = world.W["game"]["clock"]
        for al in world.W["alerts"]:
            first.setdefault(al["id"], al["at"])
        if c == 730:
            dead_1210 = world.W["me"]["alive"] is False and world.W["me"]["d"] == 1
        if c == 1081:
            rosh_1801 = world.W["obj"]["rosh"]["state"]
        if c is not None and c % 97 == 0 and (err := llm.validate(world.W["_"].get("advice_raw"), llm.SCHEMA)):
            bad.append(f"{c}: {err}")
    r3 = sorted(t for k, t in first.items() if k.startswith("R3:"))
    r4 = first.get("R4")
    check("R3 smoke fires 11:00-11:10 (waits 6 s unseen)", any(660 <= t <= 670 for t in r3), f"R3 at {[world.mmss(t) for t in r3]}")
    check("R4 roshan fires ~17:40", r4 is not None and 1055 <= r4 <= 1070, f"first R4 at {world.mmss(r4) if r4 is not None else None}")
    check("Rosh dead at 18:01", rosh_1801 == "dead", f"state={rosh_1801}")
    check("me dead at 12:10", dead_1210)
    if data.S["heroes"]:  # draft helper: 5 distinct picks during hero selection, none after
        g = world.W["game"]
        st, g["state"] = g["state"], "DOTA_GAMERULES_STATE_HERO_SELECTION"
        rules.draft(world.W, -10)
        dp = (world.W.get("draft") or {}).get("picks") or []
        g["state"] = st
        rules.draft(world.W, 0)
        check("draft picks in hero selection", len({p["hero"] for p in dp}) == 5 and world.W["draft"] is None, str([p["hero"] for p in dp]))
    f = ROOT / "fixtures/draft/hover_2560x1440_top200.png"  # game 9090603898: AA + Bounty still grey (hover), Invoker/Sniper/Pudge locked
    if f.exists() and _vision()._portraits(block=True):
        import cv2, numpy as np
        fr = np.zeros((1440, 2560, 3), np.uint8); fr[:200] = cv2.imread(str(f))
        dp = _vision().draft_picks(fr)
        check("draft: grey hovers are not picks", dp["enemies"] == [None, None, "invoker", "sniper", "pudge"] and not any(dp["allies"]), str(dp))
    W_, g = world.W, world.W["game"]  # R10: my T3 losing HP -> high DEFEND; destroyed -> brief "Lost" info
    saved = (dict(W_["obj"]["my_buildings"]), list(W_["alerts"]), g["clock"])
    for i, hp in enumerate((100, 99, 97, 95, 93)):
        g["clock"], W_["obj"]["my_buildings"] = 5000 + i, {"dota_badguys_tower3_bot": hp}
        rules.defend(W_, g["clock"])
    r10 = next((a for a in W_["alerts"] if a["rule"] == "R10"), None)
    check("R10 defend fires on building damage", bool(r10) and r10["level"] == "high" and "bot T3" in r10["text"], r10 and r10["text"])
    W_["obj"]["my_buildings"], W_["alerts"], g["clock"] = saved
    W_, g = world.W, world.W["game"]  # R11 alone deep -> TOO FAR; R12 allies fighting nearby -> JOIN FIGHT
    saved = (dict(W_["me"]), {k: dict(v) for k, v in W_["heroes"].items()}, list(W_["alerts"]), g["clock"], g["daytime"])
    g["clock"], g["daytime"] = 900, True
    W_["me"].update(alive=True, x=4500, y=3500, hp_pct=90, team="radiant", hero="juggernaut")
    for e in W_["heroes"].values():
        e.update(visible=False, mia_s=None, dead=False)
    W_["_"].setdefault("dropped_at", {}).pop("R11", None)
    rules.overextend(W_, 900)
    r11 = next((a for a in W_["alerts"] if a["rule"] == "R11"), None)
    ally = next(s for s, e in W_["heroes"].items() if e["ally"] and s != "juggernaut")
    foe = next(s for s, e in W_["heroes"].items() if not e["ally"])
    W_["me"].update(x=0, y=0)
    W_["heroes"][ally].update(visible=True, x=2500, y=0)
    W_["heroes"][foe].update(visible=True, x=3000, y=200)
    W_["_"].setdefault("dropped_at", {}).pop("R12", None)  # earlier sims may have just cleared it (re-fire cooldown)
    W_["hud"], W_["alerts"], W_["decision"] = {"threats": [], "fight": None, "mia": []}, [], None  # R12 never fires next to a retreat alert
    rules.join_fight(W_, 900)
    solo = next((a for a in W_["alerts"] if a["rule"] == "R12"), None)  # one ally against one enemy is not "your team is fighting"
    ally2 = next(s for s, e in W_["heroes"].items() if e["ally"] and s not in ("juggernaut", ally))
    W_["heroes"][ally2].update(visible=True, x=2600, y=100)
    rules.join_fight(W_, 900)
    r12 = next((a for a in W_["alerts"] if a["rule"] == "R12"), None)
    check("R11 too far + R12 join fight (2+ allies; never for a 1v1)", bool(r11) and bool(r12) and not solo and bool(W_["hud"]["fight"]),
          f"{r11 and r11['text']} | {r12 and r12['text']} | solo {solo and solo['text']}")
    W_["me"], W_["alerts"], g["clock"], g["daytime"] = saved[0], saved[2], saved[3], saved[4]
    for k, v in saved[1].items():
        W_["heroes"][k] = v
    check("rules advice validates against SCHEMA", not bad, "; ".join(bad[:3]) or f"last: {world.W['advice'].get('headline')!r}")
    check("timers populated", len(world.W["timers"]) >= 3, f"{[t['name'] for t in world.W['timers']]}")
    check("to_text compact", len(world.to_text()) < 2000, f"{len(world.to_text())} chars")
    att = world.W["attention"]
    check("attention fed by the sim", att["seq"] > 5 and att["level"] in ("none", "info", "warn", "high"), f"seq {att['seq']}, now {att['word']}")

    # turbo: 40-min turbo timeline in turbo mode (aegis 240, neutral tiers halved, buyback 15:00, no R7, Roshan again ~29:15)
    world.reset()
    world.W["settings"] = {"role": 1, "bracket": None, "turbo": False}  # Turbo must be AUTO-detected from passive income
    first, aegis, rosh2 = {}, None, None
    for row in timeline(40, turbo=True):
        now = 1e9 + row["t"]
        world.merge_gsi(row["body"], now=now)
        world.tick(now)
        rules.evaluate(world.W, now)
        c = world.W["game"]["clock"]
        for al in world.W["alerts"]:
            first.setdefault(al["id"], al["at"])
        if c == 1090:
            aegis = world.W["obj"]["rosh"]["aegis_until"]
        if c == 1801:
            rosh2 = world.W["obj"]["rosh"]["state"]
    r4b = [t for k, t in first.items() if k == "R4" or (k.startswith("rosh:") and t > 1700)]
    check("turbo: aegis 240 s", aegis == 1085 + 240, f"aegis_until {aegis}")
    check("turbo: neutral tier 2 at 7:30", 450 <= first.get("N:2", -1) < 460, f"N:2 at {first.get('N:2')}")
    check("turbo: buyback rule from 15:00, no R7, no hint", 900 <= first.get("R6", -1) < 1800 and not any(k.startswith("R7") for k in first)
          and "TURBO" not in first and world.W["game"]["mode"] == "turbo", f"R6 at {first.get('R6')}")
    check("40-min sim: Roshan again ~30:00", rosh2 == "dead" and any(1745 <= t < 1800 for k, t in first.items() if k == "R4" or k.startswith("R3")),
          f"rosh {rosh2} at 30:01, R4/R3 after 29:00: {sorted(world.mmss(t) for k, t in first.items() if (k == 'R4' or k.startswith('R3')) and t > 1700)}")
    world.reset()
    world.W["settings"] = {"role": 1, "bracket": None, "turbo": False, "turbo_match": "always"}
    for row in timeline(6, turbo=True):  # manually normal, turbo gpm -> hint
        world.merge_gsi(row["body"], now=1e9 + row["t"])
        world.tick(1e9 + row["t"])
        rules.evaluate(world.W, 1e9 + row["t"])
    check("turbo hint at 5:00 when gpm > 700", any(al["rule"] == "TURBO" for al in world.W["alerts"]))
    world.reset()
    world.W["settings"].update(turbo=True, turbo_match="always")
    world.tick(1e9)
    world.W["me"]["team"], e = "radiant", world._hero("lion", "dire")
    e.update(visible=False, last_seen=599)
    world.W["_"]["kills"], world.W["game"]["score"] = 0, [1, 0]
    world._kills(600, "radiant")
    check("turbo: enemy respawn x0.75", e["dead"] and abs(e["dead_until"] - 600 - 0.75 * (6 + 25)) < 0.01, f"dead_until {e['dead_until']}")
    world.reset()
    body = next(r["body"] for r in timeline() if (r["body"].get("map") or {}).get("clock_time") == 100)
    body["map"] |= {"roshan_state": "respawn_variable", "roshan_state_end_seconds": 95, "tormentor_state": "alive"}
    world.merge_gsi(body, now=1e9)
    world.tick(1e9)
    r = world.W["obj"]["rosh"]
    check("GSI roshan_state/tormentor preferred", r["state"] == "maybe" and r["src"] == "gsi" and world.W["obj"]["tormentor"]["state"] == "alive")
    world.reset()  # a fresh enemy summon moves its hidden owner's last-known spot; an old ward seen again does not
    body = next(r["body"] for r in timeline() if (r["body"].get("map") or {}).get("clock_time") == 100)
    foe = 3 if body["player"]["team_name"] == "radiant" else 2
    body["minimap"]["oW"] = {"xpos": 1000, "ypos": 1000, "image": "minimap_plaincircle", "team": foe, "unitname": "npc_dota_venomancer_plague_ward", "visionrange": 800}
    world.merge_gsi(body, now=1e9)
    e = world._hero("venomancer", world.TEAM[foe])
    e.update(visible=False, dead=False, last_xy=[-5000, -5000], last_seen=50)
    world.W["_"]["summ"] = []
    world.merge_gsi(body, now=1e9 + 1)
    hit = e["last_xy"] == [1000, 1000]
    e["last_xy"] = [-5000, -5000]
    world.merge_gsi(body, now=1e9 + 2)
    check("enemy summon hints its hidden owner once", hit and e["last_xy"] == [-5000, -5000]
          and world.summon_owner("npc_dota_shadow_shaman_ward_1", {"shadow_shaman", "venomancer"}) == "shadow_shaman"
          and world.summon_owner("npc_dota_dark_troll_warlord_skeleton_warrior", {"troll_warlord"}) is None, f"last_xy {e['last_xy']}")

    _fog_checks(check)

    # attention priority + seq (synthetic)
    world.reset()
    W = world.W
    W["settings"]["turbo"] = False
    W["game"]["clock"], W["me"]["alive"] = 100, True
    log_ = []

    def att(c, alerts=(), dec=None):
        W["alerts"], W["decision"] = list(alerts), dec
        rules.attention(W, c)
        A = W["attention"]
        log_.append((A["seq"], A["word"], A["level"], A["sticky"]))

    T_ = {"id": "T:Bounty runes:240", "level": "info", "text": "Bounty runes in 10s", "rule": "T", "at": 100, "ttl": 20}
    R2 = {"id": "R2", "level": "warn", "text": "3 missing: farm closer to tower", "rule": "R2", "at": 101, "ttl": 15}
    R1w, R1h = ({"id": "R1", "level": lv, "text": "x", "rule": "R1", "at": 102, "ttl": 20} for lv in ("warn", "high"))
    dec = {"at_clock": 100, "action": {"choice": "retreat", "p": 0.7, "probs": {}}, "where": {"choice": "base", "p": 0.9},
           "danger": {"score": 1, "conf": 0.5}, "rosh_taken": {"p": 0.1}, "fight": {"p": 0.2}, "next_item": None, "target": None}
    att(100)
    att(100, [T_])
    att(101, [T_], dec)
    att(102, [T_, R2], dec)
    att(103, [T_, R2, R1w], dec)
    att(104, [T_, R2, R1h], dec)
    att(105, [T_, R2], dec)
    att(106, [T_], dec)
    att(130, [T_], dec)  # decision now stale -> timers
    att(131)
    att(140)
    want = [(0, None, "none", False), (1, "RUNE", "info", True), (2, "BACK", "warn", True), (3, "3 MISSING", "warn", True),
            (4, "GANK", "warn", True), (5, "DANGER", "high", True), (5, "3 MISSING", "warn", True), (5, "BACK", "warn", True),
            (5, "RUNE", "info", True), (5, "RUNE", "info", False), (5, None, "none", False)]
    check("attention priority + seq", log_ == want, f"{[(s_, w_) for s_, w_, _, _ in log_]}")

    _v2_checks(check)

    # Jev: question building, request shape and answer parsing against a local mock server
    srv, seen = _mock_jev()
    saved = jev.URL, os.environ.get("JEV_API_KEY")
    jev.URL, os.environ["JEV_API_KEY"] = f"http://127.0.0.1:{srv.server_port}/v1/systemone", "test-key"  # the real key never leaves
    world.reset()
    W = world.W
    W["settings"] = {"role": 1, "bracket": None, "turbo": False}
    for row in timeline():
        world.merge_gsi(row["body"], now=1e9 + row["t"])
        world.tick(1e9 + row["t"])
        rules.evaluate(W, 1e9 + row["t"])
        if W["game"]["clock"] == 640:
            break
    W["_"]["advice_raw"]["next_items"] = ["black_king_bar", "manta"]
    q = jev.questions(W)
    jev.maybe_decide(W, 1e9 + 2000)
    for _ in range(100):
        if not W["jev"]["in_flight"]:
            break
        time.sleep(0.03)
    d, b = W["decision"] or {}, seen.get("body") or {}
    okq = all(q_["type"] in ("noul", "choice", "score") and (q_["type"] != "choice" or 1 <= len(q_["criteria"]) <= 255)
              and (q_["type"] != "score" or 1 <= len(q_["criteria"]) <= 10) for q_ in b.get("questions", {}).values())
    check("Jev questions", set(q) >= {"action", "where", "danger", "rosh_taken", "fight", "next_item"} and len(q["action"]["criteria"]) == 12
          and okq and b.get("model") == "jev-latest" and "mode: normal" in b.get("state", "") and seen.get("auth") == "Bearer test-key",
          f"{sorted(q)}; target {'asked' if 'target' in q else 'skipped'} ({len(q.get('target', {}).get('criteria', {}))} candidates)")
    check("Jev answer parsing", d.get("action", {}).get("choice") == "retreat" and d["action"]["p"] == 0.8 and d["where"]["choice"] == "base"
          and d["fight"]["p"] == 0.7 and d["danger"]["score"] == 1.2 and d["next_item"] == {"choice": "black_king_bar", "p": 0.8}
          and W["jev"]["ok"] and W["jev"]["calls"] == 1 and abs(W["jev"]["usd_est"] - 0.000042) < 1e-9,
          f"{json.dumps({k: d.get(k) for k in ('action', 'danger', 'next_item')})[:160]}, jev {W['jev']['latency_ms']} ms")
    junk = jev.parse({"answers": {"action": {"choice": "moon"}, "danger": "x"}}, q, 1, 1)
    check("Jev junk answers -> neutral", junk["action"]["choice"] is None and junk["danger"]["score"] == 0 and junk["next_item"] is None)
    srv.shutdown()
    jev.URL = saved[0]
    os.environ.pop("JEV_API_KEY") if saved[1] is None else os.environ.__setitem__("JEV_API_KEY", saved[1])

    # recorder: writer in a subprocess, kill -9 mid-stream, every JSONL line must still parse and meta.json be valid
    p = subprocess.Popen([sys.executable, "-c", CRASH, str(tmp / "rec"), str(ROOT)], stdout=subprocess.PIPE, text=True)
    sd = Path(p.stdout.readline().strip())
    time.sleep(1.5)
    p.kill()
    p.wait()
    bad, n = 0, {}
    for f in sd.glob("*.jsonl"):
        for line in f.read_text().splitlines():
            n[f.stem] = n.get(f.stem, 0) + 1
            try:
                json.loads(line)
            except ValueError:
                bad += 1
    meta = _meta(sd)
    check("recorder survives kill -9", not bad and n.get("gsi", 0) > 200 and n.get("events", 0) > 200 and meta.get("test") == "crash"
          and len(list((sd / "frames").glob("mm_*.png"))) > 5, f"{n}, {bad} bad lines, {len(list((sd / 'frames').glob('*')))} frames")

    # null-safety: junk payloads must not raise or corrupt state
    junk = [None, [], {}, {"player": None}, {"player": {"activity": "playing", "team_name": "radiant"}},
            {"player": {"activity": "playing", "team_name": "dire"}, "map": {"clock_time": "12", "matchid": None},
             "hero": {"xpos": "abc", "alive": None, "id": "x"}, "minimap": {"o1": None, "o2": {"xpos": None}, "o3": {"xpos": 1, "ypos": "2", "unitname": 5}},
             "events": [None, {"event_type": 5}, {"event_type": "roshan_killed", "game_time": "nan"}], "items": {"slot0": "x", "teleport0": None},
             "abilities": {"ability0": None, "x": {"name": 3}}, "buildings": {"radiant": {"t": None}, "dire": 7}},
            {"player": {"activity": "playing", "team_name": "radiant"}, "map": {"clock_time": float("inf")}}]
    try:
        for j in junk:
            world.merge_gsi(j)
        world.merge_cv(None)
        world.merge_cv({"frame_ok": True, "lineup": {"heroes": [None] * 9 + ["lion"], "scores": [0] * 10, "locked": True},
                        "dead": [True] * 10, "minimap": [{"hero": "lion"}, None, {"hero": "lion", "x": "1", "y": 2}]})
        world.tick()
        rules.evaluate(world.W, time.time())
        json.loads(world.dumps())
        check("GSI/CV null-safety", True, f"{len(junk)} junk payloads + junk cv")
    except Exception as e:
        check("GSI/CV null-safety", False, repr(e))

    z = world.zone
    check("zone()", (z(*FOUNTAIN, "radiant"), z(2000, -6300, "radiant"), z(2000, -6300, "dire"), z(100, -100, "radiant"),
                     z(-3000, -2800, "radiant"), z(0, -3500, "radiant"), z(0, 3500, "radiant"))
          == ("base", "safe_lane", "off_lane", "river", "mid", "own_jungle", "enemy_jungle"))
    check("SCHEMA rejects bad advice", llm.validate({"headline": "x"}, llm.SCHEMA) and
          llm.validate({"headline": "x", "do_now": [], "next_items": [], "where": "moon", "risk": "low", "why": ""}, llm.SCHEMA))
    env = {"KEEP": "real"}
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("# c\n\nA=1\nB = 'two words'\nexport C=\"3\"\nKEEP=override\nbad line\n")
    llm.load_env(f.name, env)
    os.unlink(f.name)
    check(".env parser", env == {"KEEP": "real", "A": "1", "B": "two words", "C": "3"}, str(sorted(env)))
    saved = {k: os.environ.get(k) for k in ("SC_BASE_URL", "SC_MODEL", "GEMINI_API_KEY", "SC_API_KEY")}
    os.environ.update(SC_BASE_URL="https://generativelanguage.googleapis.com/v1beta/openai/", SC_MODEL="m", GEMINI_API_KEY="dummy-gem")
    os.environ.pop("SC_API_KEY", None)
    t = llm.target("SC")
    for k, v in saved.items():
        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    check("LLM key picked by host", t and t["key"] == "dummy-gem" and t["base"].endswith("/openai"))
    check("redact()", "sk-abcdef123456" not in llm.redact("Incorrect API key: sk-abcdef123456"))

    try:
        if a.no_vision:
            raise RuntimeError("--no-vision")
        vres = _vision().selfcheck()
        res += [(f"vision: {n}", ok, d) for n, ok, d in vres]
    except Exception as e:
        print(f"SKIP vision checks ({e!r})")
    try:
        if a.no_vision:
            raise RuntimeError("--no-vision")
        import inspector
        res += inspector.selfcheck()  # inventory reading on recorded own-hero frames (GSI = truth)
    except Exception as e:
        print(f"SKIP inventory checks ({e!r})")
    import builds, skills
    import habits
    res += skills.selfcheck() + builds.selfcheck() + habits.selfcheck()
    import voice
    res += voice.selfcheck()
    import listen
    res += listen.selfcheck()
    import coach
    res += coach.selfcheck()

    # postgame: the sim split over two recorded sessions (R1 just before the 12:00 death), dire wins -> report, games.jsonl,
    # profile hints; then compact() merges them into one dir that still replays and re-analyzes
    import postgame
    root, rows = tmp / "rec", list(timeline())
    rows[-1]["body"]["map"] |= {"win_team": "dire", "game_state": "DOTA_GAMERULES_STATE_POST_GAME"}
    A, B = root / "20990101-000000_8512345678", root / "20990101-000100_8512345678"
    for d, part in ((A, rows[:len(rows) // 2]), (B, rows[len(rows) // 2:])):
        (d / "frames").mkdir(parents=True)
        (d / "gsi.jsonl").write_text("".join(json.dumps(r) + "\n" for r in part))
        (d / "world.jsonl").write_text("".join(json.dumps({"w": 1e9 + c, "game": {"clock": c}}) + "\n" for c in range(0, 1500)))
    (A / "events.jsonl").write_text(json.dumps({"w": 1e9 + 712, "clock": 712, "kind": "alert", "op": "new", "id": "R1", "rule": "R1",
                                                "level": "high", "text": "Danger: leave now / hold TP"}) + "\n")
    for ms in (1e12 + 715e3, 1e12 + 900e3):  # kept (3 s after the high alert), dropped
        (A / "frames" / f"full_{int(ms)}.jpg").write_bytes(b"x")
    (A / "frames" / f"mm_{int(1e12 + 1e3)}.png").write_bytes(b"x")
    (A / "frames" / f"mm_{int(1e12 + 2e3)}.png").write_bytes(b"x")  # same 30 s bucket: dropped
    rep = postgame.analyze([A, B], llm_call=False)
    dz = rep.get("deaths") or [{}]
    check("postgame report: loss, 1 warned death, files + profile", rep.get("result") == "loss" and len(rep["deaths"]) == 1 and dz[0].get("warned")
          and (B / "report.md").exists() and postgame.profile().get("games") == 1 and 0 < len(postgame.profile_hints("juggernaut", 1)) <= 400
          and set(postgame.summary(rep)) >= {"result", "kda", "deaths", "highlights", "lessons", "goal", "one_lesson", "one_praise", "coach_misses",
                                             "next_goal"} and rep["goal"] == {"text": "6 deaths or fewer", "target": 6, "now": 1, "met": True, "why": rep["goal"]["why"]}
          and rep["mode"] == "normal" and rep["one_praise"].startswith("You kept it to 1 death") and rep["next_goal"] == "3 deaths or fewer"
          and "R1" not in (B / "report.md").read_text().split("## Details")[0],
          f"{rep.get('result')}, deaths {[(world.mmss(x.get('clock')), x.get('warned')) for x in dz]}, hints {postgame.profile_hints('juggernaut', 1)!r}")
    live_s, postgame.LIVE_S = postgame.LIVE_S, 0  # just written; a newer dir makes the match not the live one
    (root / "20990101-000200").mkdir()
    c = postgame.compact([A, B])
    postgame.LIVE_S = live_s
    re_ = postgame.analyze([A], llm_call=False, write=False) if A.exists() else {}
    check("postgame compact: merged, gz, frames pruned, replayable", c.get("target") == A.name and not B.exists() and not (A / "cv.jsonl").exists()
          and len(rows_from(A / "gsi.jsonl")) == len(rows) and sorted(f.name.split("_")[0] for f in (A / "frames").iterdir()) == ["full", "mm"]
          and "storage" in (A / "report.md").read_text() and len(re_.get("deaths") or []) == 1 and re_.get("result") == "loss", str(c))
    world.reset()
    for n, ok, d in res:
        print(f"{'PASS' if ok else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))
    fails = sum(1 for _, ok, _ in res if not ok)
    print(f"{len(res) - fails}/{len(res)} passed")
    sys.exit(1 if fails else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("install-gsi", help="write the GSI cfg + token")
    p.add_argument("--cfg-dir", help="target dir (default: Dota's cfg/gamestate_integration)")
    p.add_argument("--port", type=int, default=3939)
    p = sp.add_parser("sim", help="write fixtures/sim.jsonl and post it to the running app")
    p.add_argument("--speed", type=float, default=4)
    p.add_argument("--minutes", type=int, default=25, help="game length (25 = the v1 timeline; 40 adds Roshan #2 + late fights)")
    p.add_argument("--turbo", action="store_true", help="turbo-like gpm/levels (run the app with --turbo too)")
    p.add_argument("--port", type=int, default=3939)
    p.add_argument("--write-only", action="store_true")
    p = sp.add_parser("replay", help="re-post a recordings/*/gsi.jsonl with its timing")
    p.add_argument("file")
    p.add_argument("--speed", type=float, default=1)
    p.add_argument("--port", type=int, default=3939)
    p = sp.add_parser("synth", help="synthetic frames (vision); default lineup = the sim's 10 heroes, enemies fogged")
    p.add_argument("--n", type=int, default=200)
    p.add_argument("--random", action="store_true", help="random 10-hero lineup with deaths (vision stress, not sim-matched)")
    p.add_argument("--out", default=str(ROOT / "fixtures/synth"))
    sp.add_parser("snap", help="save one real frame + overlay (vision)")
    p = sp.add_parser("selfcheck", help="PASS/FAIL checks; exit 1 on any FAIL")
    p.add_argument("--no-vision", action="store_true", help="skip the (CPU-heavy) synthetic vision checks")
    p = sp.add_parser("sessions", help="list recorded sessions")
    p.add_argument("--dir", default=str(REC))
    p = sp.add_parser("review", help="diagnostic for one session -> DIR/review.md (match report: postgame)")
    p.add_argument("dir")
    p = sp.add_parser("postgame", help="post-game report for a match (all its sessions) -> last DIR/report.md + data/profile")
    p.add_argument("dirs", nargs="*", help="session dir(s); other sessions of the same match are added (default: the latest match)")
    p.add_argument("--no-llm", action="store_true", help="skip the one LLM debrief call")
    p.add_argument("--no-compact", action="store_true", help="keep the raw session dirs")
    p.add_argument("--root", default=str(REC))
    p = sp.add_parser("compact", help="merge + shrink analyzed matches (dry run unless --yes; never the live session)")
    p.add_argument("dirs", nargs="*", help="session dir(s) of the match(es) (default: the latest finished match)")
    p.add_argument("--all", action="store_true", help="every recorded match")
    p.add_argument("--yes", action="store_true", help="really delete (otherwise only print what would be deleted)")
    p.add_argument("--root", default=str(REC))
    p = sp.add_parser("fetch-match", help="after the game: OpenDota match JSON -> DIR/opendota_match.json (+ parse request)")
    p.add_argument("dir")
    a = ap.parse_args()
    if a.cmd == "synth":
        return _vision().synth(a.out, a.n, lineup=None if a.random else ["juggernaut", *ALLY, *ENEMY])
    if a.cmd == "snap":
        return _vision().snap()
    {"install-gsi": install_gsi, "sim": sim, "replay": replay, "selfcheck": selfcheck, "sessions": sessions, "review": review,
     "fetch-match": fetch_match, "postgame": postgame_cmd, "compact": compact_cmd}[a.cmd](a)


if __name__ == "__main__":
    main()
