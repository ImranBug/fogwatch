"""Public game data, cached per Dota client build under data/cache/<build>/. Works offline with an empty cache."""
import functools, json, logging, re, threading, urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DOTA = Path.home() / "Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota"
OD = "https://api.opendota.com/api"
DC = "https://raw.githubusercontent.com/odota/dotaconstants/master/build"
log = logging.getLogger("coach")
S = {"heroes": {}, "items": {}, "item_ids": {}}  # heroes: short name -> OpenDota heroStats row
H = {}  # hero_id -> {"items", "matchups", "benchmarks"} (each may be None when offline)
_tried = set()


def build():
    """Client build from steam.inf; a new build invalidates the cache."""
    try:
        return re.search(r"ClientVersion=(\d+)", (DOTA / "steam.inf").read_text()).group(1)
    except (OSError, AttributeError):
        return "unknown"  # ponytail: no Dota install -> cache never invalidates; delete data/cache to refresh


CACHE = ROOT / "data/cache" / build()


def fetch(name, url):
    """Cached JSON, else one GET (10 s timeout) written to cache. None if offline and uncached."""
    p = CACHE / name
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "live-coach"}), timeout=10) as r:
            v = json.load(r)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(v))
        return v
    except Exception as e:
        log.warning("data: %s unavailable (%s)", name, e)
        return None


def load_static():
    """Run once at startup in a background thread: 3 requests, or 0 when cached."""
    hs = fetch("heroStats.json", OD + "/heroStats")
    S["heroes"] = {r["name"][14:]: r for r in hs if isinstance(r, dict) and str(r.get("name", "")).startswith("npc_dota_hero_")} \
        if isinstance(hs, list) else {}
    S["items"] = fetch("items.json", DC + "/items.json") or {}
    S["item_ids"] = fetch("item_ids.json", DC + "/item_ids.json") or {}
    log.info("data: %d heroes, %d items (cache %s)", len(S["heroes"]), len(S["items"]), CACHE.name)


def hero_data(hero_id):
    """Non-blocking: returns cached per-hero data or None, kicking off one background fetch (3 requests)."""
    if not isinstance(hero_id, int) or hero_id <= 0:
        return None
    if hero_id not in H and hero_id not in _tried:
        _tried.add(hero_id)  # ponytail: one attempt per run, restart to retry after going online
        threading.Thread(target=_load_hero, args=(hero_id,), daemon=True).start()
    return H.get(hero_id)


def _load_hero(i):
    H[i] = {"items": fetch(f"hero_{i}_items.json", f"{OD}/heroes/{i}/itemPopularity"),
            "matchups": fetch(f"hero_{i}_matchups.json", f"{OD}/heroes/{i}/matchups"),
            "benchmarks": fetch(f"hero_{i}_benchmarks.json", f"{OD}/benchmarks?hero_id={i}")}


ME = {}  # account_id -> list of {hero_id, games, win, last_played} (OpenDota /players/{id}/heroes; my own history only)


def my_heroes(account_id):
    """Non-blocking: the player's own hero history, cached per day under data/cache/<build>/; None until loaded."""
    a = str(account_id or "")
    if not a.isdigit() or a == "0":
        return None
    if a not in ME and ("me", a) not in _tried:
        _tried.add(("me", a))
        day = __import__("time").strftime("%Y%m%d")
        threading.Thread(target=lambda: ME.__setitem__(a, fetch(f"me_{a}_heroes_{day}.json", f"{OD}/players/{a}/heroes")),
                         daemon=True).start()
    return ME.get(a) if isinstance(ME.get(a), list) else None


@functools.cache
def valve_build(hero):
    """Valve's default build from the install: {"Starting_Items": [...], "Early_Game": [...], ...}; {} if missing."""
    try:
        txt = (DOTA / "itembuilds" / f"default_{hero}.txt").read_text(errors="replace")
    except OSError:
        return {}
    return {sec: re.findall(r'"item"\s+"item_(\w+)"', body)
            for sec, body in re.findall(r'"#DOTA_Item_Build_(\w+)"\s*\{([^}]*)\}', txt)}


def find_item(name):
    """'item_bfury' | 'bfury' | 'Battle Fury' -> (key, dotaconstants row) or (None, None)."""
    k = str(name).lower().strip().removeprefix("item_")
    items = S["items"]
    if k in items:
        return k, items[k]
    return next(((key, it) for key, it in items.items() if str(it.get("dname", "")).lower() == k), (None, None))
