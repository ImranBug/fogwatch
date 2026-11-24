"""Deterministic rules, run every tick. evaluate(W, now) mutates W alerts/timers/advice/enemy_report.
Thresholds are starting values to tune from --record sessions. All times are game clock seconds."""
import math, statistics
import builds, data, fog, habits, skills, jev, world
from world import mmss, num

# (name, first, period, lead_s, roles, until); roles: "all" or role numbers ("2" = mid)
# Turbo: runes and day/night run on the same clock as a normal game; what follows the faster game is scaled in timers():
# the Stack cut-off and the Tormentor spawn are halved (UNVERIFIED, like the neutral tiers).
TIMERS = [("Bounty runes", 0, 240, 20, "all", None), ("Water rune", 120, 120, 20, "2", 240),
          ("Power rune", 360, 120, 20, "2", None), ("Wisdom shrine", 420, 420, 30, "3,4,5", None),
          ("Lotus", 180, 180, 0, "all", None), ("Stack", 53, 60, 4, "4,5", 1500),  # :53 per creep_pull_timings
          ("Day/Night", 300, 300, 15, "all", None), ("Tormentor", 900, None, 30, "all", None)]
NEUTRAL_TIERS = [0, 900, 1500, 2100, 3600]  # turbo: halved (UNVERIFIED)
COUNTERS = {"evasion": (["phantom_assassin", "windranger", "troll_warlord"], ["monkey_king_bar"]),
            "invis": (["riki", "clinkz", "bounty_hunter", "weaver", "nyx_assassin", "invoker", "treant"], ["ward_sentry", "dust"]),
            "illusions": (["phantom_lancer", "naga_siren", "terrorblade", "chaos_knight"], ["mjollnir", "bfury"]),
            "heal": (["necrolyte", "huskar", "alchemist", "morphling"], ["spirit_vessel"]),
            "magic": (["lion", "lina", "zuus", "skywrath_mage", "leshrac"], ["black_king_bar"])}  # heuristic, hand-curated; any key owned = covered
SECTION = {"start": "Starting_Items", "early": "Early_Game", "mid": "Mid_Items", "late": "Late_Items"}
RANK = {"info": 0, "warn": 1, "high": 2}
RISK = {"low": 0, "med": 1, "high": 2}
LANES = ("safe_lane", "off_lane", "mid")
# alert rule -> (attention word, icon); None word = derived from the alert (see _alert_word)
WORDS = {"R1": ("GANK", "danger"), "R2": (None, "missing"), "R3": ("SMOKE", "smoke"), "R4": ("ROSH", "rosh"),
         "rosh": ("ROSH DEAD", "rosh"), "aegis": ("AEGIS", "rosh"), "R5": ("NIGHT", "night"), "R6": ("BUYBACK", "buyback"),
         "R7": ("LAST HITS", "farm"), "N": ("NEUTRAL", "item"), "TURBO": ("TURBO?", "ok"), "D": ("PICK", "target"), "R10": ("DEFEND", "danger"), "R10x": ("LOST", "danger"),
         "R11": ("TOO FAR", "back"), "R12": ("JOIN FIGHT", "fight"), "R13": ("BACK OFF", "back"), "R14": ("PUSH", "push"),
         "R15": ("DON'T GO", "back"), "R16": ("BUY TP", "item")}
RETREAT = ("R1", "R11", "R13", "R15")  # rules that say "leave": while one is up, no rule says "go" (R12 join, R14 push)
TIMER_WORDS = {"Bounty runes": ("RUNE", "rune"), "Water rune": ("RUNE", "rune"), "Power rune": ("RUNE", "rune"),
               "Wisdom shrine": ("SHRINE", "rune"), "Lotus": ("LOTUS", "rune"), "Stack": ("STACK", "stack"),
               "Day/Night": ("NIGHT", "night"), "Tormentor": ("TORMENTOR", "rosh")}
LINGER = 5  # s of clock an ended attention event stays shown with sticky=False


def evaluate(W, now):
    c = W["game"]["clock"]
    if c is None:
        return
    D, _ = 0.0, W["_"]
    W["hud"] = {"threats": [], "fight": None, "mia": [], "fog": []}  # overlay: edge arrows + minimap marks (danger/join_fight fill it)
    _history(W, c)
    _score(W, c)
    if "demoted" not in _:  # precision gate, once per match: a rule that is mostly wrong for this player is shown, not spoken
        import postgame
        _["demoted"] = set(postgame.rule_tuning().get("demote") or [])
    _["eased_now"] = {r for r, t in (_.get("eased") or {}).items() if t > c}  # eased: "bad call" by voice, 5 minutes
    _["soft"] = _["demoted"] | _["eased_now"]
    if (W["src_age"]["gsi_s"] or 0) > 5 and not W["game"]["paused"]:
        W["alerts"] = []  # GSI stopped (menu, disconnect, game over): the clock froze, so nothing would ever expire
    else:
        W["alerts"] = [a for a in W["alerts"] if c - a["at"] <= a["ttl"]]
        timers(W, c)
        events(W, c)
        draft(W, c)
        if c > 0 and W["game"].get("state") == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":  # positional rules only after the horn
            try:  # unseen enemies for the overlay (also while I am dead). Guarded: a raise here would skip every alert of the tick
                W["hud"]["fog"] = fog.build(W["heroes"], W["me"], c, _.get("sight") or (), _.setdefault("fog", {}))
            except Exception:
                if not _.get("fog_err"):  # once per match (world.reset recreates W["_"])
                    world.log.exception("fog.build failed; hud.fog stays empty")
                _["fog_err"], _["fog"], W["hud"]["fog"] = True, {}, []
            D = W["_"]["D"] = danger(W, c)
            pressure(W, c)
            missing(W, c)
            smoke(W, c)
            rosh_suspicion(W, c)
        else:
            W["alerts"] = [a for a in W["alerts"] if a["rule"] not in ("R1", "R2", "R3", "R4")]
        defend(W, c)
        if c > 0 and W["game"].get("state") == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
            overextend(W, c)
            chain_feed(W, c)
            join_fight(W, c)
            objective(W, c)
        night(W, c)
        buyback(W, c)
        no_tp(W, c)
        last_hits(W, c)
        turbo_hint(W, c)
        weak_heroes(W)
        skills.update(W)
        habits.track(W, c)
        advice(W, c, D, next_items(W, c))
    gold = W["me"]["gold"] or 0
    for i in (W.get("advice") or {}).get("next_items") or []:
        i["affordable"] = bool(i.get("cost")) and gold >= i["cost"]
    W["hud"]["danger"] = round(D, 2)  # the raw R1 score (the glance danger is the decision model's when it has one)
    attention(W, c)
    glance(W, c, D)


def _turbo(W):
    return W["game"].get("mode") == "turbo"


def _laning(W, c):
    return c < (300 if _turbo(W) else 600)


def _history(W, c):
    """W['_']['hist']: my (clock, x, y, hp%) of the last 20 s while alive (R13 HP loss, R15 heading, coach.py debrief), and
    W['_']['ally_dead']: (clock, x, y) of allies that died in the last 15 s."""
    _, me = W["_"], W["me"]
    h = _.setdefault("hist", [])
    if me["alive"] and me["x"] is not None and (not h or c > h[-1][0]):
        h.append((c, me["x"], me["y"], me["hp_pct"] if me["hp_pct"] is not None else 100))
    while h and (c - h[0][0] > 20 or h[0][0] > c):
        h.pop(0)
    was, now = _.get("ally_was_dead", set()), {s for s, e in W["heroes"].items() if e["ally"] and e["dead"] and s != me["hero"]}
    _["ally_dead"] = [d for d in _.get("ally_dead", []) if 0 <= c - d[0] <= 15] + \
        [(c, *W["heroes"][s]["last_xy"]) for s in now - was if W["heroes"][s]["last_xy"]]
    _["ally_was_dead"] = now


def _score(W, c):
    """Kill-score changes of the last 30 s as [(clock, mine, theirs)] in W['_']['sc'] (for winning())."""
    me, sc = W["me"], W["game"].get("score") or [0, 0]
    my = 0 if me.get("team") != "dire" else 1
    now, h = (sc[my], sc[1 - my]), W["_"].setdefault("sc", [])
    if not h or h[-1][1:] != now:
        h.append((c, *now))
    W["_"]["sc"] = [x for x in h if c - x[0] <= 30] or h[-1:]


def winning(W, c, s=15):
    """My team won the fight that is going on: in the last `s` seconds we took a kill and more kills than we lost. Retreat calls stop
    (the danger score counts enemies, not who is dying), and the voice praises instead. Low HP next to an enemy is still 'Get out!'."""
    h = W["_"].get("sc") or []
    old = next((x for x in reversed(h) if c - x[0] >= s), h[0] if h else None)
    if not old:
        return False
    mine, theirs = h[-1][1] - old[1], h[-1][2] - old[2]
    return mine >= 1 and mine > theirs


def _ago(W, c, s):
    """My history sample from about `s` seconds ago (the oldest one if the history is shorter), or None."""
    return next((x for x in W["_"].get("hist") or [] if x[0] >= c - s), None)


def alert(W, key, level, text, ttl, refire=25, **extra):
    """Add alert `key`. While still active at the same/lower level it is kept alive (no re-fire); a higher level re-fires.
    A rule in W['_']['soft'] (precision gate, or eased by "bad call") is shown as info only: amber on screen, never spoken.
    `extra`: structured fields for the pages and the voice (R1 dir, ...), refreshed while the alert lives."""
    c, rule = W["game"]["clock"], key.split(":")[0]
    force = extra.get("force") and rule not in W["_"].get("eased_now", ())  # force beats the precision gate, never the player's "bad call"
    if not force:
        extra.pop("force", None)
    if level != "info" and rule in W["_"].get("soft", ()) and not force:
        level, extra = "info", {**extra, "soft": True}
    W["_"].setdefault("off_since", {}).pop(key, None)  # condition true again: cancel a pending drop
    a = next((a for a in W["alerts"] if a["id"] == key), None)
    if not a and level != "high" and c - W["_"].get("dropped_at", {}).get(key, -1e9) < refire:
        return  # just cleared: don't re-fire a warn within 25 s (no flicker); high always gets through
    if a and (RANK[level] <= RANK[a["level"]] or extra.get("soft")):
        a["ttl"], a["text"] = max(a["ttl"], c - a["at"] + ttl), text if level == a["level"] else a["text"]
        a.update(extra, **({"level": "info"} if extra.get("soft") else {}))  # eased while it was up: amber from now on
        return
    W["alerts"] = [{"id": key, "level": level, "text": text, "rule": rule, "at": c, "ttl": ttl, **extra}] + \
                  [a for a in W["alerts"] if a["id"] != key]


def _active(W, rules):
    return any(a["rule"] in rules for a in W["alerts"])


def drop(W, key, hold=0):
    """Remove alert `key` once its condition has been false for `hold` s (hysteresis against 1 Hz flapping)."""
    c, off = W["game"]["clock"] or 0, W["_"].setdefault("off_since", {})
    if not any(a["id"] == key for a in W["alerts"]):
        return
    if c - off.setdefault(key, c) >= hold:
        W["alerts"] = [a for a in W["alerts"] if a["id"] != key]
        W["_"].setdefault("dropped_at", {})[key] = c
        off.pop(key, None)


def _role_ok(roles, role):
    return roles == "all" or (role is not None and str(role) in roles.split(","))


def timers(W, c):
    out = []
    if W["game"].get("state") not in ("DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"):
        W["timers"] = []  # draft/strategy/loading: the GSI clock is frozen there, countdowns would lie
        return
    k = 0.5 if _turbo(W) else 1
    for name, first, period, lead, roles, until in TIMERS:
        if not _role_ok(roles, W["me"]["role"]):
            continue
        if name == "Stack":
            until *= k
        elif name == "Tormentor":
            first *= k
        at = first if c < first else first + (int((c - first) // period) + 1) * period if period else None
        if at is None or (until is not None and at > until):
            continue
        out.append({"name": name, "at_clock": at, "in_s": round(at - c, 1), "lead": lead})
        if lead and 0 < at - c <= lead:
            alert(W, f"T:{name}:{at}", "info", f"{name} in {int(at - c)}s", lead)
    W["timers"] = sorted(out, key=lambda t: t["in_s"])
    for i, t in enumerate(NEUTRAL_TIERS):
        t *= 0.5 if _turbo(W) else 1
        if t <= c < t + 20 and c >= 0:
            alert(W, f"N:{i + 1}", "info", f"Neutral tier {i + 1} unlocked: craft neutral", 30)


def events(W, c):
    new, W["_"]["new_events"] = W["_"]["new_events"], []
    r = W["obj"]["rosh"]
    for e in new:
        if e["type"] == "roshan_killed":
            W["alerts"] = [a for a in W["alerts"] if a["rule"] != "R4"]
            alert(W, f"rosh:{e['clock']}", "info", f"Roshan killed by {e.get('killed_by_team') or '?'}: "
                  f"respawn {mmss(e['clock'] + 480)}-{mmss(e['clock'] + 660)}", 10)  # the game announces it: brief info, no flash
        elif e["type"] == "aegis_picked_up" and r["aegis_until"]:
            alert(W, f"aegis:{e['clock']}", "info", f"Aegis picked up: expires {mmss(r['aegis_until'])}", 10)


def _enemies(W):
    return [(s, e) for s, e in W["heroes"].items() if not e["ally"] and not e["dead"]]


def _d(a, b):
    return math.dist(a, b) if a and b and None not in a and None not in b else math.inf


def _allies_near(W, pos, r=1500):
    return sum(1 for s, e in W["heroes"].items() if e["ally"] and s != W["me"]["hero"] and e["visible"] and _d((e["x"], e["y"]), pos) <= r)


def _missing(W):
    """Enemies that walked into fog 8-60 s ago (world sets vanished_at only after >= 5 s in sight, so a respawn at the fountain
    or a flickering icon is not "missing"); longer than that is just fog, not a threat."""
    c = W["game"]["clock"]
    return sum(1 for _, e in _enemies(W) if e["vanished_at"] is not None and 8 <= c - e["vanished_at"] <= 60)


JUMPERS = {"magnataur", "earthshaker", "axe", "enigma", "tidehunter", "centaur", "sand_king", "faceless_void", "slardar", "batrider",
           "puck", "mars", "dark_seer", "legion_commander", "beastmaster", "spirit_breaker", "storm_spirit", "void_spirit", "pangolier",
           "phoenix", "tusk", "earth_spirit", "primal_beast", "nyx_assassin", "slark", "riki", "bounty_hunter", "queenofpain", "antimage"}
JUMP_R = 2600  # blink / leap range + a step: these heroes are a threat before they are "near" (player feedback 2026-11-12)


def danger(W, c):
    """R1 likely-gank score D for me; returns D (also used for advice risk)."""
    me = W["me"]
    if not me["alive"] or None in (me["x"], me["y"]):
        drop(W, "R1")
        return 0.0
    pos, smoke = (me["x"], me["y"]), W["_"].get("smoke") or {}
    smoked = smoke.get("heroes", []) if c <= smoke.get("until", -1e9) else []
    lane = c < 600 and world.zone(*pos, me["team"]) in LANES + ("river",)
    if lane and sum(1 for _, e in _enemies(W) if e["visible"] and _d((e["x"], e["y"]), pos) <= 2000) >= 3:
        lane = False  # a third enemy showed up in my lane: that is a gank, nobody is "just a lane opponent" any more
    threat, laners, hud = 0.0, 0, W["hud"]
    came, hd = _ago(W, c, 10), world.home_dist(*pos, me["team"])  # walked out of base: the path behind me was just empty
    from_base = bool(came) and world.home_dist(*came[1:3], me["team"]) < hd - 1500
    for s, e in _enemies(W):
        if e["vanished_at"] is not None and 1 <= c - e["vanished_at"] <= 30 and e["last_xy"]:  # seen walking into fog (4 s felt late)
            hud["mia"].append({"hero": s, "x": e["last_xy"][0], "y": e["last_xy"][1], "r": min(e["reach_u"], 2500)})
        if e["visible"] and _d((e["x"], e["y"]), pos) <= (JUMP_R if s in JUMPERS else 2000):
            if lane and world.zone(e["x"], e["y"], me["team"]) in LANES + ("river",) and laners < 2:
                laners += 1  # laning (< 10:00): your lane opponents are not a gank; a 3rd hero is
            else:
                threat += 1.0
                hud["threats"].append({"hero": s, "x": e["x"], "y": e["y"], "kind": "seen"})
        elif s in smoked and _d(e["last_xy"], pos) <= e["reach_u"] + 1025:
            threat += 1.0
            hud["threats"].append({"hero": s, "x": e["last_xy"][0], "y": e["last_xy"][1], "kind": "smoke"})
        elif e["mia_s"] is not None and 4 <= e["mia_s"] <= 15 and _d(e["last_xy"], pos) <= min(e["reach_u"] + 1000, 2500) \
                and not (from_base and world.home_dist(*e["last_xy"], me["team"]) < hd):  # "3 behind me" as I came from base (32:33)
            threat += 0.6
            hud["threats"].append({"hero": s, "x": e["last_xy"][0], "y": e["last_xy"][1], "kind": "missing"})
    enemy_half = (pos[0] + pos[1] > 0) == (me["team"] != "dire")
    D = max(0.0, threat - _allies_near(W, pos)) * (1.5 if enemy_half else 1) * (1.25 if not W["game"]["daytime"] else 1)
    ex = {"dir": _dir(pos, hud["threats"], me["team"]), "seen": any(t["kind"] == "seen" for t in hud["threats"])}
    if len(hud["threats"]) < 2:
        D = min(D, 1.4)  # one enemy is a 1v1, not a gank (it scored 1.9 alone at night on their side); low HP next to it is "Get out!"
    seen = sum(1 for t in hud["threats"] if t["kind"] == "seen")
    allies, hp = _allies_near(W, pos), me["hp_pct"] if me["hp_pct"] is not None else 100
    joined = c - W["_"].get("r12_at", -1e9) <= 15 and seen <= allies + 1  # we just called "join them": not "back" 5 s later
    teamfight = allies >= 2 and allies >= seen - 1 and hp > 50
    close = [e for _, e in _enemies(W) if e["visible"] and _d((e["x"], e["y"]), pos) <= 1600]
    if len(close) >= 3 and not teamfight:
        D = max(D, 1.5)  # three in sight within 1600: warn now, not when Morphling is at 208 (21:46)
    if allies == 0 and (me.get("role") or 0) >= 4 and any(s in JUMPERS and e["visible"] and _d((e["x"], e["y"]), pos) <= 800 for s, e in _enemies(W)):
        D = max(D, 1.5)  # a lone support with AM / LC at 700: one blink is a kill (18:46, 6:56)
    h = W["_"].get("sc") or []
    o = next((x for x in reversed(h) if c - x[0] >= 15), h[0]) if h else None
    if D >= 2.5 and not close and o and h[-1][1] - o[1] >= 1 and h[-1][1] - o[1] >= h[-1][2] - o[2]:
        D = 2.4  # nobody within 1600 and our fight is going even or better: no "Get out. Now." from missing heroes alone (22:16)  # 3v4 / 5v5 with my team: a team fight, not a gank (review 16:33, 22:56)
    W["hud"]["winning"] = win = winning(W, c) or (allies >= seen + 1 and hp > 50) or joined or teamfight
    if win or world.zone(*pos, me["team"]) == "base":
        drop(W, "R1")  # going our way, or already home: no "back" / "get out" (low HP next to an enemy is still "Get out!")
    elif D >= 2.5:
        alert(W, "R1", "high", "Danger: leave now / hold TP", 20, **ex)
    elif D >= 1.5:  # refire=0: a danger that comes back 10 s after it cleared is called again (the voice limits its own repeats)
        alert(W, "R1", "warn", "Likely gank: back toward tower", 20, refire=0, **ex)
    elif D < 1.0:
        drop(W, "R1", hold=8)  # condition over for 8 s (1.0-1.5 = hysteresis band: let it run out)
    return D


def _dir(pos, threats, team):
    """Where the threat comes from, as the player would say it: 'behind' (between me and my base), 'river', 'jungle', or None."""
    if not threats:
        return None
    t = (sum(x["x"] for x in threats) / len(threats), sum(x["y"] for x in threats) / len(threats))
    if _d(t, world.FOUNTAIN[team]) < _d(pos, world.FOUNTAIN[team]) - 600:
        return "behind"
    z = world.zone(*t, team)
    return "river" if z in ("river", "roshan") else "jungle" if z.endswith("jungle") else None


def pressure(W, c):
    """R13 laning pressure: R1 ignores the two lane opponents, so nothing called the laning deaths. Laning only, an enemy within
    1300 and either HP <= 60% while losing it fast (>= 15% in the last 6 s) or HP <= 50% and outnumbered -> back off and heal.
    (60, not 50: the recorded laning deaths went from 75% to dead in 8 s; at 50% the call came 3-5 s before the death.)"""
    me = W["me"]
    hp = me["hp_pct"] if me["hp_pct"] is not None else 100
    if not me["alive"] or me["x"] is None or not _laning(W, c) or hp > 60:
        return drop(W, "R13", hold=5)
    pos = (me["x"], me["y"])
    foes = sum(1 for _, e in _enemies(W) if e["visible"] and _d((e["x"], e["y"]), pos) <= 1300)
    old = _ago(W, c, 6)
    if foes and ((old and old[3] - hp >= 15) or (hp <= 50 and foes > _allies_near(W, pos, 1300) + 1)):
        alert(W, "R13", "warn", "BACK OFF: low health next to them. Heal up", 10, refire=10)
    else:
        drop(W, "R13", hold=5)


def missing(W, c):
    """R2: 3+ enemies recently missing while I'm exposed and no ally is with me."""
    me, n = W["me"], _missing(W)
    if me["alive"] and None not in (me["x"], me["y"]) and n >= 3 and not _allies_near(W, (me["x"], me["y"])) and (
            (me["x"] + me["y"] > 0) == (me["team"] != "dire")
            or world.zone(me["x"], me["y"], me["team"]) in ("river", "own_jungle", "enemy_jungle", "roshan")):
        alert(W, "R2", "info", f"{n} missing: farm closer to tower", 15)  # info: amber on screen, never spoken (82% had no consequence)
    else:
        drop(W, "R2", hold=8)


def smoke(W, c):
    """R3: 2+ enemies vanish within 3 s of each other from within 1500 u and stay unseen 3 s -> possible smoke.
    world only sets vanished_at after >= 5 s visible; laning fog flicker (< 10:00, in a lane/base) is ignored;
    one alert per hero pair per 60 s."""
    team, last, sm = W["me"]["team"], W["_"].setdefault("r3_last", {}), W["_"].get("smoke")
    if sm and c <= sm["until"] and any(W["heroes"].get(h, {}).get("visible") for h in sm["heroes"]):
        W["_"]["smoke"] = None  # one of them is seen again: smoke broken or never was
        W["alerts"] = [a for a in W["alerts"] if a["rule"] != "R3"]
    score = sum(W["game"]["score"])  # fights: deaths + vision flicker look exactly like a smoke -> stay quiet 20 s after any kill
    if score != W["_"].get("r3_score"):
        W["_"]["r3_score"], W["_"]["r3_kill_at"] = score, c
    if c - W["_"].get("r3_kill_at", -1e9) < 20:
        return
    gone = sorted(((e["vanished_at"], s, e) for s, e in _enemies(W)
                   if not e["visible"] and not e["dead"] and e["vanished_at"] is not None and 6 <= c - e["vanished_at"] <= 18
                   and world.zone(*e["last_xy"], team) != "base"  # leaving a base view / fountain is not a smoke
                   and not (c < 600 and world.zone(*e["last_xy"], team) in LANES)), key=lambda g: g[0])
    for i, (t0, s0, e0) in enumerate(gone):
        grp = [s0] + [s for t, s, e in gone[i + 1:] if t - t0 <= 3 and _d(e["last_xy"], e0["last_xy"]) <= 1500]
        key = frozenset(grp)
        if len(grp) >= 2 and not any(key & k and t0 - t < 60 for k, t in last.items()):
            last[key] = t0
            W["_"]["smoke"] = {"heroes": grp, "until": t0 + 45}
            alert(W, f"R3:{int(t0)}", "info", f"Possible smoke: {'+'.join(grp)} vanished at {world.zone(*e0['last_xy'], team)}", 45)
            return


def rosh_suspicion(W, c):
    """R4: several enemies MIA near the active pit while Roshan is (maybe) up and nobody of ours is watching."""
    r = W["obj"]["rosh"]
    pit = r["pits"].get(r["pit"])
    if r["state"] == "dead" or not pit:
        return drop(W, "R4")  # pits: world defaults, /api/mark or a GSI sighting
    far = [e for _, e in _enemies(W) if e["visible"] and e["x"] is not None and _d((e["x"], e["y"]), pit) > 3000]
    if len(far) >= 3:  # three of them are seen elsewhere: at most two could be at Roshan (player feedback 2026-11-12)
        return drop(W, "R4")
    mia = [e for _, e in _enemies(W) if e["mia_s"] is not None and 10 <= e["mia_s"] <= 60]  # long-unseen = just fog
    near = [e for e in mia if _d(e["last_xy"], pit) <= 3000]
    if len(mia) < 3 or len(near) < 2:
        return drop(W, "R4", hold=5)
    if any(e["ally"] and e["visible"] and _d((e["x"], e["y"]), pit) <= 1500 for e in W["heroes"].values()):
        return drop(W, "R4")
    if len(near) >= 4:
        alert(W, "R4", "high", "Enemies likely at Roshan: smoke/ward or push the opposite lane", 30)
    else:
        alert(W, "R4", "warn", "Possible Roshan", 30)


def night(W, c):
    """R5: night starts (Night Stalker night -> high)."""
    _, g = W["_"], W["game"]
    prev, _["day"] = _.get("day"), (g["daytime"], g["ns_night"])
    if prev and c > 0:
        if g["ns_night"] and not prev[1]:
            alert(W, f"R5:ns{int(c // 60)}", "high", "Night Stalker night: vision 800, stay near allies", 30)
        elif prev[0] and not g["daytime"]:
            alert(W, f"R5:{int(c // 300)}", "info", "Night: vision 800 - ward/avoid river", 30)


def _bname(n):
    """dota_badguys_tower3_bot -> ('bot T3', tier 3); bad_rax_melee_mid -> ('mid barracks', 3); *_fort -> ('ANCIENT', 5)."""
    lane = next((l for l in ("top", "mid", "bot") if n.endswith(l)), "")
    if "fort" in n:
        return "ANCIENT", 5
    if "rax" in n:
        return f"{lane} barracks", 3
    t = next((int(ch) for ch in n.split("tower")[-1][:1] if ch.isdigit()), 1) if "tower" in n else 1
    return (f"{lane} T{t}" if lane else f"T{t}"), (4 if t == 4 else t)


def defend(W, c):
    """R10: my building losing HP (>= 2.5% in 6 s) -> DEFEND. T1/T2 warn; T3, barracks, T4, Ancient high.
    GSI only gives my own team's buildings, which is exactly what this needs. Lost buildings -> brief info."""
    H, bs = W["_"].setdefault("bhist", {}), W["obj"].get("my_buildings") or {}
    hit = []
    for n, pct in bs.items():
        if pct is None:
            continue
        h = H.setdefault(n, [])
        h.append((c, pct))
        while h and c - h[0][0] > 6:
            h.pop(0)
        if pct <= 0:
            if h[0][1] > 0 or len(h) == 1 and n in W["_"].get("b_alive", set()):
                label, _ = _bname(n)
                alert(W, f"R10x:{n}", "info", f"Lost {label}", 10)
            W["_"].setdefault("b_alive", set()).discard(n)
            continue
        W["_"].setdefault("b_alive", set()).add(n)
        label, tier = _bname(n)
        if h[0][1] - pct >= (2.5 if tier >= 3 else 5) and not (_laning(W, c) and tier == 1 and pct > 50):  # lane harass on T1 is normal
            hit.append((n, label, tier, pct))
    if not hit:
        drop(W, "R10", hold=8)
        return
    n, label, tier, pct = max(hit, key=lambda x: (x[2], -x[3]))
    me, team = W["me"], W["me"]["team"]
    lane = next((l for l in ("top", "mid", "bot") if n.endswith(l)), None)
    zone = "base" if tier >= 3 else {"mid": "mid", "top": "off_lane" if team == "radiant" else "safe_lane",
                                     "bot": "safe_lane" if team == "radiant" else "off_lane"}.get(lane)
    near = [s for s, e in _enemies(W) if e["visible"] and e["x"] is not None and world.zone(e["x"], e["y"], team) == zone]
    alive_allies = [e for s, e in W["heroes"].items() if e["ally"] and s != me["hero"] and not e["dead"]]
    there = sum(1 for e in alive_allies if e["visible"] and e["x"] is not None and world.zone(e["x"], e["y"], team) == zone)
    in_base = me["x"] is not None and world.zone(me["x"], me["y"], team) == "base"
    glyph = W["obj"].get("glyph_cd")
    by = f"by {'+'.join(near[:3])}" if near else ""
    if tier >= 3:  # high ground: always defend, together
        txt, lvl = f"DEFEND {label} ({pct:.0f}%)", "high"
        tips = [by] + (["GLYPH ready"] if glyph == 0 else []) + ([] if in_base or not me["alive"] else ["TP and group"])
    elif len(near) >= 3 and there + 1 < len(near):  # outnumbered: don't feed a tower; trade or stay grouped
        txt, lvl = f"LET {label} GO ({pct:.0f}%)", "warn"
        tips = [f"{len(near)} enemies there", "stay with team / push the other side"]
    else:  # 0-2 enemies or we match them: worth defending
        txt, lvl = f"DEFEND {label} ({pct:.0f}%)", "warn"
        tips = [by or "nobody seen: likely creeps", "" if in_base or not me["alive"] else "TP to defend" if len(near) <= 1 else "go with an ally"]
    tips = [t for t in tips if t]
    alert(W, "R10", lvl, txt + (": " + ", ".join(tips) if tips else ""), 12)
    a = next((x for x in W["alerts"] if x["id"] == "R10"), None)  # None: alert() refused a re-fire inside the 25 s cooldown
    if a:  # structured fields for the dashboard's lane pointer
        a.update(lane=lane, tier=tier, building=n, hp=pct, by=near[:3], defend=not txt.startswith("LET"), zone=zone,
                 foes=len(near), allies=there)


def overextend(W, c):
    """R11: deep in the enemy half with no ally near (reckless). Depth = distance past the river along my attack diagonal."""
    me = W["me"]
    if not me["alive"] or me["x"] is None or c < 60:
        return drop(W, "R11")
    depth = (me["x"] + me["y"]) * (1 if me["team"] == "radiant" else -1)  # > 0 = enemy half; lane T1s sit near +4000
    allies = _allies_near(W, (me["x"], me["y"]), 2500)
    miss = sum(1 for _, e in _enemies(W) if not e["visible"])  # living enemies out of sight, however long
    dead = sum(1 for e in W["heroes"].values() if e.get("team") and not e["ally"] and e["dead"])
    lim = 4500 - (1000 if not W["game"]["daytime"] else 0) - 500 * min(miss, 3)
    if depth > lim and allies == 0 and miss >= 2 and dead < 3 and not any(a["rule"] in ("R1", "R10") and a["level"] == "high" for a in W["alerts"]):
        lvl = "high" if depth > lim + 2500 and miss >= 3 else "warn"
        force = (me["hp_pct"] or 100) < 50 or miss >= 3  # demoted R11 stayed silent twice before a death (review 6:47, 13:48)
        alert(W, "R11", lvl, f"TOO FAR: alone deep, {miss} enemies unseen. Back up", 10, force=force)
    else:
        drop(W, "R11", hold=5)


def _fights(W):
    """(visible living allies within 1200 of a visible enemy, visible enemies) -> ([(short, e)], [e])."""
    me = W["me"]
    allies = [(s, e) for s, e in W["heroes"].items() if e["ally"] and s != me["hero"] and e["visible"] and not e["dead"] and e["x"] is not None]
    foes = [e for _, e in _enemies(W) if e["visible"] and e["x"] is not None]
    return [(s, e) for s, e in allies if any(_d((e["x"], e["y"]), (f["x"], f["y"])) <= 1200 for f in foes)], foes


def chain_feed(W, c):
    """R15 chain-feed guard: I'm walking toward a fight that is already lost: the allies there are dead or outnumbered by 2+, and
    with me (and whoever walks with me) we would still be fewer. High: the one 'don't go' call."""
    me = W["me"]
    if not me["alive"] or me["x"] is None or c < 60:
        return drop(W, "R15")
    pos = (me["x"], me["y"])
    fighting, foes = _fights(W)
    died = [(x, y) for t, x, y in W["_"].get("ally_dead") or [] if c - t <= 12]
    spots = [(e["x"], e["y"]) for _, e in fighting] + died
    if spots:
        f = (sum(x for x, _ in spots) / len(spots), sum(y for _, y in spots) / len(spots))
        there = sum(1 for e in foes if _d((e["x"], e["y"]), f) <= 1500)
        with_me = sum(1 for s, e in W["heroes"].items() if e["ally"] and s != me["hero"] and e["visible"] and not e["dead"]
                      and _d((e["x"], e["y"]), pos) <= 1000 and not any(s == k for k, _ in fighting))
        d, old = _d(pos, f), _ago(W, c, 2)
        lost = there >= 2 and there > len(fighting) + 1 + with_me and (there - len(fighting) >= 2 or (died and not fighting)) \
            and world.home_dist(*f, me["team"]) > 4500  # a fight at my own base is a defence, not a fight to skip
        h = W["_"].get("sc") or []
        o = next((x for x in reversed(h) if c - x[0] >= 10), h[0]) if h else None
        we_kill = bool(h) and h[-1][1] - o[1] >= 1 and h[-1][1] - o[1] >= h[-1][2] - o[2]  # we trade at least even in the last 10 s (22:12)
        if lost and not we_kill and 1300 < d <= 4000 and old and _d(old[1:3], f) - d >= 200:  # "It's lost" in a fight won 2-0 (11:29)
            return alert(W, "R15", "high", "DON'T GO: that fight is lost", 6, x=f[0], y=f[1])
    drop(W, "R15", hold=3)


def join_fight(W, c):
    """R12: allies fighting near me (enemies within 1200 of them) and I'm healthy, not already in it -> join. Not in laning,
    not while a retreat rule or a retreat decision is up, only when the allies there already match the enemies; once per 60 s."""
    me, d = W["me"], _decision(W, c)
    if not me["alive"] or me["x"] is None or (me["hp_pct"] or 0) < 60 or _laning(W, c) or _active(W, RETREAT) \
            or (d and d["action"]["choice"] in ("retreat", "heal_base") and d["action"]["p"] >= 0.5):
        return drop(W, "R12")
    pos = (me["x"], me["y"])
    fighting, foes = _fights(W)
    if not fighting:
        return drop(W, "R12", hold=4)
    fx = sum(e["x"] for _, e in fighting) / len(fighting)
    fy = sum(e["y"] for _, e in fighting) / len(fighting)
    d = _d(pos, (fx, fy))
    near_foes = sum(1 for f in foes if any(_d((f["x"], f["y"]), (e["x"], e["y"])) <= 1200 for _, e in fighting))
    around = sum(1 for f in foes if _d((f["x"], f["y"]), (fx, fy)) <= 2500)  # the ones about to collapse count too (11:04: "2v2" vs 4)
    if near_foes and 1300 < d <= 3500 and len(fighting) >= max(2, around) and W["_"].get("D", 0) < 1.0:  # 2+ allies (1v1 is not "your team"), ~10 s away at most
        z = world.zone(fx, fy, me["team"]).replace("_", " ")
        W["hud"]["fight"] = {"x": fx, "y": fy, "allies": len(fighting), "foes": near_foes}
        alert(W, "R12", "warn", f"JOIN FIGHT {z}: {len(fighting)} allies vs {near_foes}", 8, refire=60)
        W["_"]["r12_at"] = c
    else:
        drop(W, "R12", hold=4)


def objective(W, c):
    """R14 objective window: 3+ enemies dead while I'm alive, after laning, and no retreat rule is up -> take a tower."""
    me = W["me"]
    dead = sum(1 for e in W["heroes"].values() if e.get("team") and not e["ally"] and e["dead"])
    h = W["_"].get("sc") or []
    fresh = bool(h) and h[-1][1] - next((x[1] for x in reversed(h) if c - x[0] >= 20), h[0][1]) >= 1  # a kill in the last 20 s
    safe = (me["hp_pct"] or 0) >= 50 and not any(e["visible"] and e["x"] is not None and _d((e["x"], e["y"]), (me["x"], me["y"])) <= 1000
                                                  for _, e in _enemies(W)) if me["x"] is not None else False
    friend = me["x"] is not None and _allies_near(W, (me["x"], me["y"]), 1500) >= 1  # "Take a tower" sent a lone support to die (6:38)
    if me["alive"] and dead >= 3 and fresh and safe and friend and not _laning(W, c) and not _active(W, RETREAT):
        alert(W, "R14", "warn", f"PUSH: {dead} enemies dead. Take a tower", 12, refire=90, dead=dead)
    else:
        drop(W, "R14")


def buyback(W, c):
    """R6: cores should hold buyback gold after 30:00 (turbo 15:00)."""
    me, role = W["me"], W["me"]["role"]
    if c >= (900 if _turbo(W) else 1800) and role is not None and role <= 3 and None not in (me["gold"], me["buyback_cost"]) \
            and me["gold"] < me["buyback_cost"]:
        alert(W, "R6", "warn", f"Save buyback (need {me['buyback_cost'] - me['gold']})", 30)


def no_tp(W, c):
    """R16: no teleport scroll for a while after the horn and gold for one -> buy one (called at most every 3 minutes). The wait is this
    player's habit (habits.grace: room for someone who rebuys on their own) and 10 s when a tower needs defending (a TP is urgent)."""
    me, _ = W["me"], W["_"]
    if me.get("tp") is not False or c < 0 or W["game"].get("state") != "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
        _["no_tp"] = None
        return drop(W, "R16")
    _["no_tp"] = _.get("no_tp") or c
    home = me["x"] is not None and world.zone(me["x"], me["y"], me["team"]) == "base"
    wait = 10 if home or any(a["rule"] == "R10" and a.get("defend") is not False for a in W["alerts"]) else habits.grace("tp")
    calm = me["x"] is None or not any(e["visible"] and _d((e["x"], e["y"]), (me["x"], me["y"])) <= 1500 for _, e in _enemies(W))
    if c - _["no_tp"] >= wait and calm and (me["gold"] or 0) >= ((data.S["items"].get("tpscroll") or {}).get("cost") or 100):
        alert(W, "R16", "info", "NO TP: buy a teleport scroll", 20, refire=180)  # info: spoken once, never takes the screen from a warning


def last_hits(W, c):
    """R7: cores (role 1-3) at 5/10/15 min vs an early-game LH target. OpenDota /benchmarks are whole-game averages
    (late farm included), so the target is 0.6 x p30 LH/min (Juggernaut: 4.8/min = 48 at 10:00), else 5/min."""
    me = W["me"]
    m, lh, role = int(c // 60), me["lh"], me["role"]
    if _turbo(W):
        return  # benchmarks are normal-mode farm
    if m not in (5, 10, 15) or c % 60 >= 15 or lh is None or role is None or role > 3:
        return
    bm = ((data.hero_data(me["hero_id"]) or {}).get("benchmarks") or {}).get("result") or {}
    p30 = next((num(p.get("value")) for p in bm.get("last_hits_per_min") or [] if isinstance(p, dict) and p.get("percentile") == 0.3), None)
    tgt = 0.6 * p30 if p30 else 5.0  # ponytail: flat factor; a per-minute curve from recorded games would be better
    if lh / m < tgt:
        alert(W, f"R7:{m}", "info", f"Last hits {lh} at {m}:00, target {tgt * m:.0f}: focus the wave", 30)


def next_items(W, c):
    """R8: the AI build for this game (builds.py, cached) minus what I own; until it arrives, popular items for my phase
    (OpenDota, else Valve build). Plus counters to the enemy lineup."""
    me = W["me"]
    have = _owned(W)
    b = builds.update(W, c, have, _turbo(W))
    wip = _building(W)
    if b and b.get("build"):
        W["_"]["build"] = b
        nxt = list(dict.fromkeys(wip + [k for k in b["build"] if k not in have]))
        if nxt:
            return nxt[:4]
    k = 0.5 if _turbo(W) else 1
    phase = "start" if c < 0 else "early" if c < 600 * k else "mid" if c < 1500 * k else "late"
    pop = ((data.hero_data(me["hero_id"]) or {}).get("items") or {}).get(f"{phase}_game_items") or {}
    ids, items, have = data.S["item_ids"], data.S["items"], _owned(W)
    picks = [ids.get(str(i)) for i, _ in sorted(pop.items(), key=lambda kv: -(num(kv[1]) or 0))] if isinstance(pop, dict) else []
    if not any(picks) and me["hero"]:
        picks = data.valve_build(me["hero"]).get(SECTION[phase], [])
    picks = [p for p in dict.fromkeys(picks) if p and p not in have]
    if phase != "start":  # popularity is dominated by component buys: prefer finished items, drop parts of other picks
        picks = [p for p in picks if (items.get(p) or {}).get("components") or (phase == "early" and p == "boots")] or picks
        parts = set().union(*(_parts(p) - {p} for p in picks))
        picks = [p for p in picks if p not in parts and not (phase != "late" and "boots" in have and "boots" in _parts(p))]
    enemies = {s for s, e in W["heroes"].items() if not e["ally"]}
    return list(dict.fromkeys(wip + picks[:3] + [ks[0] for heroes, ks in COUNTERS.values() if enemies & set(heroes) and not have & set(ks)]))


def _building(W):
    """Items I'm visibly building: I hold its recipe, or 2+ loose parts worth half its price or more. Best match first, at most 2.
    ponytail: one level of components; a part that fits two items goes to the one it pays more of."""
    me, items = W["me"], data.S["items"]
    loose = [k for k in me["items"] + me["backpack"] + me["stash"] if k and not (items.get(k) or {}).get("components")]
    out = []
    for k, v in items.items():
        comp, cost = v.get("components") or [], v.get("cost") or 0
        if not comp or not cost or k in loose or k.startswith("recipe"):
            continue
        pool, paid, n = list(loose), 0, 0
        for p in comp:
            if p in pool:
                pool.remove(p)
                paid, n = paid + ((items.get(p) or {}).get("cost") or 0), n + 1
        if f"recipe_{k}" in loose or (n >= 2 and paid >= 0.5 * cost):  # boots alone are not "building Tranquils"
            out.append((f"recipe_{k}" not in loose, -paid / cost, k))
    return [k for *_, k in sorted(out)[:2]]


def _parts(k):
    """Item key plus all its recursive components (dotaconstants)."""
    return {k}.union(*(_parts(c) for c in (data.S["items"].get(k) or {}).get("components") or []))


def _owned(W):
    """Owned item keys plus everything they were built from."""
    me = W["me"]
    held = set(me["items"] + me["backpack"] + me["stash"]) - {None}
    if me.get("scepter"):  # consumed Aghanim's (blessing) / shard leave the inventory; GSI keeps the flag
        held |= {"ultimate_scepter", "ultimate_scepter_2"}
    if me.get("shard"):
        held.add("aghanims_shard")
    return set().union(*(_parts(k) for k in held))


def _z(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return lambda v: 0.0
    mu, sd = statistics.fmean(vals), statistics.pstdev(vals) or 1.0
    return lambda v: 0.0 if v is None else (v - mu) / sd


DRAFT_STATES = ("DOTA_GAMERULES_STATE_HERO_SELECTION", "DOTA_GAMERULES_STATE_STRATEGY_TIME")
ROLE_FIT = {1: lambda r: r[:1] == ["Carry"], 2: lambda r: "Nuker" in r[:2] and r[:1] != ["Support"],  # OpenDota roles[0] = primary
            3: lambda r: r[:1] in (["Initiator"], ["Durable"]) or ("Initiator" in r[:2] and r[:1] != ["Support"]),
            4: lambda r: r[:1] == ["Support"] and ("Disabler" in r or "Initiator" in r or "Escape" in r), 5: lambda r: r[:1] == ["Support"]}


def draft(W, c):
    """Pick help in hero selection: strongest heroes for my role in my bracket (or Turbo), smoothed winrate.
    GSI gives a player no draft info, so this ignores picks so far. ponytail: add counters once the draft screen is read."""
    if W["game"].get("state") not in DRAFT_STATES:
        W["draft"] = None
        return
    hs, b, tb = data.S["heroes"], W["settings"].get("bracket"), _turbo(W)
    role = W["settings"].get("role") if W["settings"].get("role_match") == W["game"]["matchid"] else None

    def wr(h):  # (wins + 25) / (picks + 50): low-sample heroes pulled to 50%
        pick, win = (num(h.get("turbo_picks")), num(h.get("turbo_wins"))) if tb else \
            (num(h.get(f"{b}_pick")), num(h.get(f"{b}_win"))) if b else (num(h.get("pub_pick")), num(h.get("pub_win")))
        return ((win or 0) + 25) / ((pick or 0) + 50)

    used = set()

    def best(r, n):
        out = [s for s in sorted(hs, key=lambda s: -wr(hs[s])) if s not in used and ROLE_FIT[r](hs[s].get("roles") or [])][:n]
        used.update(out)
        return out

    src = "Turbo" if tb else f"bracket {b}" if b else "pubs"
    # 3 picks from MY history (OpenDota, own account): heroes I play a lot and win with, recent, fitting the role
    by_id, mine, now = {h.get("id"): s for s, h in hs.items()}, [], __import__("time").time()
    for r in data.my_heroes(W["me"].get("account")) or []:
        s, g, w = by_id.get(r.get("hero_id")), num(r.get("games"), 0) or 0, num(r.get("win"), 0) or 0
        if s and g >= 10 and (not role or ROLE_FIT[role](hs[s].get("roles") or [])):
            recent = 0.03 if now - (num(r.get("last_played"), 0) or 0) < 180 * 86400 else 0
            mine.append(((w + 10) / (g + 20) + recent + 0.01 * min(g, 300) / 30, s, g, w))
    you = [(s, role or next((k for k in range(1, 6) if ROLE_FIT[k](hs[s].get("roles") or [])), None), f"you: {g} games, {w / g:.0%} win")
           for _, s, g, w in sorted(mine, reverse=True)[:3]]
    used.update(s for s, _, _ in you)
    meta = [(s, role) for s in best(role, 5 - len(you))] if role else [(s, r) for r in range(1, 6) for s in best(r, 1)][:5 - len(you)]
    picks = [(s, r, why, "you") for s, r, why in you] + [(s, r, f"meta: {wr(hs[s]):.1%} in {src}", "meta") for s, r in meta]
    mine_h = W["me"].get("hero")  # picked already: show that hero, not more pick options (player, 2026-11-12)
    W["draft"] = {"role": role, "src": src, "picked": mine_h and {"hero": mine_h, "name": (hs.get(mine_h) or {}).get("localized_name") or mine_h},
                  "picks": [{"hero": s, "name": hs[s].get("localized_name") or s, "role": r, "wr": round(wr(hs[s]), 3),
                                                       "why": why, "kind": kind} for s, r, why, kind in picks]}
    if picks:
        names = ", ".join(hs[s].get("localized_name") or s for s, *_ in picks[:3])
        alert(W, "D", "info", f"Strong picks{f' (pos {role})' if role else ''}: {names}", 30)


def weak_heroes(W):
    """R9: rank enemies by weakness. Recomputed only when the enemy set or the data changes."""
    me, hs = W["me"], data.S["heroes"]
    mu = (data.hero_data(me["hero_id"]) or {}).get("matchups")
    enemies = sorted(s for s, e in W["heroes"].items() if not e["ally"])
    b, have, tb = W["settings"].get("bracket"), _owned(W), _turbo(W)
    sig = (tuple(enemies), len(hs), b, bool(mu), tuple(sorted(have)), tb)
    if W["_"].get("weak_sig") == sig:
        return
    W["_"]["weak_sig"] = sig

    def wr(h):
        pick, win = (num(h.get("turbo_picks")), num(h.get("turbo_wins"))) if tb else \
            (num(h.get(f"{b}_pick")), num(h.get(f"{b}_win"))) if b else (num(h.get("pub_pick")), num(h.get("pub_win")))
        return win / pick if pick and win is not None else None

    def armor(h):
        return (num(h.get("base_armor"), 0) or 0) + (num(h.get("base_agi"), 0) or 0) * 0.167

    def hp(h):
        return (num(h.get("base_health"), 0) or 0) + (num(h.get("base_str"), 0) or 0) * 22

    zw, za, zh = _z([wr(h) for h in hs.values()]), _z([armor(h) for h in hs.values()]), _z([hp(h) for h in hs.values()])
    vs = {num(m.get("hero_id")): m for m in mu if isinstance(m, dict)} if isinstance(mu, list) else {}
    out = []
    for s in enemies:
        h, reasons, score, w = hs.get(s), [], 0.0, None
        if h:
            w = wr(h)
            a, b_, c_ = -zw(w), -za(armor(h)), -zh(hp(h))
            score = 0.4 * a + 0.3 * b_ + 0.2 * c_
            reasons += [r for r, z in ((f"winrate {w:.0%}" if w else "", a), ("low armor", b_), ("low HP", c_)) if z > 0.5 and r]
            m = vs.get(num(h.get("id")))
            if m and (num(m.get("games_played")) or 0) >= 30:
                mw = num(m.get("wins"), 0) / num(m.get("games_played"))
                score += 0.1 * (mw - 0.5) * 10
                if mw > 0.55:
                    reasons.append(f"you win {mw:.0%} vs them")
        out.append({"hero": s, "weak_score": round(score, 2), "winrate": round(w, 3) if w else None, "reasons": reasons,
                    "counter_items": [k for heroes, ks in COUNTERS.values() if s in heroes and not have & set(ks) for k in ks]})
    W["enemy_report"] = sorted(out, key=lambda r: -r["weak_score"])


def advice(W, c, D, items):
    """Rules advice in llm.SCHEMA shape (kept in W['_']['advice_raw']). Fresh LLM advice (< 90 s) is shown instead,
    but a newer warn/high alert or a higher rules risk takes over its headline and risk (alerts are authoritative)."""
    me = W["me"]
    act = sorted(W["alerts"], key=lambda a: (-RANK[a["level"]], -a["at"]))
    nxt = next((t for t in W["timers"] if t["in_s"] > 0), None)
    nt = f", next {nxt['name']} {mmss(nxt['at_clock'])}" if nxt else ""
    head = act[0]["text"] if act else ("Pre-game: starting items, plan your lane" if c < 0 else f"Laning: last-hit{nt}" if c < 600
                                       else f"Mid game: farm safe, group for objectives{nt}" if c < 1500 else "Late game: group up, keep buyback")
    do = [a["text"] for a in act[1:] if a["rule"] not in ("T", "R7")][:2]
    heroes, tgt = W["heroes"], None
    for r in W["enemy_report"]:
        e = heroes.get(r["hero"])
        if tgt is None and me["alive"] is not False and e and e["visible"] and not e["dead"] and not any(
                s != r["hero"] and not o["ally"] and o["visible"] and _d((o["x"], o["y"]), (e["x"], e["y"])) <= 1500 for s, o in heroes.items()):
            tgt = r["hero"]
            do.append(f"Kill target: {tgt} (isolated)")
        r["target"] = r["hero"] == tgt  # dashboard TARGET tag
    d = _decision(W, c)
    if d and d["action"]["choice"] in jev.ACTIONS and d["action"]["p"] >= 0.6:  # no model name, no percentage on screen
        where = f" ({d['where']['choice'].replace('_', ' ')})" if d["where"]["choice"] else ""
        do.insert(len(do) - (tgt is not None), jev.ACTIONS[d["action"]["choice"]][0].capitalize() + where)
    miss = _missing(W)
    raw = {"headline": head, "do_now": do[:3], "next_items": items,
           "where": world.zone(me["x"], me["y"], me["team"]) if me["alive"] is not False else "base",
           "risk": "high" if D >= 2.5 else "med" if D >= 1.5 else "low",
           "why": f"danger {D:.1f}, {miss} enemies missing, rosh {W['obj']['rosh']['state']}"}
    W["_"]["advice_raw"] = raw
    la = W["_"].get("llm_advice")
    if not la or c - la["at"] > 90:
        W["advice"] = finish(raw, W, "rules")
        return
    a, have = dict(la), _owned(W)
    if (act and RANK[act[0]["level"]] >= 1 and act[0]["at"] > la["at"]) or RISK[raw["risk"]] > RISK[a["risk"]]:
        a.update(headline=raw["headline"][:90], risk=raw["risk"])
    a["next_items"] = [i for i in la["next_items"] if i["key"] not in have]
    W["advice"] = a


def finish(a, W, src):
    """Schema-shaped advice -> dashboard advice: truncate to prompt limits, resolve item names/costs."""
    me, nxt = W["me"], []
    gold, owned = me["gold"] or 0, _owned(W)
    for n in list(dict.fromkeys(_building(W) + list(a["next_items"])))[:5]:  # what I'm already building comes first, whoever wrote the list
        key, it = data.find_item(n)
        if key and key in owned:
            continue  # the LLM sometimes suggests what you already carry
        cost = num((it or {}).get("cost"))
        nxt.append({"name": (it or {}).get("dname") or str(n).replace("_", " "), "key": key, "cost": cost,
                    "affordable": bool(cost) and gold >= cost})
    return {"headline": a["headline"][:90], "do_now": [s[:60] for s in a["do_now"][:3]], "next_items": nxt,
            "where": a["where"], "risk": a["risk"], "why": a["why"][:200], "src": src, "at": W["game"]["clock"]}


def turbo_hint(W, c):
    """Normal-mode gpm > 700 at 5:00 is very unlikely: suggest the Turbo toggle (GSI has no game-mode field)."""
    if not _turbo(W) and W["game"].get("mode_src") == "manual" and 300 <= c < 330 and (W["me"]["gpm"] or 0) > 700:  # auto handles the rest
        alert(W, "TURBO", "info", "Looks like Turbo? toggle Turbo", 60)


def _decision(W, c):
    d = W.get("decision")
    return d if d and c - d["at_clock"] <= jev.STALE else None


def _short_item(name):
    return name.upper() if len(name) <= 9 else "".join(w[0] for w in name.split()).upper()


def _alert_word(a, daytime):
    if a["rule"] == "T":
        name = a["id"].split(":")[1]
        w, icon = TIMER_WORDS.get(name, (name.upper()[:10], "rune"))
        return ("DAY" if name == "Day/Night" and not daytime else w), icon
    w, icon = WORDS.get(a["rule"], (a["rule"], "danger"))
    if a["rule"] == "R1" and a["level"] == "high":
        w = "DANGER"
    if a["rule"] == "R10" and a["text"].startswith("LET"):
        w, icon = "LET IT GO", "back"
    return w or a["text"].split(":")[0].upper()[:12], icon  # R2: "3 missing: ..." -> "3 MISSING"


def _attention_now(W, c):
    """Current top attention event (id, level, word, icon, text) or None.
    Priority: high alerts > warn alerts > the decision model's warnings (p >= 0.6) > amber alerts (R2/R3 and demoted rules, info)
    > the model's other strong suggestions > skill / buy / me dead / other info alerts > none.
    The text never names the model or shows a percentage."""
    act = sorted(W["alerts"], key=lambda a: (-RANK[a["level"]], -a["at"]))
    top = act[0] if act else None
    if top and top["level"] != "info":
        return (top["id"], top["level"], *_alert_word(top, W["game"]["daytime"]), top["text"])
    me, d, cands = W["me"], _decision(W, c), []
    if d and me["alive"] is not False:
        if d["danger"]["score"] >= 3:
            cands.append(("jev:danger", "warn", "DANGER", "danger", "A gank or a death is likely here"))
        a = d["action"]
        if a["choice"] in jev.ACTIONS and a["p"] >= 0.6:
            desc, w, icon, lvl = jev.ACTIONS[a["choice"]]
            cands.append((f"jev:{a['choice']}", lvl, w, icon, desc.capitalize()))
        if d["rosh_taken"]["p"] >= 0.6:
            cands.append(("jev:rosh", "warn", "ROSH", "rosh", "The enemies are likely taking Roshan"))
        if d["fight"]["p"] >= 0.6:
            cands.append(("jev:fight", "info", "FIGHT", "fight", "Take the fight now"))
    amber = next((a for a in act if a["rule"] in ("R2", "R3") or a.get("soft")), None)
    cands = sorted(cands, key=lambda x: -RANK[x[1]])  # stable: first of the highest level
    if cands and (cands[0][1] != "info" or not amber):
        return cands[0]
    if amber:
        return (amber["id"], "info", *_alert_word(amber, W["game"]["daytime"]), amber["text"])
    sk = W.get("skill") or {}
    if sk.get("points") and sk.get("next") and me["alive"] is not False:
        n = sk["next"]
        return (f"skill:{me['level']}", "info", f"SKILL {n['slot'] if n['slot'] != 'talent' else 'TALENT'}", "item",
                f"Level up: {n['label']}" + (f" ({n['slot']})" if n["slot"] != "talent" else ""))
    nxt = (W["advice"].get("next_items") or [None])[0]
    if nxt and nxt["affordable"] and nxt["key"] and (me["alive"] is False or world.zone(me["x"], me["y"], me["team"]) == "base"):
        return (f"buy:{nxt['key']}", "info", f"BUY {_short_item(nxt['name'])}", "item", f"Buy {nxt['name']} ({nxt['cost']}g)")
    if me["alive"] is False:
        return ("dead", "info", f"DEAD {me['respawn_s'] or 0}s", "dead", f"Dead: respawn in {me['respawn_s'] or 0}s")
    if top:
        return (top["id"], top["level"], *_alert_word(top, W["game"]["daytime"]), top["text"])
    return None


def attention(W, c):
    """W["attention"]: one thing to look at. seq +1 when the shown event is new (not shown in the last 30 s) or escalated;
    falling back to one shown recently (or a 1 s DEAD countdown) does not bump seq."""
    A, cur, seen = W["attention"], _attention_now(W, c), W["_"].setdefault("att_seen", {})
    if cur is None:
        if A["sticky"]:
            A.update(sticky=False, since_clock=c)  # ended: linger LINGER s, then clear
        elif A["level"] != "none" and c - (A["since_clock"] or c) > LINGER:
            A.update(id=None, level="none", word=None, icon=None, text=None, since_clock=None, dir=None)
        return
    i, lvl, word, icon, text = cur
    r, (t0, r0) = RANK[lvl], seen.get(i, (-1e9, -1))
    if c - t0 > 30 or r > r0:
        A["seq"] += 1
    if i != A["id"] or lvl != A["level"] or not A["sticky"]:
        A["since_clock"] = c
    seen[i] = (c, r)
    A.update(id=i, level=lvl, word=word, icon=icon, text=text[:70], sticky=True,
             dir=next((a.get("dir") for a in W["alerts"] if a["id"] == i and a["rule"] == "R1"), None))


def glance(W, c, D):
    """W["glance"]: compact summary for the Glance view / overlay."""
    d, r = _decision(W, c), W["obj"]["rosh"]
    items = W["advice"].get("next_items") or []
    pick = d and d["next_item"] and next((i for i in items if i["key"] == d["next_item"]["choice"]), None)
    nt = next((t for t in W["timers"] if t["in_s"] > 0), None)
    k = 0.5 if _turbo(W) else 1  # one phase cut-off for rules and UI: laning < 10:00, late >= 30:00 (Turbo halves)
    W["phase"] = ph = "pre" if c < 0 else "laning" if c < 600 * k else "mid" if c < 1800 * k else "late"
    if d and d["action"]["choice"] in jev.ACTIONS and d["action"]["p"] >= 0.6:  # the model's word only when it is confident
        _, w, icon, _ = jev.ACTIONS[d["action"]["choice"]]
        act = {"word": w, "icon": icon, "p": d["action"]["p"], "src": "model"}
    else:  # otherwise the plain default for this phase and role
        farm = (W["me"]["role"] or 5) <= (3 if ph == "laning" else 2)
        w, icon = ("GET READY", "ok") if ph == "pre" else ("GROUP", "fight") if ph == "late" or (ph == "mid" and not farm) else \
            ("FARM", "farm") if farm else ("SUPPORT", "ok")
        act = {"word": w, "icon": icon, "p": None, "src": "phase"}
    if r["state"] == "dead" and r["killed_at"] is not None and r["src"] != "gsi":
        rosh = f"dead, up {mmss(r['killed_at'] + 480)}-{mmss(r['killed_at'] + 660)}"
    else:
        rosh = r["state"] + (f" {r['pit']}" if r["state"] != "dead" else "") + \
            (f" ({int(r['respawn_in_s'])}s)" if r["src"] == "gsi" and r["respawn_in_s"] else "")
    if r["aegis_until"] and r["aegis_until"] > c:
        rosh += f", aegis until {mmss(r['aegis_until'])}"
    W["glance"] = {"next_item": pick or (items[0] if items else None),
                   "next_timer": {"name": nt["name"], "in_s": nt["in_s"]} if nt else None,
                   "mia": _missing(W),
                   "danger": round(d["danger"]["score"]) if d else 4 if D >= 2.5 else 3 if D >= 1.5 else 2 if D >= 1 else 1 if D > 0 else 0,
                   "action": act, "rosh": rosh}
