"""Skill points: which ability to level next. Order = one cached LLM call per hero+role (data/cache/skills/), else a rule:
ultimate at 6/12/18, one point in each basic by level 4, then max basics in slot order. W["skill"] is set by update(W)."""
import json, logging, threading
from pathlib import Path
import llm

log = logging.getLogger("coach")
DIR = Path(__file__).parent / "data/cache/skills"
CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/abilities/"
SLOTS = "QWEDFR"
ORDER = {"type": "object", "additionalProperties": False, "required": ["order"],
         "properties": {"order": {"type": "array", "items": {"type": "string"}}}}
_busy, _orders, _off = set(), {}, {"v": 0, "match": None}


def _fetch(hero, role, names, ult):
    """Background: ask the LLM once for a 25-pick order using only these names, 'talent' or 'attribute'; cache it."""
    p = DIR / f"{hero}_{role}.json"
    try:
        sys_ = "You are a Dota 2 expert. Reply with the standard strong skill build for the current patch as JSON."
        user = (f"Hero {hero}, position {role}. Abilities (exact keys): {', '.join(names)}; ultimate: {ult}. "
                "Give 'order': exactly 25 entries for hero levels 1..25, each one of the ability keys above, 'talent' or 'attribute'. "
                "Basic abilities max at 4 levels, ultimate at 3 (levels 6, 12, 18); talents at 10, 15, 20, 25.")
        for t in llm.TARGETS:
            try:
                out, usd, _ = llm._post(t, sys_, user, 4000, ORDER, timeout=30)  # background thread. 800 tokens / 10 s: the models think first (650-2200 tokens, 10-15 s) and came back empty or late all game
                order = [o for o in out.get("order", []) if o in names or o in ("talent", "attribute")]
                if len(order) >= 16:
                    DIR.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps({"order": order, "model": t["model"], "usd": usd}))
                    _orders[(hero, role)] = order
                    return
            except Exception as e:
                log.warning("skills %s: %s", t["model"], llm.redact(repr(e))[:200])
    finally:
        _busy.discard((hero, role))


def _order(hero, role, names, ult):
    k = (hero, role)
    if k not in _orders:
        try:
            _orders[k] = json.loads((DIR / f"{hero}_{role}.json").read_text())["order"]
        except (OSError, ValueError, KeyError):
            if llm.TARGETS and k not in _busy:
                _busy.add(k)
                threading.Thread(target=_fetch, args=(hero, role, names, ult), daemon=True).start()
            return None
    return _orders[k]


def label(name, hero):
    """'lich_frost_nova' -> 'Frost Nova' (the hero key is the prefix; a one-word guess when it isn't)."""
    n = name.removeprefix(f"{hero}_") if hero and name.startswith(f"{hero}_") else name.split("_", 1)[-1]
    return n.replace("_", " ").title()


def _rule_pick(ab, level):
    """Fallback: ult whenever allowed, else first basic with < 1 point (by lvl 4), else lowest-slot basic not maxed."""
    ult = next((a for a in ab if a["ult"]), None)
    if ult and ult["level"] < min(3, level // 6):
        return ult["name"]
    basics = [a for a in ab if not a["ult"] and a["level"] < 4 and a["level"] < (level + 1) // 2]
    fresh = [a for a in basics if a["level"] == 0]
    return (fresh or basics or [{"name": "talent"}])[0]["name"]


TALENT_LV = (10, 15, 20, 25)
NO_POINT = (17, 19, 21, 22, 23, 24)  # Dota levels that give no ability point (the coach said "Level up" there, game 9086253341)


def earned(lvl):
    """Ability points a hero has earned by level `lvl`."""
    return lvl - sum(1 for n in NO_POINT if n <= lvl)


def update(W):
    me = W["me"]
    ab = [a for a in me.get("abilities") or [] if not a["name"].startswith("plus_") and "generic_hidden" not in a["name"]]
    lvl, hero = me.get("level") or 0, me.get("hero")
    if not ab or not hero or not lvl:
        W["skill"] = None
        return
    if _off["match"] != W["game"]["matchid"]:
        _off.update(v=0, match=W["game"]["matchid"])
    spent = sum(a["level"] for a in ab) + (me.get("talents") or 0) + (me.get("attributes_level") or 0)
    _off["v"] = max(_off["v"], spent - earned(lvl))  # innate abilities that GSI reports as levelled for free
    points = max(0, earned(lvl) - spent + _off["v"])
    if not points:
        W["skill"] = {"points": 0, "next": None, "then": [], "src": None}
        return
    names, ult = [a["name"] for a in ab], next((a["name"] for a in ab if a["ult"]), "")
    order, picks = _order(hero, me.get("role") or 0, names, ult), []
    lv = {a["name"]: a["level"] for a in ab}
    if order:
        done = earned(lvl) - points  # picks already made (incl. talents/attributes)
        for o in order[done:]:
            if o in lv:
                lv[o] += 1
                if (o == ult and lv[o] > min(3, (lvl + len(picks)) // 6)) or (o != ult and lv[o] > 4):
                    continue  # maxed, or an ultimate level the hero level doesn't allow yet (it was suggested at 11)
            picks.append(o)
            if len(picks) == 4:
                break
    picks = picks or [_rule_pick(ab, lvl)]
    if lvl in TALENT_LV and (me.get("talents") or 0) < sum(n <= lvl for n in TALENT_LV):
        picks = ["talent"] + [p for p in picks if p != "talent"]  # level 20 said "Take stats" (review 2026-11-12): the order drifts

    def info(n):
        if n in ("talent", "attribute"):
            return {"name": n, "label": n.title(), "slot": "talent", "img": None}
        i = names.index(n)
        return {"name": n, "label": label(n, hero), "slot": "R" if n == ult else SLOTS[min(i, 4)],
                "img": CDN + n + ".png"}
    W["skill"] = {"points": points, "next": info(picks[0]), "then": [info(n) for n in picks[1:4]], "src": "llm" if order else "rule"}


def selfcheck():
    ab = [{"name": "a_q", "level": 1, "ult": False}, {"name": "a_w", "level": 0, "ult": False},
          {"name": "a_e", "level": 0, "ult": False}, {"name": "a_r", "level": 0, "ult": True}]
    return [("skills: no ability point at levels 17/19/21-24", earned(16) == 16 and earned(17) == 16 and earned(24) == 18 and earned(25) == 19, ""),
            ("skills: rule picks a fresh basic at lvl 2", _rule_pick(ab, 2) == "a_w", _rule_pick(ab, 2)),
            ("skills: rule picks ult at 6", _rule_pick([dict(a, level=2 if not a["ult"] else 0) for a in ab], 6) == "a_r", "")]
