"""Fog layer: W["hud"]["fog"], one entry per enemy hero that is alive and not in sight. Where it was last seen and how much that
spot is still worth (conf), and whether it could be on me within ~10 s (p10 -> tier, with a 15 s life). Tables only: stdlib, no numpy,
no model file. No cue before the hero has been unseen CUE_MIN_S; any real sighting earns a ghost; only a respawn has no position.
    build(heroes, me, c, vis, st) -> list of entries (called by rules.evaluate, guarded there)
    python fog.py          self-check
"""
import math

# P(seen within 1200 u of me in the next 10 s) [fight][distance last_xy -> me][unseen time]; 12 train Turbo games (lead/odds_table.json)
D_E = (0, 800, 1200, 1600, 2000, 2500, 3000, 4000, 5000, 7000)
A_E = (0, 2, 4, 7, 10, 15, 20, 30, 45)
ODDS = (((.68, .39, .34, .16, .05, .05, .05, .05, .01), (.72, .47, .44, .38, .35, .26, .14, .04, .04), (.43, .41, .31, .31, .14, .14, .11, .05, .05),
         (.33, .35, .25, .12, .12, .12, .09, .07, .07), (.22, .21, .19, .16, .14, .04, .04, .01, .01), (.17, .16, .14, .08, .06, .06, .06, .04, .04),
         (.15, .14, .12, .07, .06, .06, .05, .01, .01), (.05, .04, .03, .05, .05, .05, .03, .01, .01), (.02, .02, .04, .04, .04, .05, .07, .07, .07),
         (.03, .03, .02, .02, .03, .03, .02, .04, .02)),
        ((.68, .66, .56, .50, .17, .04, .04, .04, .04), (.70, .60, .59, .51, .51, .27, .15, .15, .15), (.53, .46, .42, .31, .25, .25, .25, .25, .02),
         (.47, .40, .36, .28, .28, .28, .23, .19, .16), (.33, .41, .25, .22, .22, .17, .17, .13, .13), (.13, .24, .19, .27, .21, .11, .11, .09, .09),
         (.15, .17, .18, .16, .16, .05, .05, .04, .04), (.03, .03, .04, .10, .10, .09, .09, .09, .06), (.06, .03, .03, .04, .08, .11, .21, .23, .28),
         (.03, .03, .05, .05, .07, .08, .09, .12, .11)))
# P(the hero shows up within 1000 u of its last spot) by unseen seconds: last spot outside / inside (> 300 u deep) my team's vision
CONF_DARK = ((2, .85), (3.5, .55), (5, .36), (8, .30), (11, .22), (14, .16), (20, .12), (45, .08))
CONF_LIT = ((2, .92), (3.5, .80), (5, .68), (7, .62), (9, .50), (11, .42), (20, .36), (30, .27), (45, .10))
LIT_DEPTH = 300      # u inside a vision disc
HINT_CAP = .45       # a summon is not its owner: never a solid ghost
GHOST = .30          # conf at which the overlay draws the ghost (documented here, applied in overlay.html)
FIGHT_R = 2000       # an enemy in sight this close to me = "fight" column of ODDS
SOFT_ON, SOFT_OFF, HARD_ON, HARD_OFF = .20, .12, .40, .25
CONFIRM_S, RELEASE_S, LIFE_S, MAX_CUES = 1.0, 2.0, 15.0, 2
CUE_MIN_S = 2.0      # no cue while the hero has been unseen for less: icons drop out for < 2 s 10-22 times per 10 min near me (4 Hz GSI)
SAME_S = 1.5         # the time of the last evidence (clock - unseen time) moving by more than this = a new unseen stretch
MOVED_S = 0.3        # ... and by more than this = located again between two ticks: its unseen time starts over (clock - mia_s is steady to 0.1 s)


def _interp(kn, a):
    if a <= kn[0][0]:
        return kn[0][1]
    for (a0, v0), (a1, v1) in zip(kn, kn[1:]):
        if a <= a1:
            return v0 + (v1 - v0) * (a - a0) / (a1 - a0)
    return kn[-1][1]


def _bin(edges, v):
    return max(i for i, e in enumerate(edges) if v >= e)


def odds(fight, dist, age):
    return ODDS[int(bool(fight))][_bin(D_E, max(0, dist))][_bin(A_E, max(0, age))]


def lit(vis, x, y):
    """True when (x, y) is more than LIT_DEPTH inside one of my team's vision discs. vis = iterable of (x, y, visionrange)."""
    return any(r - LIT_DEPTH > 0 and (vx - x) ** 2 + (vy - y) ** 2 < (r - LIT_DEPTH) ** 2 for vx, vy, r in vis)


def build(heroes, me, c, vis, st):
    """heroes = W["heroes"], me = W["me"], c = game clock (s), vis = my team's vision sources [(x, y, r)], st = dict kept between calls."""
    ok = bool(me.get("alive")) and me.get("x") is not None and me.get("y") is not None
    pos = (me["x"], me["y"]) if ok else None
    foes = [(s, e) for s, e in heroes.items() if e.get("team") and not e.get("ally")]
    fight = ok and any(e["visible"] and not e.get("dead") and e.get("x") is not None and math.dist((e["x"], e["y"]), pos) <= FIGHT_R for _, e in foes)
    order = st.setdefault("order", [])
    out = []
    for s, e in foes:
        if s not in order:
            order.append(s)  # ponytail: first-seen order; the top-bar slot (e["slot"]) replaces it once CV knows all five
        if e["visible"] or e.get("dead") or not e.get("last_xy") or e.get("mia_s") is None:
            st.pop(s, None)
            continue
        # illusion: the last copy in sight died with no kill event (or its corpse is still waiting for one): no position, like a respawn (user 2026-11-25)
        src = "respawn" if e.get("resp_at") is not None else "illusion" if e.get("illusion") or e.get("mark_fresh") else "hint" if e.get("hint_at") is not None else "seen"
        age, (x, y) = e["mia_s"], e["last_xy"]
        h, ev = st.get(s), c - age
        if h is None or h["src"] != src or abs(h["t0"] - ev) > SAME_S:  # new evidence = new unseen stretch
            h = st[s] = {"src": src, "t0": ev, "ev": ev, "age": age, "ttl": 1.0, "conf": 1.0, "soft": False, "hard": False, "up": None, "low": None, "hlow": None, "cue": None, "shown": False, "done": False}
        # Live, the clock is a whole GSI second + the time since the last payload, so it steps back by up to 0.25 s four times a second
        # and mia_s with it. Within a stretch age only grows (and ttl only falls): the overlay's 2 s / 3 s / 8 s steps must not flap.
        if abs(h["ev"] - ev) > MOVED_S:
            h["ev"], h["age"] = ev, age
        age = h["age"] = max(h["age"], age, 0.0)  # (0: right after a sighting the stepped-back clock reads mia_s -0.1)
        conf = 0.0
        if src not in ("respawn", "illusion"):  # a mark needs a real sighting or a summon; the overlay's 1-3 s fade-in is what hides flicker
            conf = _interp(CONF_LIT if lit(vis, x, y) else CONF_DARK, age)
            conf = h["conf"] = min(h["conf"], HINT_CAP if src == "hint" else 1.0, conf)  # never rises within a stretch: a ghost cannot come back
        p = odds(fight, math.dist((x, y), pos), age) if ok and src != "illusion" else 0.0
        pc = p if age >= CUE_MIN_S else 0.0  # the cue state machine is blind to the flicker window; p10 itself is published as is
        if not ok:
            h.update(soft=False, hard=False, up=None, low=None, hlow=None)
        else:
            if not h["soft"]:
                h["up"] = (h["up"] if h["up"] is not None else c) if pc >= SOFT_ON else None
                if h["up"] is not None and c - h["up"] >= CONFIRM_S:
                    h.update(soft=True, low=None)
            else:
                h["low"] = None if pc >= SOFT_OFF else (h["low"] if h["low"] is not None else c)
                if h["low"] is not None and c - h["low"] >= RELEASE_S:
                    h.update(soft=False, up=None)
            if not h["hard"]:
                if pc >= HARD_ON:
                    h.update(hard=True, hlow=None)
            else:
                h["hlow"] = None if pc >= HARD_OFF else (h["hlow"] if h["hlow"] is not None else c)
                if h["hlow"] is not None and c - h["hlow"] >= RELEASE_S:
                    h["hard"] = False
        tier = 0 if src in ("respawn", "illusion") else 2 if h["hard"] else 1 if h["soft"] else 0  # a respawned hero has no position to point at
        if tier and h["cue"] is None:
            h["cue"] = c
        if (h["shown"] and not tier) or (h["cue"] is not None and c - h["cue"] >= LIFE_S):
            h["done"] = True  # one cue per unseen stretch: once it has ended (bar ran out, odds fell, I died) it stays off. Latched: the live clock steps back
        if h["done"]:
            tier = 0
        out.append({"hero": s, "slot": order.index(s) % 5, "age": round(age, 1), "x": None if src in ("respawn", "illusion") else round(x), "y": None if src in ("respawn", "illusion") else round(y),
                    "conf": round(conf, 2), "src": src, "p10": round(p, 2), "tier": tier, "ttl": 0})
        if tier:
            out[-1]["ttl"] = h["ttl"] = min(h["ttl"], max(.01, round(1 - (c - h["cue"]) / LIFE_S, 2)))
    if all(e.get("slot") is not None for _, e in foes) and len(foes) == 5:
        for o in out:
            o["slot"] = heroes[o["hero"]]["slot"] % 5
    # at most MAX_CUES. A cue that is on screen keeps its place (ranking by odds alone swapped heroes and swapped them back); the rest by (tier, p10)
    cued = sorted((o for o in out if o["tier"]), key=lambda o: (not st[o["hero"]]["shown"], -o["tier"], -o["p10"]))
    for o in cued[:MAX_CUES]:
        st[o["hero"]]["shown"] = True
    for o in cued[MAX_CUES:]:
        o["tier"], o["ttl"] = 0, 0
    return out


def demo():
    H = lambda **k: {"team": "dire", "ally": False, "visible": False, "dead": False, "x": None, "y": None, "last_xy": [1000, 0], "mia_s": 0.0, "vanished_at": 0.0, **k}
    me, st = {"alive": True, "x": 0, "y": 0}, {}
    seq = [build({"lion": H(mia_s=float(t), vanished_at=100.0)}, me, 100.0 + t, [], st)[0] for t in range(0, 40)]
    assert seq[2]["conf"] >= .8 and seq[2]["tier"] == 2, seq[2]                 # 1000 u away, unseen 2 s: solid ghost, close cue
    assert seq[0]["tier"] == seq[1]["tier"] == 0 and seq[1]["p10"] > 0            # never a cue in the first 2 s (flicker), p10 still published
    assert all(a["conf"] >= b["conf"] for a, b in zip(seq, seq[1:]))            # confidence never rises
    assert seq[9]["conf"] < GHOST <= seq[7]["conf"], (seq[7], seq[9])            # a dark ghost is gone by ~9 s
    assert seq[18]["tier"] == 0 and seq[16]["tier"] > 0 and seq[16]["ttl"] < .15  # the cue lives 15 s (from 2 s unseen), then stays off
    assert all(o["tier"] == 0 for o in seq[18:])
    st = {}
    lit_seq = [build({"lion": H(mia_s=float(t), last_xy=[6000, 0])}, me, 100.0 + t, [(6000, 300, 1800)], st)[0] for t in range(0, 25)]
    assert lit_seq[20]["conf"] >= GHOST and lit_seq[20]["tier"] == 0             # vanished inside my vision, far away: ghost stays, no cue
    st = {}
    assert build({"lion": H(mia_s=5.0, vanished_at=None)}, me, 100.0, [], st)[0]["conf"] > 0    # a brief sighting (< 2 s visible) is still a sighting
    r = build({"lion": H(mia_s=5.0, resp_at=95.0, last_xy=[7000, 6400])}, me, 100.0, [], st)[0]
    assert r["src"] == "respawn" and r["x"] is None and r["conf"] == 0 and r["tier"] == 0, r
    r = build({"lion": H(mia_s=5.0, last_xy=[1000, 0], illusion=True)}, me, 100.0, [], {})[0]   # its last copy in sight was an illusion that died
    assert r["src"] == "illusion" and r["x"] is None and r["p10"] == 0 and r["tier"] == 0, r
    hint = build({"lion": H(mia_s=3.0, vanished_at=None, hint_at=100.0)}, me, 100.0, [], st)[0]
    assert hint["src"] == "hint" and 0 < hint["conf"] <= HINT_CAP, hint
    st = {}
    five = {n: H(mia_s=3.0, last_xy=[1300, 100 * i]) for i, n in enumerate("abcde")}
    first = {o["hero"] for o in build(five, me, 100.0, [], st) if o["tier"]}
    assert len(first) == 2, first                                                # never more than two cues
    five["e"]["last_xy"] = [900, 0]                                              # a third hero's odds overtake: the two cues on screen stay
    assert {o["hero"] for o in build(five, me, 100.5, [], st) if o["tier"]} == first == {"a", "b"}
    assert all(o["tier"] == 0 and o["p10"] == 0 for o in build(five, {"alive": False, "x": 0, "y": 0}, 101.0, [], st))  # I am dead: marks, no cues
    assert build({"lion": H(visible=True)}, me, 100.0, [], {}) == [] and build({"lion": H(dead=True)}, me, 100.0, [], {}) == []
    st = {}
    jit = [build({"lion": H(mia_s=m)}, me, 100.0 + m, [], st)[0] for m in (1.8, 2.0, 1.9, 2.1, 1.9)]  # the live clock steps back by a fraction of a second
    assert [o["age"] for o in jit] == [1.8, 2.0, 2.0, 2.1, 2.1] and [o["tier"] for o in jit] == [0, 2, 2, 2, 2], jit  # age and the cue do not flap
    assert all(a["ttl"] >= b["ttl"] for a, b in zip(jit[1:], jit[2:]))
    end = [build({"lion": H(mia_s=m)}, me, 100.0 + m, [], st)[0] for m in (16.95, 17.1, 16.9, 17.2)]  # ... also at the end of the cue's life: off stays off
    assert [o["tier"] for o in end] == [2, 0, 0, 0] and end[0]["ttl"] > 0, end
    assert build({"lion": H(mia_s=-0.1)}, me, 100.0, [], {})[0]["age"] == 0     # ... and never a negative unseen time
    st = {}
    walk = [build({"lion": H(mia_s=float(t), last_xy=[1000, 0])}, {"alive": True, "x": x, "y": 0}, 100.0 + t, [], st)[0]["tier"] for t, x in enumerate((0, 0, 0, 0, 0, -4000, -4000, -4000, -4000, 0, 0, 0, 0))]
    assert walk[4] == 2 and not any(walk[7:]), walk                              # I walked off and back: a cue that ended does not return in the same unseen stretch
    st = {}
    turn = [build({"lion": H(mia_s=float(t), last_xy=[6000, 0])}, me, 100.0 + t, [(6000, 300, 1800)] if t >= 10 else [], st)[0]["conf"] for t in range(8, 13)]
    assert all(a >= b for a, b in zip(turn, turn[1:])) and turn[-1] < GHOST, turn  # the spot turning lit later does not bring a faded ghost back
    st = {}
    build({"lion": H(mia_s=1.2)}, me, 101.2, [], st)
    assert build({"lion": H(mia_s=0.2)}, me, 101.6, [], st)[0]["age"] == 0.2  # seen again between two ticks: the unseen time starts over
    print("fog demo ok")


if __name__ == "__main__":
    demo()
