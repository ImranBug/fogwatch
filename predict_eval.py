"""Replay accuracy harness for "where / when are the unseen enemy heroes" (dev tool: stdlib + numpy, never imported by the app).

  uv run predict_eval.py extract                    # slow, once: recordings/*/gsi.jsonl(.gz) -> world.py -> tracks/<session>.npz
  uv run predict_eval.py stats                      # sessions, train/test split, episode counts, how far heroes really get
  uv run predict_eval.py eval --predictor baseline  # metric tables, test + train (seconds)
  uv run predict_eval.py eval --predictor predict:Belief --split test --lead 5 --out run.json
  uv run predict_eval.py display --split test       # fog.py as the overlay shows it: marks, cues, who really arrives (acceptance numbers)
  uv run predict_eval.py occupancy                  # walkable grid from TRAIN hero positions -> tracks/occupancy.npy
  uv run predict_eval.py demo                       # self-check on a synthetic game

Tracks live in $PREDICT_TRACKS (default <COACH_DATA_DIR or ./data>/tracks). The split is by game: test = the N_TEST most recent
finished games. Anything learned from data (priors, the walkable mask) must use train only: `games("train")`.

PREDICTOR INTERFACE (streaming, so a predictor cannot see the future: it is handed one second at a time, in order)
  --predictor <name in PREDICTORS> | <module>:<callable> | <path/to/file.py>:<callable>
  callable() is called once per game and returns an object with
    reset(game)   optional. game = {"session", "team", "hero", "turbo", "grid": GRID}
    step(obs) -> {enemy_hero: pred}   called for EVERY recorded second. Heroes left out are scored as "no prediction".
  obs (class Obs): i, clock (game seconds), day, score (mine, theirs), me {"x","y","alive","hp","level"} (x/y None when unknown),
    heroes {name: {"team","ally","visible","x","y" (None unless visible),"dead","last_xy","mia_s","vanished_at","hint","resp"}}
       exactly world.W["heroes"] at that second (a hero is absent until the coach has seen it; "hint" = a summon moved last_xy now;
       "resp" = respawned and not seen since, last_xy is no sighting; None in tracks extracted before that column existed),
    units  int16 [n,7] = x, y, visionrange, name id, image id, team (2 radiant, 3 dire, 4 neutral, 5 other), yaw (degrees): every
       object on the GSI minimap that second (hero rows included: yaw = where a hero faced); obs.names / obs.images decode the ids,
    obs.vision() -> rows of my team with visionrange > 0 (ally heroes, creeps, wards, buildings).
  pred = {"xy": (x, y)                        point estimate
          "r": radius                         region = circle around xy ...
          "grid": float [N,N] >= 0            ... or a density over GRID cells, grid[iy, ix] (any scale). Its region for the
                                              hit / area columns = the best cells holding 90 % of the weight
          "p": 0..1}                          P(this hero is seen within 1200 u of me in the next 10 s)
  GRID: N x N cells of CELL u, cell (iy, ix) covers x in [LO + ix*CELL, +CELL), same for y; cell_of(x, y) -> (iy, ix).

WHAT IS SCORED
  Fog episode: an enemy seen >= 2 s, then unseen >= 3 s, then seen again alive, not dead in between ("respawn" episodes, death ->
  first sighting, are reported apart). The prediction made `--lead` seconds before the reappearance (default 1 = the last unseen
  second) is scored against the reappearance spot: point error; region hit and region area (% of walkable cells); hit within a fixed
  area budget (the best 5/10/20 % of walkable cells: by density, or the concentric inner part of a circle). A region within
  --tol u (default 400: one second of walking, the staleness of the last unseen snapshot) of the sighting counts as a hit.
  Markers in plain sight: share of point estimates (unseen >= 4 s) deep inside my team's vision circles. Needs no truth: it is the
  "that mark is obviously wrong" rate.
  Arrival: an enemy unseen >= 4 s that appears within 1500 u of me while I am alive ("cold" = no enemy was in sight within 2000 u
  of me in the 5 s before: a gank, not one more hero joining a fight). A warning is p >= threshold. Lead = seconds between the
  first warning in the 15 s before the arrival and the arrival; a warning second is false when that hero does not arrive within
  15 s. Calibration compares p with "seen within 1200 u of me in the next 10 s".
  ponytail: truth exists only where my team has vision, so every number is conditional on the enemy being seen again: a predictor
  that puts weight where my team looks (lanes, the edge of my vision) is flattered, one that removes visible ground is punished.
  --lead 5 scores the forecast "where does it show up 5 s from now" (same bias); "markers in plain sight" is the counterweight.
"""
import argparse, gzip, importlib, importlib.util, json, math, os, sys, tempfile, time
from pathlib import Path
import numpy as np

ROOT = Path(__file__).parent
REC = ROOT / "recordings"
TRACKS = Path(os.environ.get("PREDICT_TRACKS") or Path(os.environ.get("COACH_DATA_DIR") or ROOT / "data") / "tracks")
N_TEST = 7  # most recent finished games
N, CELL, LO = 64, 275, -8800  # 64 x 64 cells of 275 u cover -8800..8800
TOL = 400  # u a hero walks in the 0-1 s between the last unseen snapshot and the sighting: a region this close to the truth is a hit
DEEP = 600  # u inside a vision circle: an enemy standing that deep is usually seen (measured hidden share: at least 6 %)
GRID = {"n": N, "cell": CELL, "lo": LO}
NA = -32768  # int16 "no position"
FOUNTAIN = {"radiant": (-7100, -6600), "dire": (7000, 6400)}  # world.FOUNTAIN (not imported: eval must not need world.py's deps)
TEAM_NUM = {"radiant": 2, "dire": 3}
DT_BUCKETS = ((3, 5), (5, 10), (10, 20), (20, 40), (40, 1e9))
PHASES = ((-1e9, 480, "0-8 min"), (480, 1080, "8-18 min"), (1080, 1e9, "18+ min"))
CX = LO + (np.arange(N) + 0.5) * CELL  # cell centres


def cell_of(x, y):
    return (min(N - 1, max(0, int((y - LO) // CELL))), min(N - 1, max(0, int((x - LO) // CELL))))


# ---------------------------------------------------------------- extract

def _gsi_path(d):
    return next((p for p in (d / "gsi.jsonl", d / "gsi.jsonl.gz") if p.exists()), None)


def sessions():
    """Finished recorded games, oldest first: a matchid, a GSI file nobody wrote to in the last 10 min, >= 10 game minutes."""
    out = []
    for d in sorted(REC.iterdir()) if REC.exists() else []:
        try:
            m = json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        p = _gsi_path(d)
        if p and m.get("matchid") and (m.get("clock") or 0) >= 600 and time.time() - p.stat().st_mtime > 600:
            out.append(d)
    return out


def extract_one(d, out):
    import world  # the real pipeline; COACH_DATA_DIR was pointed away from the live data dir in main()
    meta = json.loads((d / "meta.json").read_text())
    start, mid, p = meta["start"], str(meta["matchid"]), _gsi_path(d)
    world.set_data_dir(world.DATA)  # clears W: default settings, no state from the previous game
    names, images, ix = {}, {}, lambda tab, k: tab.setdefault(k or "", len(tab))
    rows, units, off, order, events, ev_seen, last_c, hinted = [], [], [0], {}, [], set(), None, {}
    with (gzip.open(p, "rt") if p.suffix == ".gz" else open(p)) as f:
        for line in f:
            try:
                r = json.loads(line)
                body, T = r["body"], start + r["t"]
            except (ValueError, KeyError, TypeError):
                continue  # torn last line of a killed session
            if not world.merge_gsi(body, now=T):
                continue
            world.tick(T)
            W = world.W
            c = W["_"]["clock"]
            if W["game"]["matchid"] != mid or c is None or (last_c is not None and int(c) <= last_c):
                continue  # one row per game second: the coach's knowledge at the first payload of that second
            last_c = int(c)
            me, g = W["me"], W["game"]
            who = (me["team"], me["hero"], g["mode"] == "turbo")  # kept per row: the post-game payloads reset W
            hs = {}
            for s, e in W["heroes"].items():
                if e["team"] is None:
                    continue
                order.setdefault(s, (e["team"] != me["team"], len(order)))
                lx, ly = e["last_xy"] or (None, None)
                hs[s] = (e["team"], e["visible"], e["x"] if e["visible"] else None, e["y"] if e["visible"] else None, e["dead"], lx, ly,
                         e["mia_s"], e["vanished_at"], e.get("hint_at") is not None and e.get("hint_at") != hinted.get(s),  # a sighting pops hint_at: not a new hint
                         e.get("resp_at") is not None)
                hinted[s] = e.get("hint_at")  # a hint in any payload of the last second
            sc = g["score"] if me["team"] == "radiant" else g["score"][::-1]
            rows.append((last_c, bool(g["daytime"]), me["x"], me["y"], bool(me["alive"]), me["hp_pct"], me["level"], sc[0], sc[1], hs))
            for o in (body.get("minimap") or {}).values():
                if not isinstance(o, dict):
                    continue
                x, y = world.num(o.get("xpos")), world.num(o.get("ypos"))
                if x is None or y is None or (x == 0 and y == 0) or max(abs(x), abs(y)) > 12000:
                    continue
                units.append((x, y, min(world.num(o.get("visionrange"), 0) or 0, 32000), ix(names, o.get("unitname")),
                              ix(images, o.get("image")), world.num(o.get("team"), 5) or 5, world.num(o.get("yaw"), 0) or 0))
            off.append(len(units))
            for ev in body.get("events") if isinstance(body.get("events"), list) else []:
                k = json.dumps(ev, sort_keys=True, default=str)
                if k not in ev_seen:
                    ev_seen.add(k)
                    events.append([last_c, ev])
    if len(rows) < 300:
        return None
    heroes = sorted(order, key=order.get)
    n, H = len(rows), len(heroes)
    A = {k: np.full((n, H), v, t) for k, v, t in (("know", False, bool), ("vis", False, bool), ("dead", False, bool), ("hint", False, bool), ("resp", False, bool),
                                                  ("x", NA, np.int16), ("y", NA, np.int16), ("lx", NA, np.int16), ("ly", NA, np.int16),
                                                  ("mia", np.nan, np.float32), ("van", np.nan, np.float32))}
    teams = {}
    f = lambda v, na: na if v is None else v
    for i, row in enumerate(rows):
        for s, (team, vis, x, y, dead, lx, ly, mia, van, hint, resp) in row[9].items():
            h = heroes.index(s)
            teams[s] = team
            A["know"][i, h], A["vis"][i, h], A["dead"][i, h], A["hint"][i, h], A["resp"][i, h] = True, vis, dead, hint, resp
            A["x"][i, h], A["y"][i, h], A["lx"][i, h], A["ly"][i, h] = f(x, NA), f(y, NA), f(lx, NA), f(ly, NA)
            A["mia"][i, h], A["van"][i, h] = f(mia, np.nan), f(van, np.nan)
    info = {"session": d.name, "matchid": mid, "team": who[0], "hero": who[1], "turbo": who[2], "meta_mode": meta.get("mode"), "heroes": heroes,
            "teams": [teams[s] for s in heroes], "names": list(names), "images": list(images), "events": events, "grid": GRID}
    np.savez_compressed(
        out, info=np.array(json.dumps(info)), clock=np.array([r[0] for r in rows], np.int32), day=np.array([r[1] for r in rows], bool),
        me=np.array([[f(r[2], NA), f(r[3], NA), r[4], f(r[5], -1), f(r[6], 0), r[7], r[8]] for r in rows], np.int16),
        units=np.array(units, np.int16).reshape(-1, 7), off=np.array(off, np.int64), **A)
    return info


def cmd_extract(a):
    TRACKS.mkdir(parents=True, exist_ok=True)
    for d in sessions():
        out = TRACKS / f"{d.name}.npz"
        if out.exists() and not a.force:
            continue
        t = time.time()
        info = extract_one(d, out)
        print(f"{d.name}: {'skipped (too short)' if not info else info['hero'] + ' ' + info['team']}  {time.time() - t:.0f} s", flush=True)


# ---------------------------------------------------------------- load

class Game:
    def __init__(self, path):
        z = np.load(path)
        self.info = json.loads(str(z["info"]))
        for k in z.files:
            if k != "info":
                setattr(self, k, z[k])
        self.session, self.heroes, self.n = self.info["session"], self.info["heroes"], len(self.clock)
        self.team, self.hero = self.info["team"], self.info["hero"]
        self.enemy = [t != self.team for t in self.info["teams"]]
        self.mex, self.mey, self.alive = self.me[:, 0].astype(float), self.me[:, 1].astype(float), self.me[:, 2].astype(bool)
        self.mex[self.me[:, 0] == NA] = np.nan


class Obs:
    __slots__ = ("i", "clock", "day", "score", "me", "heroes", "units", "names", "images", "team_num")

    def vision(self):
        u = self.units
        return u[(u[:, 5] == self.team_num) & (u[:, 2] > 0)]


def observations(G):
    """One Obs per recorded second, built only from that second's row."""
    nn = lambda v: None if v == NA else int(v)
    teams, names, images, tn = G.info["teams"], G.info["names"], G.info["images"], TEAM_NUM[G.team]
    resp = getattr(G, "resp", None)
    for i in range(G.n):
        o = Obs()
        m = G.me[i]
        o.i, o.clock, o.day, o.score, o.names, o.images, o.team_num = i, int(G.clock[i]), bool(G.day[i]), (int(m[5]), int(m[6])), names, images, tn
        o.me = {"x": nn(m[0]), "y": nn(m[1]), "alive": bool(m[2]), "hp": int(m[3]), "level": int(m[4])}
        o.units = G.units[G.off[i]:G.off[i + 1]]
        hs = o.heroes = {}
        for h in np.flatnonzero(G.know[i]):
            lx, mia, van = G.lx[i, h], G.mia[i, h], G.van[i, h]
            hs[G.heroes[h]] = {"team": teams[h], "ally": not G.enemy[h], "visible": bool(G.vis[i, h]), "x": nn(G.x[i, h]), "y": nn(G.y[i, h]),
                               "dead": bool(G.dead[i, h]), "last_xy": None if lx == NA else (int(lx), int(G.ly[i, h])),
                               "mia_s": None if mia != mia else float(mia), "vanished_at": None if van != van else float(van),
                               "hint": bool(G.hint[i, h]), "resp": None if resp is None else bool(resp[i, h])}
        yield o


def games(split="all"):
    """Game objects for 'train' | 'test' | 'all' (test = the N_TEST most recent tracks)."""
    ps = sorted(TRACKS.glob("[0-9]*.npz"))
    ps = {"all": ps, "train": ps[:-N_TEST], "test": ps[-N_TEST:]}[split]
    return [Game(p) for p in ps]


def walkable():
    """bool [N,N] from tracks/occupancy.npy (TRAIN hero positions, see cmd_occupancy); all True until that file exists."""
    p = TRACKS / "occupancy.npy"
    return np.load(p) > 0 if p.exists() else np.ones((N, N), bool)


def cmd_occupancy(a):
    occ = np.zeros((N, N), np.int32)
    for G in games("train"):
        v = G.vis & (G.x != NA)
        np.add.at(occ, (np.clip((G.y[v].astype(int) - LO) // CELL, 0, N - 1), np.clip((G.x[v].astype(int) - LO) // CELL, 0, N - 1)), 1)
    np.save(TRACKS / "occupancy.npy", occ)
    w = occ > 0
    tot = out = 0
    for G in games("test"):
        v = G.vis & (G.x != NA)
        iy, ix = np.clip((G.y[v].astype(int) - LO) // CELL, 0, N - 1), np.clip((G.x[v].astype(int) - LO) // CELL, 0, N - 1)
        tot, out = tot + v.sum(), out + (~w[iy, ix]).sum()
    print(f"occupancy.npy: {w.sum()} of {N * N} cells walkable ({w.mean():.0%}), {occ.sum()} hero-seconds from train games; "
          f"{out / max(tot, 1):.2%} of test hero-seconds fall outside it")


# ---------------------------------------------------------------- episodes

def episodes(G):
    """Fog and respawn episodes of every enemy, and arrivals on me. Uses the whole game (this is the truth side, not the predictor)."""
    eps, arr = [], []
    c = G.clock
    busy = np.zeros(G.n, bool)  # some enemy is in sight within 2000 u of me
    for h in np.flatnonzero(G.enemy):
        v = G.vis[:, h] & ~np.isnan(G.mex)
        busy[v] |= np.hypot(G.x[v, h] - G.mex[v], G.y[v, h] - G.mey[v]) <= 2000
    for h in np.flatnonzero(G.enemy):
        vis, dead = G.vis[:, h], G.dead[:, h]
        seen = np.flatnonzero(vis)
        for a, b in zip(seen[:-1], seen[1:]):
            if b - a < 2:
                continue  # a = last seen index, b = reappearance
            dt = int(c[b] - c[a])
            died = bool(dead[a + 1:b].any())
            run = a - 1
            while run >= 0 and vis[run]:
                run -= 1
            x1, y1, mx, my = float(G.x[b, h]), float(G.y[b, h]), G.mex[b], G.mey[b]
            d_me = math.hypot(x1 - mx, y1 - my) if G.alive[b] and mx == mx else None
            if dt >= 4 and d_me is not None and d_me <= 1500:
                arr.append({"h": int(h), "b": int(b), "dt": dt, "died": died, "cold": not busy[max(0, b - 5):b].any()})
            if dt < 3 or (not died and c[a] - c[run + 1] < 1):
                continue  # seen for a single second before vanishing: icon flicker
            mx0, my0 = G.mex[a], G.mey[a]
            eps.append({"h": int(h), "hero": G.heroes[h], "a": int(a), "b": int(b), "dt": dt, "clock": int(c[a]), "kind": "respawn" if died else "fog",
                        "x0": float(G.x[a, h]), "y0": float(G.y[a, h]), "x1": x1, "y1": y1, "on_me": d_me is not None and d_me <= 1500,
                        "me0": None if mx0 != mx0 else (mx0, my0), "me1": None if mx != mx else (mx, my)})
    return eps, arr


# ---------------------------------------------------------------- predictors

class Baseline:
    """Today's coach: the hero is at world's last_xy, somewhere within min(330 * mia_s, 6000) u of it. p = rules.danger's "missing"
    threat, a yes/no: unseen 4-15 s and last_xy within min(reach + 1000, 2500) u of me, unless I just walked out of my base past it."""

    def reset(self, game):
        self.team, self.hist = game["team"], []

    def p(self, e, me, reach):
        if not me["alive"] or me["x"] is None or not 4 <= e["mia_s"] <= 15:
            return 0.0
        home = lambda x, y: math.dist((x, y), FOUNTAIN[self.team])
        hd = home(me["x"], me["y"])
        came = self.hist[0] if len(self.hist) >= 10 else None
        if came and home(*came) < hd - 1500 and home(*e["last_xy"]) < hd:
            return 0.0
        return float(math.dist(e["last_xy"], (me["x"], me["y"])) <= min(reach + 1000, 2500))

    def step(self, obs):
        me, out = obs.me, {}
        if me["alive"] and me["x"] is not None:
            self.hist = (self.hist + [(me["x"], me["y"])])[-10:]
        for s, e in obs.heroes.items():
            if e["ally"] or e["visible"] or e["dead"] or e["last_xy"] is None or e["mia_s"] is None:
                continue
            reach = min(330 * e["mia_s"], 6000)
            out[s] = {"xy": e["last_xy"], "r": reach, "p": self.p(e, me, reach)}
        return out


class Reach(Baseline):
    """Baseline point and circle with a graded p for the warning curve: how easily a 330 u/s walk from last_xy gets within
    1200 u of me in 10 s. Not calibrated: it is the naive curve a real model has to beat."""

    def p(self, e, me, reach):
        if not me["alive"] or me["x"] is None:
            return 0.0
        need = math.dist(e["last_xy"], (me["x"], me["y"])) - 1200
        return float(min(1.0, max(0.0, 1 - need / (reach + 3300))))


def fog_heroes(obs, hinted, team):
    """obs.heroes as world.W["heroes"] looks to fog.build: hint_at / resp_at put back from the track's per-second flags. `hinted` is kept by the caller."""
    fount, heroes = FOUNTAIN["dire" if team == "radiant" else "radiant"], {}
    for s, e in obs.heroes.items():
        e = dict(e)
        if e["visible"] or e["dead"]:
            hinted.pop(s, None)
        elif e["hint"]:
            hinted[s] = obs.clock
        if not e["ally"] and e["last_xy"] is not None:
            # tracks older than the resp column: world parked a respawned hero on its fountain
            if e["resp"] if e["resp"] is not None else s not in hinted and math.dist(e["last_xy"], fount) < 5:
                e["resp_at"] = obs.clock - (e["mia_s"] or 0)
            elif s in hinted:
                e["hint_at"] = hinted[s]
        heroes[s] = e
    return heroes


class Fog:
    """fog.py as the coach runs it. point = last_xy (a ghost never moves) and region = today's reach circle, so the WHERE columns equal
    the baseline's by construction; p = p10."""

    def reset(self, game):
        self.st, self.hinted, self.team = {}, {}, game["team"]

    def step(self, obs):
        import fog
        heroes = fog_heroes(obs, self.hinted, self.team)
        sight = [(float(a), float(b), float(r)) for a, b, r in obs.vision()[:, :3]]
        return {o["hero"]: {"xy": heroes[o["hero"]]["last_xy"], "r": min(330 * heroes[o["hero"]]["mia_s"], 6000), "p": o["p10"]}
                for o in fog.build(heroes, obs.me, float(obs.clock), sight, self.st)}


PREDICTORS = {"baseline": Baseline, "reach": Reach, "fog": Fog}


def load_predictor(spec):
    if spec in PREDICTORS:
        return PREDICTORS[spec]
    mod, _, name = spec.rpartition(":")
    if mod.endswith(".py"):
        sp = importlib.util.spec_from_file_location(Path(mod).stem, mod)
        m = importlib.util.module_from_spec(sp)
        sys.modules[sp.name] = m
        sp.loader.exec_module(m)
    else:
        sys.path.insert(0, str(ROOT))
        m = importlib.import_module(mod)
    return getattr(m, name)


# ---------------------------------------------------------------- scoring

def score_region(pr, x1, y1, walk, tol=TOL):
    """(point error, hit, area as a fraction of walkable cells, rank: fraction of walkable area the predictor rates above the truth).
    The truth counts as the best-rated walkable cell within `tol` u of the sighting."""
    nw = max(int(walk.sum()), 1)
    err = math.hypot(pr["xy"][0] - x1, pr["xy"][1] - y1)
    if pr.get("grid") is not None:
        g = np.where(walk, np.asarray(pr["grid"], float), 0.0)
        d2 = (CX[None, :] - x1) ** 2 + (CX[:, None] - y1) ** 2
        near = walk & (d2 <= tol * tol)
        if not near.any():  # truth on a cell no train hero ever stood on: score the nearest walkable cell
            near = walk & (d2 <= np.where(walk, d2, np.inf).min())
        w = g[near].max()
        rank = ((g > w).sum() + ((g == w) & walk).sum() / 2) / nw
        v = np.sort(g[g > 0])[::-1]  # a density's "region" = its best cells holding 90 % of the weight
        k = int(np.searchsorted(np.cumsum(v), 0.9 * v.sum())) if len(v) else 0
        return err, bool(len(v) and w >= v[min(k, len(v) - 1)]), (k + 1) / nw, float(rank) if w > 0 else 1.0
    d = np.hypot(CX[None, :] - pr["xy"][0], CX[:, None] - pr["xy"][1])[walk]
    hit = err - tol <= pr["r"]
    return err, bool(hit), max(1, int((d <= pr["r"]).sum())) / nw, float((d < err - tol).sum() / nw) if hit else 1.0


def run(factory, G, lead=1, walk=None, tol=TOL):
    """Stream one game through a predictor. Returns (episode rows with scores, P[n,H] of p, arrivals, [markers checked, in plain sight])."""
    walk = walkable() if walk is None else walk
    eps, arr = episodes(G)
    c, want = G.clock, {}
    for e in eps:
        j = e["b"] - 1
        while j > e["a"] and c[e["b"]] - c[j] < lead:
            j -= 1
        if j > e["a"] and c[e["b"]] - c[j] <= lead + 5:  # a prediction exists at that lead (clock gaps: skip)
            want.setdefault((j, e["h"]), []).append(e)
    pred = factory()
    if hasattr(pred, "reset"):
        pred.reset({"session": G.session, "team": G.team, "hero": G.hero, "turbo": G.info["turbo"], "grid": GRID})
    P = np.full((G.n, len(G.heroes)), np.nan, np.float32)
    idx, sight = {s: h for h, s in enumerate(G.heroes)}, [0, 0]
    for obs in observations(G):
        out = pred.step(obs) or {}
        v = obs.vision().astype(float) if obs.i % 5 == 0 and out else ()
        for s, pr in out.items():
            h = idx[s]
            P[obs.i, h] = pr.get("p", 0.0)
            for e in want.get((obs.i, h), ()):
                e["err"], e["hit"], e["area"], e["rank"] = score_region(pr, e["x1"], e["y1"], walk, tol)
            if len(v) and (obs.heroes[s]["mia_s"] or 0) >= 4:  # every 5th second: is the marker somewhere I can plainly see?
                sight[0] += 1
                sight[1] += bool((v[:, 2] - np.hypot(v[:, 0] - pr["xy"][0], v[:, 1] - pr["xy"][1])).max() > DEEP)
    for e in eps:
        e["session"] = G.session
    return eps, P, arr, sight


def arrival_stats(G, P, arr, thresholds):
    """Per threshold: arrivals, warned, leads; warning seconds, true ones; alarms (runs of warning seconds per hero), true ones.
    Also the calibration pairs (p, seen within 1200 u of me in the next 10 s)."""
    c, n = G.clock, G.n
    near = np.zeros(P.shape, bool)  # seen within 1200 u of me at that second
    for h in np.flatnonzero(G.enemy):
        v = G.vis[:, h] & G.alive & ~np.isnan(G.mex)
        near[v, h] = np.hypot(G.x[v, h] - G.mex[v], G.y[v, h] - G.mey[v]) <= 1200
    nxt = np.searchsorted(c, c + 10, side="right")  # first index past clock + 10
    cum = np.vstack([np.zeros((1, P.shape[1]), int), np.cumsum(near, 0)])
    y10 = (cum[nxt] - cum[np.arange(n) + 1]) > 0
    ok = ~np.isnan(P) & G.alive[:, None]
    arr_at = np.zeros(P.shape, bool)
    for a in arr:
        arr_at[a["b"], a["h"]] = True
    cuma = np.vstack([np.zeros((1, P.shape[1]), int), np.cumsum(arr_at, 0)])
    n15 = np.searchsorted(c, c + 15, side="right")
    a15 = (cuma[n15] - cuma[np.arange(n) + 1]) > 0  # that hero arrives on me within 15 s
    out = {}
    for th in thresholds:
        warn = ok & (P >= th)
        leads = []
        for a in arr:
            k = a["b"] - 1
            first = None
            while k >= 0 and c[a["b"]] - c[k] <= 15:
                if warn[k, a["h"]]:
                    first = k
                k -= 1
            leads.append((0 if first is None else int(c[a["b"]] - c[first]), a["cold"]))
        start = warn & ~np.vstack([np.zeros((1, P.shape[1]), bool), warn[:-1]])  # ponytail: a 1 s dip splits one alarm in two
        true_alarm = 0
        for i, h in zip(*np.nonzero(start)):
            j = i
            while j + 1 < n and warn[j + 1, h]:
                j += 1
            true_alarm += bool(a15[i:j + 1, h].any())
        out[th] = {"leads": leads, "warn_s": int(warn.sum()), "true_s": int((warn & a15).sum()), "alarms": int(start.sum()), "true_alarms": true_alarm}
    return out, P[ok], y10[ok], float(c[-1] - c[0])


def q(v, *ps):
    return [float(np.percentile(v, p)) for p in ps] if len(v) else [float("nan")] * len(ps)


def region_table(eps, title):
    rows = [("all", [e for e in eps])] + [(f"{lo}-{hi if hi < 1e8 else ''} s", [e for e in eps if lo <= e["dt"] < hi]) for lo, hi in DT_BUCKETS] \
        + [(name, [e for e in eps if lo <= e["clock"] < hi]) for lo, hi, name in PHASES]
    print(f"\n{title}\n{'bucket':<10}{'n':>6}{'err p50':>9}{'p75':>7}{'p90':>7}{'hit':>6}{'area%':>7}{'hit@5%':>8}{'@10%':>6}{'@20%':>6}{'rank p50':>10}")
    res = {}
    for name, es in rows:
        es = [e for e in es if "err" in e]
        if not es:
            print(f"{name:<10}{0:>6}")
            continue
        err, rank = [e["err"] for e in es], np.array([e["rank"] for e in es])
        m = {"n": len(es), "err": q(err, 50, 75, 90), "hit": float(np.mean([e["hit"] for e in es])), "area": float(np.median([e["area"] for e in es])),
             "hit_at": [float((rank <= b).mean()) for b in (0.05, 0.10, 0.20)], "rank": float(np.median(rank))}
        res[name] = m
        print(f"{name:<10}{m['n']:>6}{m['err'][0]:>9.0f}{m['err'][1]:>7.0f}{m['err'][2]:>7.0f}{m['hit']:>6.0%}{m['area']:>7.1%}"
              f"{m['hit_at'][0]:>8.0%}{m['hit_at'][1]:>6.0%}{m['hit_at'][2]:>6.0%}{m['rank']:>10.1%}")
    return res


def boot(per_game, fn, n=300):
    """90 % interval of fn(pooled rows) when whole games are resampled (episodes of one game are not independent)."""
    rng, v = np.random.default_rng(0), []
    for _ in range(n):
        pick = [x for g in rng.integers(0, len(per_game), len(per_game)) for x in per_game[g]]
        if pick:
            v.append(fn(pick))
    return q(v, 5, 95)


def evaluate(spec, split, lead=1, tol=TOL, quiet=False):
    factory, walk = load_predictor(spec), walkable()
    ths = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9)
    eps, per_game, agg, ps, ys, secs, narr, ncold, sight = [], [], {}, [], [], 0.0, 0, 0, np.zeros(2)
    for G in games(split):
        e, P, arr, sg = run(factory, G, lead, walk, tol)
        sight += sg
        st, p, y, s = arrival_stats(G, P, arr, ths)
        eps += e
        per_game.append([x for x in e if x["kind"] == "fog" and "err" in x])
        ps.append(p), ys.append(y)
        secs, narr, ncold = secs + s, narr + len(arr), ncold + sum(x["cold"] for x in arr)
        for th, d in st.items():
            a = agg.setdefault(th, {"leads": [], "warn_s": 0, "true_s": 0, "alarms": 0, "true_alarms": 0})
            for k, v in d.items():
                a[k] += v
    fog, resp = [e for e in eps if e["kind"] == "fog"], [e for e in eps if e["kind"] == "respawn"]
    res = {"predictor": spec, "split": split, "lead": lead, "tol": tol, "games": len(per_game), "in_sight": float(sight[1] / max(sight[0], 1))}
    if quiet:
        return res, eps
    print(f"\n=== {spec} on {split}: {len(per_game)} games, {len(fog)} fog episodes, {len(resp)} respawn episodes, lead {lead} s, tolerance {tol} u ===")
    res["fog"] = region_table(fog, "FOG EPISODES (prediction vs reappearance spot; err in world units, area = share of walkable cells)")
    lo, hi = boot(per_game, lambda r: float(np.median([e["err"] for e in r])))
    lo2, hi2 = boot(per_game, lambda r: float(np.mean([e["rank"] <= 0.10 for e in r])))
    print(f"90 % interval over games: median err {lo:.0f}-{hi:.0f} u, hit@10% {lo2:.0%}-{hi2:.0%}")
    res["respawn"] = region_table(resp, "RESPAWN EPISODES (death -> first sighting; the coach's last_xy is the fountain)")
    print(f"\nMARKERS IN PLAIN SIGHT: {res['in_sight']:.1%} of {sight[0]:.0f} sampled hero-seconds (unseen >= 4 s) have the point estimate more than "
          f"{DEEP} u inside my team's vision, where an enemy would usually be seen")
    print(f"\nARRIVAL WARNING: {narr} arrivals on me (unseen >= 4 s, appears within 1500 u, me alive), {ncold} of them cold, in {secs / 60:.0f} game minutes")
    print(f"{'p >=':<6}{'recall':>8}{'cold':>6}{'lead>=3s':>10}{'cold':>6}{'lead p50':>10}{'warn s/10min':>14}{'precision(s)':>14}{'alarms/10min':>14}{'precision(alarm)':>18}")
    res["arrival"] = {}
    for th in ths:
        a, L, C = agg[th], np.array([x[0] for x in agg[th]["leads"]]), np.array([x[1] for x in agg[th]["leads"]], bool)
        m = {"recall": float((L > 0).mean()) if len(L) else None, "lead3": float((L >= 3).mean()) if len(L) else None,
             "recall_cold": float((L[C] > 0).mean()) if C.any() else None, "lead3_cold": float((L[C] >= 3).mean()) if C.any() else None,
             "lead_p50": float(np.median(L[L > 0])) if (L > 0).any() else None, "warn_s_per_10min": a["warn_s"] / secs * 600,
             "precision_s": a["true_s"] / a["warn_s"] if a["warn_s"] else None, "alarms_per_10min": a["alarms"] / secs * 600,
             "precision_alarm": a["true_alarms"] / a["alarms"] if a["alarms"] else None}
        res["arrival"][th] = m
        pc = lambda v: "-" if v is None else f"{v:.0%}"
        print(f"{th:<6}{pc(m['recall']):>8}{pc(m['recall_cold']):>6}{pc(m['lead3']):>10}{pc(m['lead3_cold']):>6}{'-' if m['lead_p50'] is None else format(m['lead_p50'], '.0f') + ' s':>10}"
              f"{m['warn_s_per_10min']:>14.0f}{pc(m['precision_s']):>14}{m['alarms_per_10min']:>14.1f}{pc(m['precision_alarm']):>18}")
    p, y = np.concatenate(ps), np.concatenate(ys)
    print(f"\nCALIBRATION of p vs 'seen within 1200 u of me in the next 10 s' ({len(p)} hero-seconds, base rate {y.mean():.1%}, "
          f"Brier {np.mean((p - y) ** 2):.4f}, always-base-rate Brier {y.mean() * (1 - y.mean()):.4f})")
    res["calibration"] = []
    edges = (0, 1e-9, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0001)
    print(f"{'p bin':<12}{'n':>8}{'mean p':>8}{'observed':>10}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi)
        if m.any():
            res["calibration"].append([lo, hi, int(m.sum()), float(p[m].mean()), float(y[m].mean())])
            print(f"{'0' if hi <= 1e-9 else f'{max(lo, 0):.2f}-{min(hi, 1):.2f}':<12}{m.sum():>8}{p[m].mean():>8.2f}{y[m].mean():>10.1%}")
    return res, eps


def cmd_eval(a):
    out = {}
    for split in (["test", "train"] if a.split == "both" else [a.split]):
        res, eps = evaluate(a.predictor, split, a.lead, a.tol)
        out[split] = {"summary": res, "episodes": eps}
    if a.out:
        Path(a.out).write_text(json.dumps(out, default=float))


# ---------------------------------------------------------------- display (what the player would see from fog.py)

def fog_replay(G):
    """fog.build second by second: conf, tier, p10, src code (0 seen, 1 hint, 2 respawn), age as [n,H]; nan / 0 / -1 where there is no entry."""
    import fog
    n, H = G.n, len(G.heroes)
    conf, tier, p10, src, age = np.full((n, H), np.nan), np.zeros((n, H), int), np.full((n, H), np.nan), np.full((n, H), -1), np.full((n, H), np.nan)
    idx, st, hinted = {s: h for h, s in enumerate(G.heroes)}, {}, {}
    for obs in observations(G):
        sight = [(float(a), float(b), float(r)) for a, b, r in obs.vision()[:, :3]]
        for o in fog.build(fog_heroes(obs, hinted, G.team), obs.me, float(obs.clock), sight, st):
            h = idx[o["hero"]]
            conf[obs.i, h], tier[obs.i, h], p10[obs.i, h], src[obs.i, h], age[obs.i, h] = o["conf"], o["tier"], o["p10"], ("seen", "hint", "respawn").index(o["src"]), o["age"]
    return conf, tier, p10, src, age


def _truth(G):
    """Per-second truth arrays [n,H] for display(): has (alive enemy out of sight with a last spot), its unseen time and distance to me,
    me alive with a position, y10 (that hero is seen within 1200 u of me in the next 10 s), arrivals on me, my deaths (row indexes)."""
    en, age = np.array(G.enemy), G.mia.astype(float)
    D = argparse.Namespace()
    D.has = G.know & ~G.vis & ~G.dead & (G.lx != NA) & ~np.isnan(age) & en[None, :]
    D.d = np.hypot(G.lx - G.mex[:, None], G.ly - G.mey[:, None]).astype(float)
    D.d[~D.has] = np.nan
    D.age, D.alive = np.where(D.has, age, np.nan), G.alive & ~np.isnan(G.mex)
    near = np.zeros(D.has.shape, bool)
    v = G.vis & en[None, :] & D.alive[:, None]
    near[v] = np.hypot(G.x - G.mex[:, None], G.y - G.mey[:, None])[v] <= 1200
    cum = np.vstack([np.zeros((1, near.shape[1]), int), np.cumsum(near, 0)])
    D.y10 = (cum[np.searchsorted(G.clock, G.clock + 10, side="right")] - cum[np.arange(G.n) + 1]) > 0
    D.arr, D.deaths = episodes(G)[1], np.flatnonzero(G.alive[:-1] & ~G.alive[1:]) + 1
    return D


def _runs(m):
    d = np.diff(np.r_[False, m, False].astype(int))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def display(split):
    """Acceptance numbers of the fog layer: fog.build replayed per second and scored as what the PLAYER sees, next to today's overlay
    rules ('?' circle = hud.mia 1-30 s after a vanish, '?' arrow = rules.danger's missing threat) on the same seconds."""
    import fog
    Gs = games(split)
    A = dict(hid=0, ghost=0, today=0, g_on=0, t_on=0, mins=0, alive=0, chips=[])
    sc = {"new": [], "today": [], "brier": []}
    R = {L: [0, 0] for L in (0, 1, 2)}; ons = {1: 0, 2: 0}; arrA = arrC = 0; rec = {1: [0, 0, 0], 2: [0, 0, 0]}; dw = {1: 0, 2: 0}; dn = 0
    T = {"arrow": [0, 0, 0], "hit": 0, "hitc": 0}
    PB, YB = [], []
    P = {1: [0, 0], 2: [0, 0]}; E = {1: [0, 0], 2: [0, 0]}; N = young = n_short = n_blink = gh = ghc = 0
    for G in Gs:
        D = _truth(G)
        conf, tier, p10, src, age = fog_replay(G)
        c = G.clock.astype(float); en = np.array(G.enemy)
        has = ~np.isnan(conf)
        ghost = has & (conf >= fog.GHOST) & (age >= 1)
        vs = c[:, None] - G.van
        today = D.has & (vs >= 1) & (vs <= 30)
        A["hid"] += has.sum(); A["ghost"] += ghost.sum(); A["today"] += today.sum(); A["mins"] += (c[-1] - c[0]) / 60
        full = ghost & (age >= 3)  # the fade-in ends at 3 s: marks the player really sees
        A["g_on"] += (full[1:] & ~full[:-1]).sum(); A["t_on"] += (today[1:] & ~today[:-1]).sum()
        chip = has & ((age >= 8) | (tier > 0)); A["chips"] += chip.sum(1).tolist()
        gh += (has & (conf >= fog.GHOST) & (age >= 2)).sum(); ghc += (has & ((conf >= fog.GHOST) | (tier > 0)) & (age >= 2)).sum()  # the overlay draws nothing before 2 s
        for h in np.flatnonzero(en):  # scored at the last unseen second: where does the hero show up
            for b in np.flatnonzero(G.vis[1:, h] & ~G.vis[:-1, h]) + 1:
                i = b - 1
                if c[b] - c[i] > 1.5 or G.lx[i, h] == NA:
                    continue
                d = math.hypot(G.x[b, h] - G.lx[i, h], G.y[b, h] - G.ly[i, h])
                if ghost[i, h]:
                    sc["new"].append(d <= 1000)
                if today[i, h]:
                    sc["today"].append(d <= 1000)
                if has[i, h] and src[i, h] != 2 and conf[i, h] > 0 and age[i, h] >= 2:
                    sc["brier"].append((conf[i, h], d <= 1000))
        a = D.alive; A["alive"] += a.sum()  # cues
        tr = tier.copy(); tr[~a] = 0
        lvl = tr.max(1); comes = (D.y10 & D.has).any(1)
        for L in (0, 1, 2):
            m = a & (lvl == L); R[L][0] += m.sum(); R[L][1] += comes[m].sum()
            if L:
                on = lvl >= L; ons[L] += (on[1:] & ~on[:-1]).sum()
        r = np.minimum(np.minimum(330 * D.age, 6000), 2500)
        thr = D.has & (D.age >= 4) & (D.age <= 15) & (D.d <= np.minimum(np.minimum(330 * D.age, 6000) + 1000, 2500))
        arrow = ((today & (np.maximum(0, D.d - r) / 350 <= 12)) | thr) & (D.d >= 500) & a[:, None]
        m = arrow.any(1) & a; T["arrow"][0] += m.sum(); T["arrow"][1] += comes[m].sum(); T["arrow"][2] += (m[1:] & ~m[:-1]).sum()
        for ar in D.arr:
            lo = np.searchsorted(G.clock, G.clock[ar["b"]] - 15); arrA += 1; arrC += ar["cold"]
            for L in (1, 2):
                w = tr[lo:ar["b"], ar["h"]] >= L
                if w.any():
                    rec[L][0] += 1; rec[L][1] += ar["cold"]; rec[L][2] += (G.clock[ar["b"]] - G.clock[lo + np.flatnonzero(w)[0]]) >= 3
            if arrow[lo:ar["b"], ar["h"]].any():
                T["hit"] += 1; T["hitc"] += ar["cold"]
        for i in D.deaths:
            arrs = [x for x in D.arr if 0 <= G.clock[i] - G.clock[x["b"]] <= 20]
            if arrs:
                dn += 1; b = min(x["b"] for x in arrs); lo = np.searchsorted(G.clock, G.clock[b] - 15)
                for L in (1, 2):
                    dw[L] += bool((lvl[lo:b] >= L).any())
        u = has & a[:, None]
        PB.append(p10[u]); YB.append(D.y10[u])
        young += ((tr > 0) & (age < fog.CUE_MIN_S)).sum()  # the cued hero itself, per cue
        for h in np.flatnonzero(en):
            for L in (1, 2):
                m = tr[:, h] == L; P[L][0] += m.sum(); P[L][1] += D.y10[m, h].sum()
            for s, e in _runs(tr[:, h] > 0):
                N += 1; L = tr[s:e, h].max(); j1 = np.searchsorted(c, c[s] + 15, side="right")
                E[L][0] += 1; E[L][1] += bool((G.vis[s:j1, h] & (np.hypot(G.x[s:j1, h] - G.mex[s:j1], G.y[s:j1, h] - G.mey[s:j1]) <= 1200)).any())
                short = c[e - 1] - c[s] + 1 <= 2
                n_short += short; n_blink += short and not (e < G.n and G.vis[e, h])
    PB, YB = np.concatenate(PB), np.concatenate(YB).astype(float)
    bz = np.array(sc["brier"])
    print(f"=== {split}: {len(Gs)} games, {A['mins']:.0f} game minutes, {A['hid']} hidden enemy hero-seconds, {arrA} arrivals on me ({arrC} cold)")
    print("WHERE (minimap)")
    print(f"  ghost on for {A['ghost'] / A['hid']:.0%} of hidden hero-seconds (today's '?' circle {A['today'] / A['hid']:.0%}); marks that reach full opacity per game minute {A['g_on'] / A['mins']:.1f} (today's '?' marks {A['t_on'] / A['mins']:.1f})")
    print(f"  when the hero shows up, it is within 1000 u of its mark: ghost {np.mean(sc['new']):.0%} (n {len(sc['new'])}), today's '?' {np.mean(sc['today']):.0%} (n {len(sc['today'])})")
    print(f"  conf calibration: Brier {np.mean((bz[:, 0] - bz[:, 1]) ** 2):.4f} (no-skill {np.mean((bz[:, 1].mean() - bz[:, 1]) ** 2):.4f}); " + "; ".join(
        f"{lo:.2f}-{hi:.2f}: {bz[(bz[:, 0] >= lo) & (bz[:, 0] < hi), 1].mean():.0%}" for lo, hi in ((0, .3), (.3, .45), (.45, .6), (.6, .8), (.8, 1.01)) if ((bz[:, 0] >= lo) & (bz[:, 0] < hi)).any()))
    ch = np.array(A["chips"])
    print(f"  strip chips: mean {ch.mean():.1f}, p90 {np.percentile(ch, 90):.0f}, empty {np.mean(ch == 0):.0%} of the time")
    print("WHEN (edge cue)")
    print(f"  cue up {1 - R[0][0] / A['alive']:.1%} of alive time (faint {R[1][0] / A['alive']:.1%}, close {R[2][0] / A['alive']:.1%}); today's '?' arrow {T['arrow'][0] / A['alive']:.1%}")
    print(f"  someone shows up within 1200 u in the next 10 s: no cue {R[0][1] / R[0][0]:.0%}, faint {R[1][1] / max(R[1][0], 1):.0%}, close {R[2][1] / max(R[2][0], 1):.0%}; today's arrow {T['arrow'][1] / T['arrow'][0]:.0%}")
    print(f"  onsets per 10 min: any cue {ons[1] / A['mins'] * 10:.1f}, close {ons[2] / A['mins'] * 10:.1f}; today {T['arrow'][2] / A['mins'] * 10:.1f}")
    print(f"  arrivals with that hero cued in the 15 s before: {rec[1][0] / arrA:.0%} (cold {rec[1][1] / max(arrC, 1):.0%}, >= 3 s ahead {rec[1][2] / arrA:.0%}), close tier {rec[2][0] / arrA:.0%} (cold {rec[2][1] / max(arrC, 1):.0%}); today {T['hit'] / arrA:.0%} (cold {T['hitc'] / max(arrC, 1):.0%})")
    print(f"  my deaths after a fog arrival with a cue up before it: {dw[1]}/{dn} (close {dw[2]}/{dn})")
    print(f"  p10 Brier {np.mean((PB - YB) ** 2):.4f} (always-base-rate {np.mean((YB.mean() - YB) ** 2):.4f})")
    print("SKEPTIC LINES")
    print(f"  the CUED hero itself shows up within 1200 u in 10 s: faint {P[1][1] / max(P[1][0], 1):.0%}, close {P[2][1] / max(P[2][0], 1):.0%} of cue seconds; "
          f"within 15 s of the cue's start: faint {E[1][1] / max(E[1][0], 1):.0%}, close {E[2][1] / max(E[2][0], 1):.0%} of cues")
    print(f"  cues per 10 min (per hero, what the player counts): {N / A['mins'] * 10:.1f}; on screen <= 2 s: {n_short / N:.0%} of cues, and gone without the hero being seen: {n_blink / N:.0%}")
    print(f"  cue seconds with the hero unseen < {fog.CUE_MIN_S:.0f} s: {young} (must be 0)")
    print(f"  minimap mark drawn (unseen >= 2 s and (conf >= {fog.GHOST} or cued)): {ghc / A['hid']:.0%} of hidden hero-seconds (conf alone {gh / A['hid']:.0%})")


def cmd_stats(a):
    ps = sorted(TRACKS.glob("[0-9]*.npz"))
    print(f"{len(sessions())} finished recorded games, {len(ps)} extracted in {TRACKS}\n")
    print(f"{'session':<30}{'split':<7}{'me':<10}{'team':<9}{'mode':<8}{'min':>5}{'fog ep':>8}{'respawn':>9}{'arrivals':>10}")
    allf = []
    for k, p in enumerate(ps):
        G = Game(p)
        eps, arr = episodes(G)
        fog = [e for e in eps if e["kind"] == "fog"]
        split = "test" if k >= len(ps) - N_TEST else "train"
        allf += [(split, e) for e in fog]
        print(f"{G.session:<30}{split:<7}{G.hero:<10}{G.team:<9}{'turbo' if G.info['turbo'] else 'normal':<8}{(G.clock[-1] - G.clock[0]) / 60:>5.0f}"
              f"{len(fog):>8}{len(eps) - len(fog):>9}{len(arr):>10}")
    for split in ("train", "test"):
        es = [e for s, e in allf if s == split]
        print(f"\n{split}: {len(es)} fog episodes. Displacement vanish -> reappearance (world units):")
        print(f"{'unseen':<9}{'n':>6}{'p25':>7}{'p50':>7}{'p75':>7}{'p90':>7}{'<600u':>7}{'u/s p50':>9}{'u/s p90':>9}{'on me':>7}")
        for lo, hi in DT_BUCKETS:
            b = [e for e in es if lo <= e["dt"] < hi]
            if b:
                d = np.array([math.hypot(e["x1"] - e["x0"], e["y1"] - e["y0"]) for e in b])
                sp = d / np.array([e["dt"] for e in b])
                print(f"{f'{lo}-{hi if hi < 1e8 else str()} s':<9}{len(b):>6}{q(d, 25)[0]:>7.0f}{q(d, 50)[0]:>7.0f}{q(d, 75)[0]:>7.0f}{q(d, 90)[0]:>7.0f}"
                      f"{(d < 600).mean():>7.0%}{q(sp, 50)[0]:>9.0f}{q(sp, 90)[0]:>9.0f}{np.mean([e['on_me'] for e in b]):>7.0%}")


def demo():
    """Self-check on a synthetic game: one enemy seen 5 s, gone 10 s, back 1000 u away and 500 u from me."""
    n, H = 40, 2
    G = Game.__new__(Game)
    G.info = {"session": "demo", "team": "radiant", "hero": "me", "turbo": False, "heroes": ["me", "foe"], "teams": ["radiant", "dire"], "names": [], "images": []}
    G.session, G.heroes, G.n, G.team, G.hero, G.enemy = "demo", ["me", "foe"], n, "radiant", "me", [False, True]
    G.clock, G.day = np.arange(n, dtype=np.int32), np.ones(n, bool)
    G.me = np.tile(np.array([500, 0, 1, 100, 1, 0, 0], np.int16), (n, 1))
    G.mex, G.mey, G.alive = G.me[:, 0].astype(float), G.me[:, 1].astype(float), G.me[:, 2].astype(bool)
    G.units, G.off = np.zeros((0, 7), np.int16), np.zeros(n + 1, np.int64)
    vis = np.zeros((n, H), bool)
    vis[:, 0], vis[5:10, 1], vis[20:, 1] = True, True, True
    G.vis, G.know, G.dead, G.hint = vis, np.ones((n, H), bool), np.zeros((n, H), bool), np.zeros((n, H), bool)
    G.x, G.y = np.where(vis, 0, NA).astype(np.int16), np.where(vis, 0, NA).astype(np.int16)
    G.x[20:, 1] = 1000
    G.lx, G.ly = np.zeros((n, H), np.int16), np.zeros((n, H), np.int16)
    G.mia = np.full((n, H), np.nan, np.float32)
    G.mia[10:20, 1] = np.arange(1, 11)
    G.van = np.full((n, H), np.nan, np.float32)
    eps, arr = episodes(G)
    assert len(eps) == 1 and eps[0]["dt"] == 11 and eps[0]["kind"] == "fog" and eps[0]["on_me"] and len(arr) == 1, (eps, arr)
    seen = []

    class Spy(Baseline):
        def step(self, obs):
            seen.append(obs.i)
            return super().step(obs)
    eps, P, arr, _ = run(Spy, G, 1, np.ones((N, N), bool))
    e = eps[0]
    assert seen == list(range(n)) and e["err"] == 1000 and e["hit"] and 0 < e["rank"] < e["area"] < 0.2, e  # 3300 u circle holds a 1000 u move
    st, p, y, secs = arrival_stats(G, P, arr, (0.5,))
    assert st[0.5]["leads"] == [(7, True)] and st[0.5]["alarms"] == 1 and st[0.5]["true_alarms"] == 1, st  # warned from mia 4 s to the arrival
    assert y.sum() == 10 and np.isnan(P[:10]).all(), (y.sum(), P[:, 1])
    grid = np.zeros((N, N))
    grid[cell_of(1000, 0)] = 1
    assert score_region({"xy": (1000, 0), "grid": grid}, 1000, 0, np.ones((N, N), bool))[1:] == (True, 1 / N / N, 0.5 / N / N)
    assert score_region({"xy": (1000, 0), "grid": grid}, 3000, 0, np.ones((N, N), bool))[1:] == (False, 1 / N / N, 1.0)
    print("demo ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("extract").add_argument("--force", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("occupancy")
    sub.add_parser("demo")
    e = sub.add_parser("eval")
    e.add_argument("--predictor", default="baseline")
    e.add_argument("--split", default="both", choices=("test", "train", "both", "all"))
    e.add_argument("--lead", type=int, default=1)
    e.add_argument("--tol", type=float, default=TOL)
    e.add_argument("--out")
    sub.add_parser("display").add_argument("--split", default="both", choices=("test", "train", "both", "all"))
    a = ap.parse_args()
    if a.cmd == "extract":
        os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp())  # world.py must not read the live settings.json
    {"extract": cmd_extract, "stats": cmd_stats, "occupancy": cmd_occupancy, "eval": cmd_eval, "demo": lambda a: demo(),
     "display": lambda a: [display(s) for s in (["test", "train"] if a.split == "both" else [a.split])]}[a.cmd](a)


if __name__ == "__main__":
    main()
