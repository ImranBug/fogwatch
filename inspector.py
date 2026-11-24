"""Inventory reading from the bottom HUD (classical CV on the captured frame only; never game memory).

When the player clicks another hero, Dota's bottom HUD shows THAT unit's abilities and 6 inventory slots (+ neutral).
GSI reports only your own hero, so: find the 3x2 slot grid (its x moves with the ability count), match each slot
against the CDN item icons, and decide whose HUD it is (items == GSI's own items -> me; else the lineup hero whose
ability icons match). The backpack row sits below the visible frame with this user's clipped geometry -> [None]*3.
Nothing here claims what an enemy "plans to buy": likely_next is a prediction (OpenDota itemPopularity minus owned).
"""
import json, logging, threading, time
from pathlib import Path
import cv2, numpy as np
import data, vision

log = logging.getLogger("inspect")
ITEM_CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/items/"
ABIL_CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/abilities/"
# HUD layout in 1080-ref units (measured on 112 recorded frames, 2560x1440 window, 1728x1117 render; x from content centre,
# y from the render's bottom edge). Grid x0 moves with the selected unit's ability count -> searched in GX.
ICON = (62, 45)            # item icon w, h
PITCH = (65.6, 48.75)      # slot pitch x, y
GY = 136                   # row-1 top, above the render bottom
GX = (100, 330)            # search range of the grid's left edge
SEP = 52                   # slot-frame grey (separators between slots)
NEUTRAL = (221, -18, 14.3)  # neutral circle centre (x from x0, y from row-2 centre), inscribed half-size; ref units
NS = (24, 20)              # matcher resolution (w, h) of one icon
T_ITEM, MARGIN, T_NEUTRAL = 0.5, 0.08, 0.7  # 0 wrong labels on 499 recorded item slots (51 unread: cooldown)
FREE = {"aegis", "ward_observer", "roshans_banner", "cheese", "refresher_shard", "tome_of_aghanim"}  # cost 0 in dotaconstants
_S = {"bank": None, "bank_key": None, "loading": False, "abil": {}, "habil": None}
_LOCK = threading.Lock()


# ---------- geometry ----------

def grid(frame, g):
    """-> (x0, y0, (iw, ih), (px, py), score) slot-grid origin in frame px; score = separator misfit (lower = better)."""
    sx, sy, ox, oy, cw, ch = g
    C, y0 = ox + cw / 2, round(oy + ch - GY * sy)
    iw, ih, px, py = ICON[0] * sx, ICON[1] * sy, PITCH[0] * sx, PITCH[1] * sy
    lo, hi = int(C + GX[0] * sx), int(C + GX[1] * sx)
    a, b = y0 + round(4 * sy), y0 + round(2 * py - 6 * sy)
    if b > frame.shape[0] or hi + 3 * px + 2 > frame.shape[1]: return None
    base = lo - 10
    gray = frame[a:b, base:int(hi + 3 * px) + 2].astype(np.float32).mean(2)
    m, s = gray.mean(0), gray.std(0)
    bw = round(4 * sx) - 1                          # separator band: columns c-round(4 sx) .. c-2 left of each slot
    win = np.lib.stride_tricks.sliding_window_view
    band = np.abs(win(m, bw).mean(1) - SEP) + win(s, bw).mean(1) + np.ptp(win(m, bw), 1)
    xs = np.arange(lo, hi)
    sc = sum(band[np.round(xs + k * px).astype(int) - base - round(4 * sx)] for k in range(4))
    hy = y0 + round(py - 4 * sy)                    # horizontal separator between the rows: flat
    hb = frame[hy:hy + 2].astype(np.float32).mean(2)
    L = int(3 * px - 4)
    c1 = np.concatenate([[0], np.cumsum(hb.sum(0))]); c2 = np.concatenate([[0], np.cumsum((hb ** 2).sum(0))])
    n = 2 * L; mu = (c1[xs + L] - c1[xs]) / n
    sc = sc + 4 * np.sqrt(np.maximum((c2[xs + L] - c2[xs]) / n - mu ** 2, 0))
    j = int(np.argmin(sc))
    return int(xs[j]), y0, (iw, ih), (px, py), float(sc[j])


# ---------- item templates ----------

def _feat(img):
    """BGR icon crop -> zero-mean unit-norm vector, hotkey + charges corners masked."""
    v = cv2.resize(img, NS, interpolation=cv2.INTER_AREA).astype(np.float32)
    v = (v - v.mean()) * _mask()
    n = float(np.linalg.norm(v))
    return (v / n).reshape(-1) if n > 1e-3 else np.zeros(v.size, np.float32)


def _mask():
    if "mask" not in _S:
        w, h = NS; m = np.ones((h, w, 1), np.float32)
        m[:round(h * 0.3), :round(w * 0.25)] = 0      # hotkey letter (own hero only)
        m[round(h * 0.6):, round(w * 0.62):] = 0      # charges / stack count
        _S["mask"] = m
    return _S["mask"]


def item_keys():
    return [k for k, v in data.S["items"].items() if v.get("img") and not k.startswith("recipe")] if data.S["items"] else \
        sorted(p.stem for p in (vision.CACHE / "items").glob("*.png"))


def _bank():
    """(keys, F[n x d]) item icon features; icons fetched once into data/cache/items/ (background thread)."""
    d = vision.CACHE / "items"
    keys = [k for k in item_keys() if (d / f"{k}.png").exists()]
    if len(keys) < len(item_keys()) and not _S["loading"]:
        _S["loading"] = True
        def load():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(8) as ex: list(ex.map(lambda k: vision._fetch(ITEM_CDN + k + ".png", d / f"{k}.png"), item_keys()))
            _S["loading"] = False
        threading.Thread(target=load, name="item-icons", daemon=True).start()
    if _S["bank_key"] != len(keys):
        fs = {k: img for k in keys if (img := cv2.imread(str(d / f"{k}.png"))) is not None}
        main = [k for k in fs if (data.S["items"].get(k) or {}).get("cost") or k in FREE] or list(fs)  # shop items; neutrals: neutral slot
        _S["bank"] = (main, np.stack([_feat(fs[k]) for k in main]) if main else np.zeros((0, NS[0] * NS[1] * 3), np.float32), list(fs))
        _S["bank_key"] = len(keys)
    return _S["bank"]


def _crops(frame, x, y, iw, ih, r=2):
    """Features of the icon box at (x, y) shifted by -r..r px (both axes)."""
    out = []
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            a, b = round(y + dy), round(x + dx)
            c = frame[a:a + round(ih), b:b + round(iw)]
            if c.shape[:2] == (round(ih), round(iw)): out.append(_feat(c))
    return np.stack(out) if out else None


def _empty(c):
    """Empty slot: dark and flat (measured: empty ~20-40 grey, std < 12; items std > 25)."""
    g = c.astype(np.float32).mean(2)
    return g.mean() < 60 and g.std() < 14


def read_items(frame, g):
    """-> {"items": [key|None]*6, "conf": [float]*6, "neutral": key|None, "x0": int, "grid": score} or None (no grid)."""
    gr = grid(frame, g)
    if gr is None: return None
    x0, y0, (iw, ih), (px, py), gsc = gr
    keys, F, allk = _bank()
    items, conf, raw = [None] * 6, [0.0] * 6, []
    for i in range(6):
        x, y = x0 + (i % 3) * px, y0 + (i // 3) * py
        c = frame[round(y):round(y + ih), round(x):round(x + iw)]
        P = _crops(frame, x, y, iw, ih)
        if P is None or not len(keys): continue
        S = (P @ F.T).max(0)
        o = np.argsort(-S)[:2]; j, best, second = int(o[0]), float(S[o[0]]), float(S[o[1]])
        raw.append((keys[j], round(best, 3), round(second, 3), _empty(c)))
        if _empty(c) and best < 0.75: items[i], conf[i] = None, round(1 - best, 3)
        elif best >= T_ITEM and best - second >= MARGIN: items[i], conf[i] = keys[j], round(best, 3)
        # else: unreadable (cooldown sweep / overlay): None with conf 0
    sx, sy = g[0], g[1]
    nx, ny = x0 + NEUTRAL[0] * sx, y0 + 1.5 * py + NEUTRAL[1] * sy
    neutral = _neutral(frame, nx, ny, NEUTRAL[2] * sx, NEUTRAL[2] * sy, allk)
    return {"items": items, "conf": conf, "neutral": neutral, "x0": x0, "grid": round(gsc, 1), "raw": raw}


def _neutral(frame, cx, cy, rx, ry, keys):
    """Neutral item: the circle shows the icon's centre; compare the inscribed square with the icons' centres."""
    nk = [i for i, k in enumerate(keys) if (data.S["items"].get(k) or {}).get("tier")]
    if not nk: return None
    if "nfeat" not in _S or _S.get("nfeat_key") != len(keys):
        d = vision.CACHE / "items"
        def cen(img):
            h, w = img.shape[:2]; s = h * 0.7  # the circle shows the icon's central 0.7 h square (fitted on 66 frames)
            return img[round(h / 2 - s / 2):round(h / 2 + s / 2), round(w / 2 - s / 2):round(w / 2 + s / 2)]
        _S["nfeat"] = np.stack([_cfeat(cen(cv2.imread(str(d / f"{keys[i]}.png")))) for i in nk]); _S["nfeat_key"] = len(keys)
    best = (-1.0, None)
    for dy in range(-3, 4, 1):
        for dx in range(-3, 4, 1):
            a, b = round(cy - ry + dy), round(cx - rx + dx)
            c = frame[max(a, 0):a + round(2 * ry), max(b, 0):b + round(2 * rx)]
            if c.shape[:2] != (round(2 * ry), round(2 * rx)): continue
            S = _S["nfeat"] @ _cfeat(c)
            j = int(S.argmax())
            if S[j] > best[0]: best = (float(S[j]), keys[nk[j]])
    return best[1] if best[0] >= T_NEUTRAL else None


def _cfeat(img):
    v = cv2.resize(img, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32); v -= v.mean()
    n = float(np.linalg.norm(v)); return (v / n).reshape(-1) if n > 1e-3 else np.zeros(16 * 16 * 3, np.float32)


# ---------- whose HUD is it: ability icons ----------

ABIL = 53                  # ability icon side (ref units), left of the inventory grid
_HA = {}


def _abilities(hero):
    """Ability names (dotaconstants hero_abilities, cached under data/cache/<build>/); [] offline."""
    if "ha" not in _HA: _HA["ha"] = data.fetch("hero_abilities.json", data.DC + "/hero_abilities.json") or {}
    a = (_HA["ha"].get("npc_dota_hero_" + hero) or {}).get("abilities") or []
    return [n for n in a if n != "generic_hidden" and "empty" not in n]


def _abil_icon(name):
    if name not in _S["abil"]:
        _S["abil"][name] = vision._fetch(ABIL_CDN + name + ".png", vision.CACHE / "abilities" / f"{name}.png")
    return _S["abil"][name]


def _abil_tpl(name, w, h):
    key = (name, w, h)
    if key not in _S["abil"]:
        ic = _abil_icon(name)
        _S["abil"][key] = None if ic is None else cv2.resize(ic, (w, h), interpolation=cv2.INTER_AREA)[h // 6:-h // 6, w // 6:-w // 6]
    return _S["abil"][key]


def identify(frame, g, x0, heroes, k=0.33):
    """-> [(hero, score)] best first: mean of each hero's top-3 ability-icon matches (TM_CCOEFF_NORMED, 1/3 res: ~55 ms for 10 heroes)
    in the ability strip left of the grid at x0. Icon centres only (hotkey, pips, mana cost sit at the edges)."""
    sx, sy, ox, oy, cw, ch = g
    y0 = oy + ch - GY * sy
    a, b = int(y0 - 8 * sy), int(y0 + (ABIL + 8) * sy)
    l, r = int(ox + cw / 2 - 420 * sx), int(x0 - 2)
    strip = cv2.resize(frame[a:b, max(l, 0):r], None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    out = []
    for h in heroes:
        sc = []
        for n in _abilities(h):
            for z in (1.0, 0.85):  # heroes with many abilities get smaller buttons
                t = _abil_tpl(n, round(ABIL * sx * k * z), round(ABIL * sy * k * z))
                if t is not None and strip.shape[0] >= t.shape[0] and strip.shape[1] >= t.shape[1]:
                    sc.append(float(cv2.minMaxLoc(cv2.matchTemplate(strip, t, cv2.TM_CCOEFF_NORMED))[1]))
        if sc: out.append((h, round(float(np.mean(sorted(sc)[-3:])), 3)))
    return sorted(out, key=lambda x: -x[1])


# ---------- per frame: whose HUD, fuse readings, W["inspect"] ----------

T_GRID, T_HERO, HERO_MARGIN = 135, 0.75, 0.08  # grid misfit: HUD frames 50-120, menus/loading 150+; hero (1/3 res): true 0.79-0.95, runner-up >= 0.10 below
LIVE = ("DOTA_GAMERULES_STATE_PRE_GAME", "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS")
ST = {"mid": None, "last": None, "seen": {}, "ep": None, "logged": None}


def _vs_me(r, me):
    """(agree, disagree) between a reading and GSI's own 6 slots; unread slots count for neither."""
    a = d = 0
    for k, c, t in zip(r["items"], r["conf"], me.get("items") or [None] * 6):
        if not c: continue
        if k == t: a += 1
        else: d += 1
    return a, d


def selected(frame, g, W, r=None):
    """-> (hero|None, reading, conf). Own items match -> me (no ability matching); else best ability match among
    the 10 known heroes (incl. me: GSI items may lag a purchase). None = not a known hero (creep, courier, building)."""
    r = r or read_items(frame, g)
    me = W.get("me") or {}
    if r is None or r["grid"] > T_GRID: return None, r, 0.0
    a, d = _vs_me(r, me)
    if d == 0 and a >= 1 and me.get("hero"): return me["hero"], r, 1.0
    heroes = {h for h in (W.get("lineup") or {}).get("heroes") or [] if h} | set(W.get("heroes") or {}) | ({me["hero"]} if me.get("hero") else set())
    key = (r["x0"], tuple(r["items"]), tuple(sorted(heroes)))
    if _S.get("id_key") != key:  # same non-hero HUD (courier, creep) as last pass: skip the ~150 ms ability match
        _S["id_key"], _S["id_res"] = key, identify(frame, g, r["x0"], sorted(heroes))
    ranked = _S["id_res"]
    if not ranked or ranked[0][1] < T_HERO or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < HERO_MARGIN): return None, r, 0.0
    return ranked[0][0], r, ranked[0][1]


def _parts(k):
    return {k}.union(*(_parts(c) for c in (data.S["items"].get(k) or {}).get("components") or []))


def value(items):
    return sum(int((data.S["items"].get(k) or {}).get("cost") or 0) for k in items if k)


def likely_next(hero, items, clock, turbo=False):
    """PREDICTION, not seen: the hero's popular items for this game phase (OpenDota itemPopularity) minus owned items
    and their components; finished items only. [] until hero data is cached."""
    hid = (data.S["heroes"].get(hero) or {}).get("id")
    k = 0.5 if turbo else 1
    phase = "early" if clock < 600 * k else "mid" if clock < 1500 * k else "late"
    pop = ((data.hero_data(hid) or {}).get("items") or {}).get(f"{phase}_game_items") or {}
    have = set().union(*(_parts(i) for i in items if i)) if any(items) else set()
    ids = data.S["item_ids"]
    picks = [ids.get(str(i)) for i, _ in sorted(pop.items(), key=lambda kv: -(kv[1] or 0))] if isinstance(pop, dict) else []
    picks = [p for p in dict.fromkeys(picks) if p and p not in have and (data.S["items"].get(p) or {}).get("components")]
    return picks[:3]


def step(frame, W, t=None):
    """One inspection pass on `frame` with world snapshot W. Updates ST; returns the new enemy record to log, or None."""
    g = (W.get("game") or {})
    if g.get("matchid") != ST["mid"]: ST.update(mid=g.get("matchid"), last=None, seen={}, ep=None, logged=None)
    if g.get("state") not in LIVE or not (W.get("me") or {}).get("hero"): return None
    if "aspect" not in _S: _S["aspect"] = vision._S["aspect"] if "aspect" in vision._S else vision.render_aspect()
    geom = vision.content_geom(frame, _S["aspect"])
    if geom is None: return None
    hero, r, conf = selected(frame, geom, W)
    clock = g.get("clock") or 0
    if hero is None or hero == W["me"]["hero"]:
        ST["ep"] = None
        return None
    ep = ST["ep"] if ST["ep"] and ST["ep"]["hero"] == hero else {"hero": hero, "items": [None] * 6, "conf": [0.0] * 6, "neutral": None}
    for i, (k, c) in enumerate(zip(r["items"], r["conf"])):
        if c: ep["items"][i], ep["conf"][i] = k, c  # unread (cooldown) slots keep the episode's earlier reading
    ep["neutral"] = r["neutral"] or ep["neutral"]
    ST["ep"] = ep
    rec = {"hero": hero, "clock": round(clock, 1), "items": list(ep["items"]), "backpack": [None] * 3, "neutral": ep["neutral"],
           "conf": round(conf, 3), "slot_conf": list(ep["conf"])}
    ST["last"] = rec
    e = (W.get("heroes") or {}).get(hero) or {}
    if e.get("team") and e.get("team") == W["me"].get("team"): return None  # ally: shown as "last", not recorded
    ST["seen"][hero] = {"clock": rec["clock"], "items": rec["items"], "backpack": rec["backpack"], "neutral": rec["neutral"],
                        "net_items_value": value(rec["items"] + [rec["neutral"]])}
    key = (hero, tuple(rec["items"]), rec["neutral"])
    if key == ST["logged"]: return None
    ST["logged"] = key
    return rec


def state(W):
    """W["inspect"] from ST + the current world (stale/ask need clock, visibility, deaths)."""
    g, me = W.get("game") or {}, W.get("me") or {}
    clock, turbo = g.get("clock") or 0, g.get("mode") == "turbo"
    seen = {h: {**s, "likely_next": likely_next(h, s["items"], clock, turbo)} for h, s in ST["seen"].items()}
    lim = 150 if turbo else 300
    enemies = {h: e for h, e in (W.get("heroes") or {}).items() if e.get("team") and e.get("team") != me.get("team")}
    stale = sorted(h for h, e in enemies.items() if not e.get("dead") and clock >= lim and clock - (ST["seen"].get(h) or {}).get("clock", -1e9) > lim)
    carry = lambda h: "Carry" in ((data.S["heroes"].get(h) or {}).get("roles") or [])
    vis = [h for h in stale if enemies[h].get("visible")]  # only worth asking when you can click them now
    ask = max(vis, key=lambda h: (carry(h), clock - (ST["seen"].get(h) or {}).get("clock", -1e9)), default=None)
    return {"last": ST["last"], "seen": seen, "stale": stale, "ask": ask}


# ---------- worker (app.py hands frames over; never blocks the capture thread) ----------

_W = {"frame": None, "ev": threading.Event(), "thread": None, "new": False, "period": 0.7}


def offer(frame):
    """Capture-thread hook: hand over the newest frame (no copy, no work). True once after a new enemy record."""
    _W["frame"] = frame; _W["ev"].set()
    if _W["thread"] is None:
        _W["thread"] = threading.Thread(target=_run, name="inspect", daemon=True); _W["thread"].start()
    new, _W["new"] = _W["new"], False
    return new


def _run():
    import rec, world
    while True:
        _W["ev"].wait(); _W["ev"].clear()
        t0 = time.monotonic()
        try:
            snap = world.snapshot()
            r = step(_W["frame"], snap)
            with world.lock:
                if world.W: world.W["inspect"] = state(world.W)
            if r:
                _W["new"] = True
                rec.event("inspect", **r)
                log.info("inspect: %s %s neutral=%s", r["hero"], r["items"], r["neutral"])
        except Exception:
            log.exception("inspect failed; continuing")
        time.sleep(max(0.0, _W["period"] - (time.monotonic() - t0)))  # <= ~1.4 Hz


# ---------- selfcheck: recorded frames where GSI gives the truth ----------

def _pairs(root, n=60):
    """[(frame path, world row)] full_*.jpg frames with the nearest world.jsonl row (<= 1.5 s), in-game, own hero known."""
    import bisect
    out = []
    for d in sorted(Path(root).glob("*_*")):
        rows = []
        try:
            import gzip  # compacted sessions keep world.jsonl.gz
            wf = d / "world.jsonl"
            for line in (open(wf) if wf.exists() else gzip.open(d / "world.jsonl.gz", "rt")):
                try: rows.append(json.loads(line))
                except ValueError: pass
        except OSError: continue
        ws = [r.get("w", 0) for r in rows]
        lu = (json.loads((d / "meta.json").read_text()).get("lineup") or {}).get("heroes") if (d / "meta.json").exists() else None
        for f in sorted((d / "frames").glob("full_*.jpg")):
            t = int(f.stem[5:]) / 1000; i = bisect.bisect_left(ws, t)
            j = min((j for j in (i - 1, i) if 0 <= j < len(rows)), key=lambda j: abs(ws[j] - t), default=None)
            if j is None or abs(ws[j] - t) > 1.5: continue
            r = rows[j]
            if (r.get("game") or {}).get("state") == LIVE[1] and (r.get("me") or {}).get("hero") and (r["game"].get("clock") or 0) > 5:
                out.append((f, {**r, "lineup": {"heroes": lu or []}}))
    return out[::max(1, len(out) // n)][:n]


def selfcheck(root=None):
    """-> [(name, ok, detail)]: slot accuracy and selected-unit detection on recorded own-hero frames, plus the
    W["inspect"] bookkeeping on a synthetic world."""
    res = []
    if not data.S["items"]: data.load_static()
    P = _pairs(root or Path(__file__).parent / "recordings")
    if not P:
        res.append(("inventory: recorded frames", True, "skipped: no recordings with full_*.jpg frames"))
    else:
        asp = vision.render_aspect()
        n = ok = wrong = unread = me_ok = id_ok = id_n = 0
        for f, W in P:
            fr = cv2.imread(str(f)); g = vision.content_geom(fr, asp)
            r = read_items(fr, g) if g else None
            if r is None: continue
            h, _, _ = selected(fr, g, W, r); me_ok += h == W["me"]["hero"]
            if h not in (None, W["me"]["hero"]):
                continue  # the player had another unit selected: the HUD shows its items, not mine (2 Lich frames, 2026-11-13)
            for k, c, t in zip(r["items"], r["conf"], W["me"]["items"]):
                n += 1; ok += k == t; wrong += k is not None and k != t; unread += not c and t is not None
            if id_n < 12:  # pretend the HUD is someone else's: the ability matcher must still name the real hero
                id_n += 1; rk = identify(fr, g, r["x0"], sorted({x for x in W["lineup"]["heroes"] if x} | {W["me"]["hero"]}))
                id_ok += bool(rk) and rk[0][0] == W["me"]["hero"]
        fr_n = len(P)
        res += [("inventory: slot accuracy on own-hero frames", n and ok / n >= 0.85, f"{ok}/{n} = {100 * ok / max(n, 1):.1f}% exact, "
                 f"{wrong} wrong labels, {unread} unread (cooldown), {fr_n} frames"),
                ("inventory: wrong-label rate <= 2%", n and wrong / n <= 0.02, f"{wrong}/{n}"),
                ("inventory: selected unit = me on own-hero frames", me_ok >= 0.9 * fr_n, f"{me_ok}/{fr_n}"),
                ("inventory: ability icons name the HUD's hero", id_ok >= 0.9 * id_n, f"{id_ok}/{id_n}")]
    # bookkeeping on a synthetic world (no frames)
    saved = dict(ST)
    ST.update(mid="x", last=None, seen={"sven": {"clock": 100.0, "items": ["black_king_bar", None, None, None, None, None],
                                                  "backpack": [None] * 3, "neutral": None, "net_items_value": 4050}}, ep=None, logged=None)
    W = {"game": {"clock": 700.0, "mode": "normal", "matchid": "x"}, "me": {"hero": "lich", "team": "dire"},
         "heroes": {"sven": {"team": "radiant", "visible": True, "dead": False}, "jakiro": {"team": "radiant", "visible": True, "dead": False},
                    "lion": {"team": "radiant", "visible": False, "dead": False}, "huskar": {"team": "radiant", "visible": True, "dead": True},
                    "lich": {"team": "dire", "visible": True, "dead": False}}}
    s = state(W)
    ST.clear(); ST.update(saved)
    res.append(("inventory: stale/ask bookkeeping", s["stale"] == ["jakiro", "lion", "sven"] and s["ask"] == "sven"
                and "black_king_bar" not in s["seen"]["sven"]["likely_next"], f"stale={s['stale']} ask={s['ask']}"))
    res.append(("inventory: item value", value(["black_king_bar", "tango", None]) == 4050 + int((data.S["items"].get("tango") or {}).get("cost") or 0), ""))
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for name, ok, d in selfcheck(): print("PASS" if ok else "FAIL", name, d)
