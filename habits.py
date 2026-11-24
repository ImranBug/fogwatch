"""This player's routine habits: how long a skill point / the ultimate point / a missing teleport scroll usually waits before the
player deals with it. Learned live (rules.evaluate calls track() every tick), kept in data/profile/habits.json (last 40 per kind).
The voice uses it to wait before a routine reminder (grace) and to remind early where this player is habitually late (ult_slow)."""
import json, logging, statistics
import world

log = logging.getLogger("coach")
KEEP, _ = 40, {"path": None, "h": None, "open": {}, "match": None}
# Dota total XP to reach level 6. ponytail: 7.3x table, re-check after an XP-table patch (the pre-reminder fires 250 XP early)
XP6 = 2440


def _path():
    return world.DATA / "profile" / "habits.json"


def load():
    if _["path"] != _path():  # --data-dir runs and the selfcheck point DATA elsewhere
        _["path"], _["h"] = _path(), {k: v for k, v in world._load(_path()).items() if isinstance(v, list)}
    return _["h"]


def record(kind, secs):
    h = load()
    h[kind] = (h.get(kind, []) + [round(secs, 1)])[-KEEP:]
    try:
        _["path"].parent.mkdir(parents=True, exist_ok=True)
        _["path"].write_text(json.dumps(h))
    except OSError as e:
        log.warning("habits: profile not writable (%r)", e)


def median(kind, default):
    v = load().get(kind) or []
    return statistics.median(v) if len(v) >= 2 else default


def grace(kind):
    """Seconds to wait before reminding. A player who handles it quickly gets room to do it alone (1.5x their median); a player who
    habitually forgets (median at the cap) gets the short wait, because for them the reminder is the useful part."""
    if kind == "skill":
        m = median("skill", 10)
        return 20 if m >= 30 else min(45, max(15, 1.5 * m))
    if kind == "ult":
        return 0 if ult_slow() else 8
    m = median("tp", 30)  # tp
    return 45 if m >= 80 else min(120, max(45, 1.5 * m))


def ult_slow():
    return median("ult", 0) >= 10


def track(W, c):
    """Open an episode when a skill point / no-TP appears, record its length when it is dealt with. Alive, in game only."""
    me, sk, o = W["me"], W.get("skill") or {}, _["open"]
    if _["match"] != W["game"]["matchid"]:
        _["match"], _["open"] = W["game"]["matchid"], {}
        o = _["open"]
    if me["alive"] is not True or c is None or c <= 0 or W["game"].get("state") != "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS":
        o.clear()  # dead time is not the player's delay
        return
    now = {"skill": bool(sk.get("points")) and (sk.get("next") or {}).get("slot") != "R",
           "ult": bool(sk.get("points")) and (sk.get("next") or {}).get("slot") == "R", "tp": me.get("tp") is False}
    for k, on in now.items():
        if on:
            o.setdefault(k, c)
        elif k in o:
            record(k, min(c - o.pop(k), 180))  # ponytail: 180 s cap, so one forgotten point doesn't own the median


def selfcheck():
    import tempfile
    from pathlib import Path
    saved = world.DATA
    world.set_data_dir(Path(tempfile.mkdtemp(prefix="habits-check-")))
    try:
        g0 = grace("skill")
        for s in (3, 4, 5):
            record("skill", s)
        quick = grace("skill")
        for s in (60, 70, 90, 120, 150):
            record("skill", s)
        record("ult", 25), record("ult", 30)
        return [("habits: default skill grace 15 s", g0 == 15, str(g0)),
                ("habits: quick player gets 15 s", quick == 15, str(quick)),
                ("habits: forgetful player gets 20 s", grace("skill") == 20, str(grace("skill"))),
                ("habits: slow ult -> remind at once", ult_slow() and grace("ult") == 0, str(load().get("ult")))]
    finally:
        world.set_data_dir(saved)
        _["path"] = None
