"""Jev (TypeSafe AI) decisions: one ~0.3 s call per 10 s of game clock (sooner on new warn/high alerts), never blocking.
Off without JEV_API_KEY (read from .env via llm.py) or with app.py --no-jev. W["decision"] holds the latest fresh answer.
`uv run jev.py` makes one tiny smoke call."""
import json, logging, os, threading, time, urllib.error, urllib.request
import llm, rec, world  # llm loads ../.env and owns redact()

log = logging.getLogger("coach")
E = os.environ.get
URL = E("JEV_URL", "https://api.typesafe.ai/v1/systemone")
MODEL, TIMEOUT = E("JEV_MODEL", "jev-latest"), float(E("JEV_TIMEOUT_S", 5))
MAX_CALLS, MAX_USD = int(E("JEV_MAX_CALLS_PER_GAME", 600)), float(E("JEV_MAX_USD_PER_GAME", 0.10))
PRICE_IN = float(E("JEV_PRICE_IN", 0.042))  # USD per 1M input tokens; output is free
PERIOD, DEBOUNCE, STALE = 10, 3, 15  # game-clock seconds
DISABLED = False  # app.py --no-jev
IN_GAME = ("DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "DOTA_GAMERULES_STATE_PRE_GAME")

# action -> (criteria description, attention word, icon, attention level when p >= 0.6)
ACTIONS = {"farm_lane": ("farm the safest lane wave", "FARM", "farm", "info"),
           "farm_jungle": ("farm jungle camps", "JUNGLE", "farm", "info"),
           "gank": ("go kill an enemy hero with allies", "GO GANK", "target", "info"),
           "group": ("join teammates", "GROUP", "fight", "info"),
           "push": ("push a lane or tower", "PUSH", "push", "info"),
           "defend": ("defend our tower or base", "DEFEND", "push", "info"),
           "take_objective": ("take a tower, Tormentor or other objective", "OBJECTIVE", "push", "info"),
           "roshan": ("do or contest Roshan", "ROSH", "rosh", "info"),
           "stack_pull": ("stack or pull neutral camps", "STACK", "stack", "info"),
           "ward": ("place or remove wards", "WARD", "ward", "info"),
           "retreat": ("back off to safety now", "BACK", "back", "warn"),
           "heal_base": ("go to fountain to heal or buy", "HEAL", "back", "warn")}
DANGER = ["very unlikely", "unlikely", "possible", "likely", "very likely"]


def questions(W):
    """Question set for this state. next_item/target only when there is a real choice (>= 2 candidates)."""
    q = {"action": {"type": "choice", "instructions": "Best thing for me (the player) to do in the next 30 seconds.",
                    "criteria": {k: v[0] for k, v in ACTIONS.items()}},
         "where": {"type": "choice", "instructions": "Map zone I should be in for that.",
                   "criteria": {z: None for z in llm.SCHEMA["properties"]["where"]["enum"]}},
         "danger": {"type": "score", "instructions": "How likely I get ganked or die in the next 20 seconds.", "criteria": DANGER},
         "rosh_taken": {"type": "noul", "instructions": "Are the enemies taking Roshan right now?"},
         "fight": {"type": "noul", "instructions": "Should my team take a fight in the next 30 seconds?"}}
    import data
    items = {}
    for k in (W["_"].get("advice_raw") or {}).get("next_items") or []:
        it = data.S["items"].get(k) or {}
        items[k] = f"{it.get('dname') or k} ({it.get('cost') or '?'}g)"
    if len(items) >= 2:
        q["next_item"] = {"type": "choice", "instructions": "Item I should buy next.", "criteria": items}
    tg = {s: None for s, e in W["heroes"].items() if not e["ally"] and not e["dead"]
          and (e["visible"] or (e["mia_s"] is not None and e["mia_s"] < 10))}
    if len(tg) >= 2:
        q["target"] = {"type": "choice", "instructions": "Enemy hero my team should focus in the next fight.", "criteria": tg}
    return q


def state(W):
    al = "; ".join(f"{a['level']} {a['rule']}: {a['text']}" for a in W["alerts"]) or "-"
    return f"{world.to_text(decision=False)}\nmode: {W['game']['mode']} | role: {W['me']['role'] or '?'} | active alerts: {al}"


def parse(resp, asked, at_clock, ms):
    """Jev response -> W["decision"] shape. Unknown/missing answers become neutral values, never raise."""
    A = resp.get("answers") if isinstance(resp, dict) else None
    A = A if isinstance(A, dict) else {}

    def ch(name):
        a = A.get(name) if isinstance(A.get(name), dict) else {}
        k, probs = a.get("choice"), a.get("probabilities") if isinstance(a.get("probabilities"), dict) else {}
        if name not in asked or k not in asked[name]["criteria"]:
            return None
        p = world.num(probs.get(k), world.num(a.get("confidence"), 0))  # probability of the pick; "confidence" is lower/other
        return {"choice": k, "p": round(p, 3), "probs": {o: round(world.num(v, 0), 3) for o, v in probs.items()}}

    def nl(name):
        a = A.get(name) if isinstance(A.get(name), dict) else {}
        return {"p": round(world.num(a.get("noul"), 0), 3)}

    sc = A.get("danger") if isinstance(A.get("danger"), dict) else {}
    nxt, tgt = ch("next_item"), ch("target")
    return {"at_clock": at_clock, "latency_ms": ms, "model": resp.get("model") if isinstance(resp, dict) else None,
            "action": ch("action") or {"choice": None, "p": 0, "probs": {}},
            "where": ch("where") or {"choice": None, "p": 0, "probs": {}},
            "danger": {"score": round(world.num(sc.get("score"), 0), 2), "conf": round(world.num(sc.get("confidence"), 0), 3)},
            "rosh_taken": nl("rosh_taken"), "fight": nl("fight"),
            "next_item": nxt and {"choice": nxt["choice"], "p": nxt["p"]}, "target": tgt and {"choice": tgt["choice"], "p": tgt["p"]}}


def post(body, key):
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def maybe_decide(W, now):
    """Check cadence + guards; start at most one background call. Call under world.lock."""
    J, g, key = W["jev"], W["game"], E("JEV_API_KEY")
    J["enabled"] = bool(key) and not DISABLED and not llm.PAUSED
    c = g["clock"]
    if not J["enabled"] or c is None or g["paused"] or g["state"] not in IN_GAME or (W["src_age"]["gsi_s"] or 0) > 5:
        return
    st = W["_"].setdefault("jev", {"last": None, "seen": set(), "seq": 0, "urgent": False})
    for a in W["alerts"]:
        if a["level"] != "info" and (a["id"], a["level"]) not in st["seen"]:
            st["seen"].add((a["id"], a["level"]))
            st["urgent"] = True
    at = W["attention"]
    if at["seq"] != st["seq"]:
        st["seq"] = at["seq"]
        st["urgent"] |= at["level"] in ("warn", "high") and not str(at["id"]).startswith("jev:")  # not our own BACK
    last = st["last"]
    due = last is None or c - last >= PERIOD or (st["urgent"] and c - last >= DEBOUNCE)
    if not due or J["in_flight"] or J["calls"] >= MAX_CALLS or J["usd_est"] >= MAX_USD:
        return
    st.update(last=c, urgent=False)
    asked, mid = questions(W), g["matchid"]
    body = {"model": MODEL, "state": state(W), "questions": asked}
    J.update(in_flight=True, calls=J["calls"] + 1, last_at=now)
    rec.event("jev_req", at_clock=c, state=body["state"], questions=sorted(asked))

    def run():
        t0, out, err, usd = time.time(), None, None, 0.0
        try:
            resp = post(body, key)
            usd = ((resp.get("usage") or {}).get("input_tokens") or 0) * PRICE_IN / 1e6
            out = parse(resp, asked, c, round((time.time() - t0) * 1000))
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code} {llm.redact(e.read()[:200].decode(errors='replace'))}"
        except Exception as e:
            err = llm.redact(repr(e))[:200]
        ms = round((time.time() - t0) * 1000)
        if err:
            log.warning("jev: %s", err)
        with world.lock:
            if W["game"]["matchid"] != mid:
                return  # new game: W["jev"] was reset
            W["jev"].update(in_flight=False, ok=out is not None, err=err, latency_ms=ms, usd_est=round(W["jev"]["usd_est"] + usd, 6))
            late = (W["game"]["clock"] or 0) - c > STALE
            if out and not late:
                W["decision"] = out
            rec.event("jev_resp", at_clock=c, ms=ms, usd=round(usd, 7), ok=out is not None, late=late, err=err, decision=out)

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":  # smoke: one tiny call; prints no secrets
    key = E("JEV_API_KEY")
    if not key:
        raise SystemExit("JEV_API_KEY not set")
    q = questions(world.W)  # the real question set (no next_item/target without a game)
    t0 = time.time()
    try:
        r = post({"model": MODEL, "state": "t=12:00 mode: normal me=juggernaut hp20 alone in enemy jungle, 3 enemies missing", "questions": q}, key)
        print(f"OK {time.time() - t0:.2f}s model={r.get('model')} usage={r.get('usage')} answers={json.dumps(r.get('answers'))[:400]}")
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} {llm.redact(e.read()[:300].decode(errors='replace'))}")
