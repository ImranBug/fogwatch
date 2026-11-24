"""Item builds: an AI writes the purchase order once per situation (hero, role, mode, both lineups), cached on disk;
between refreshes the coach just walks the list, so no tokens are spent per tick.
Refresh when the enemy lineup changes and every 12 min of clock (6 in Turbo). Strategic model: LLM_STRATEGIC_MODEL
(default gpt-6.1-terra on the primary host), falling back to the normal LLM targets."""
import hashlib, json, logging, threading, time
from pathlib import Path
import data, llm

log = logging.getLogger("coach")
DIR = Path(__file__).parent / "data/cache/builds"
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["build", "situational", "why"],
          "properties": {"build": {"type": "array", "items": {"type": "string"}},
                         "situational": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                     "required": ["item", "when"],
                                                                     "properties": {"item": {"type": "string"}, "when": {"type": "string"}}}},
                         "why": {"type": "string"}}}
SYSTEM = ("You are a top Dota 2 coach. Patch 7.41. Give a concrete purchase order for THIS game as JSON. Use dotaconstants item "
          "keys (e.g. blink, black_king_bar, arcane_boots, magic_wand, glimmer_cape, force_staff, ward_observer). 'build': 6-9 "
          "items in buy order from now (finished items and key early components only, no consumables except for supports' "
          "wards/smoke), fitted to role and the enemy lineup. If I already hold parts of an item, that item comes first: never "
          "plan around what I am building. In Turbo everyone, supports too, earns about twice the gold and the game ends near "
          "25 minutes: skip cheap stopgap items after minute 5 and go straight to the big items that win fights. "
          "'situational': 2-4 {item, when}. 'why': one sentence.")
S = {"key": None, "build": None, "busy": False, "at": None, "usd": 0.0}


def _targets():
    t = llm.TARGETS[:1]
    m = llm.E("LLM_STRATEGIC_MODEL", "gpt-6.1-terra")
    return ([dict(t[0], model=m)] if t and m else []) + llm.TARGETS


def _valid(keys):
    items = data.S["items"]
    out = []
    for k in keys:
        k = str(k).lower().removeprefix("item_").strip().replace(" ", "_")
        if k not in items:  # accept display names too
            k = next((x for x, v in items.items() if str(v.get("dname", "")).lower().replace(" ", "_") == k), None)
        if k and k not in out:
            out.append(k)
    return out


def _fetch(key, user):
    try:
        for t in _targets():
            try:
                out, usd, _ = llm._post(t, SYSTEM, user, 4000, SCHEMA, timeout=30)  # background thread; the models think first: 900 tokens / 10 s came back empty or late all game
                S["usd"] += usd
                b = _valid(out.get("build", []))
                if len(b) >= 3:
                    sit = [{"item": k, "when": s.get("when", "")[:80]} for s in out.get("situational", []) for k in _valid([s.get("item")])]
                    v = {"build": b, "situational": sit, "why": str(out.get("why", ""))[:200], "model": t["model"], "usd": usd}
                    DIR.mkdir(parents=True, exist_ok=True)
                    (DIR / f"{key}.json").write_text(json.dumps(v))
                    S.update(build=v)
                    return
            except Exception as e:
                log.warning("builds %s: %s", t["model"], llm.redact(repr(e))[:200])
    finally:
        S["busy"] = False


def update(W, c, have, turbo):
    """Refresh the build when the situation key changes; returns current build dict or None (non-blocking)."""
    me = W["me"]
    if not me.get("hero") or c is None or not llm.TARGETS:
        return S["build"]
    allies = sorted(s for s, e in W["heroes"].items() if e.get("ally") and s != me["hero"])
    enemies = sorted(s for s, e in W["heroes"].items() if not e.get("ally"))
    period = 360 if turbo else 720
    slot = int(max(c, 0) // period)
    seen = (W.get("inspect") or {}).get("seen") or {}
    big = sorted({i for v in seen.values() for i in v.get("items") or []
                  if i and (data.S["items"].get(i) or {}).get("cost", 0) >= 2500})  # a new big enemy item -> rethink the build
    notes = "; ".join(n["text"] for n in W.get("player_notes") or [])  # what the player told the coach by voice (listen.py)
    raw = f"{me['hero']}|{me.get('role')}|{turbo}|{','.join(enemies)}|{slot}|{','.join(big)}|{notes}"
    key = hashlib.sha1(raw.encode()).hexdigest()[:16]
    if key == S["key"]:
        return S["build"]
    if S["busy"]:
        return S["build"]
    S["key"] = key
    p = DIR / f"{key}.json"
    if p.exists():
        S["build"] = json.loads(p.read_text())
        return S["build"]
    owned = sorted(k for k in set(me["items"] + me["backpack"] + me["stash"]) - {None})
    user = (f"Me: {me['hero']} position {me.get('role') or '?'}, level {me.get('level')}, {me.get('gold')} gold, "
            f"game clock {int(c // 60)}:{int(c % 60):02d}, mode {'Turbo' if turbo else 'normal'}.\n"
            f"Owned: {', '.join(owned) or 'nothing'}.\nAllies: {', '.join(allies) or '?'}.\nEnemies: {', '.join(enemies) or 'unknown yet'}."
            + "".join(f"\nSeen on {h}: {', '.join(i for i in v.get('items') or [] if i)}" for h, v in seen.items())
            + (f"\nPlayer says: {notes}" if notes else ""))
    S.update(busy=True, at=time.time())
    threading.Thread(target=_fetch, args=(key, user), daemon=True).start()
    return S["build"]


def selfcheck():
    data.S["items"] = data.S["items"] or {"blink": {"dname": "Blink Dagger"}, "black_king_bar": {"dname": "Black King Bar"}}
    v = _valid(["item_blink", "Black King Bar", "nonsense", "blink"])
    return [("builds: item key validation", v[:2] == ["blink", "black_king_bar"] and "nonsense" not in v, str(v))]
