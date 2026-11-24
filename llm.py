"""Occasional LLM phrasing of strategy. OpenAI-compatible /chat/completions over stdlib urllib, primary + fallback.
Off unless LLM_BASE_URL is set. Never blocks the tick loop. Secrets are never logged.
`uv run llm.py` does one tiny smoke call per configured provider."""
import json, logging, os, re, threading, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlparse
import rec, world

ROOT = Path(__file__).parent
log = logging.getLogger("coach")


def load_env(path, env=os.environ):
    """Tiny .env parser: KEY=VALUE, '#' comments and blanks ignored, quotes stripped, existing keys never overridden."""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            env.setdefault(k.removeprefix("export ").strip(), v)


load_env(ROOT / ".env")         # precedence: real env > ./.env > ../.env (shared by sibling projects)
load_env(ROOT.parent / ".env")

KEY_BY_HOST = [("openai.com", "OPENAI_API_KEY"), ("anthropic.com", "ANTHROPIC_API_KEY"), ("googleapis.com", "GEMINI_API_KEY")]
E = os.environ.get
TIMEOUT, MIN_INTERVAL, MAX_CALLS = float(E("LLM_TIMEOUT_S", 10)), float(E("LLM_MIN_INTERVAL_S", 20)), int(E("LLM_MAX_CALLS_PER_GAME", 60))
MAX_USD, PRICE_IN, PRICE_OUT = float(E("LLM_MAX_USD_PER_GAME", 0.10)), float(E("LLM_PRICE_IN", 0.10)), float(E("LLM_PRICE_OUT", 0.50))
DISABLED = False  # app.py --no-llm
PAUSED = False  # the dashboard "AI" switch: app.py sets it every tick from settings.ai (off unless true). Paused = no GPT, Jev, talk or live-line call at all


def target(prefix):
    base, model = E(f"{prefix}_BASE_URL"), E(f"{prefix}_MODEL")
    if not base or not model:
        return None
    host = urlparse(base).hostname or ""
    key = E(f"{prefix}_API_KEY") or next((E(v) for h, v in KEY_BY_HOST if host == h or host.endswith("." + h)), None)
    return {"base": base.rstrip("/"), "model": model, "host": host, "key": key}


TARGETS = [t for t in (target("LLM"), target("LLM_FALLBACK")) if t] if E("LLM_BASE_URL") else []  # no primary = LLM off

SECRETS = [v for k, v in os.environ.items() if v and len(v) >= 12 and re.search(r"KEY|TOKEN|SECRET", k)]


def reload():
    """Keys changed at runtime (dashboard /api/keys): rebuild the targets and the redaction list."""
    global TARGETS
    TARGETS = [t for t in (target("LLM"), target("LLM_FALLBACK")) if t] if E("LLM_BASE_URL") else []
    SECRETS[:] = [v for k, v in os.environ.items() if v and len(v) >= 12 and re.search(r"KEY|TOKEN|SECRET", k)]


SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["headline", "do_now", "next_items", "where", "risk", "why"],
          "properties": {"headline": {"type": "string"}, "do_now": {"type": "array", "items": {"type": "string"}},
                         "next_items": {"type": "array", "items": {"type": "string"}},
                         "where": {"type": "string", "enum": ["safe_lane", "mid", "off_lane", "own_jungle", "enemy_jungle",
                                                              "river", "roshan", "base", "with_team"]},
                         "risk": {"type": "string", "enum": ["low", "med", "high"]}, "why": {"type": "string"}}}

SYSTEM = """You are a concise Dota 2 coach watching one player's game from a second monitor.
Advise only. Never tell the player to type in chat, ping, or automate inputs.
Output JSON matching the schema. Limits: headline <= 90 chars, do_now <= 3 items of <= 60 chars each,
next_items <= 3 item names, why <= 200 chars. Rule alerts in the snapshot are authoritative: never contradict them.
Prefer concrete actions (where to farm, what to buy next, which objective, who to kill or avoid).
Snapshot format: t=clock, mode: normal|turbo, day|night, me=hero L=level hp/mp %, g=gold, bb=buyback cost, kda, lh, items, pos=zone,
enemies hero(vis,zone | MIAns,last zone | dead | unseen), alerts, rosh state and active pit, score radiant-dire, weak enemies, and optionally jev: a fast decision model's
suggestion (action(probability) @zone, danger 0-4, fight / rosh_taken probabilities); use it as a hint, alerts still win.
Your text is shown to the player: never name that model and never quote a probability or percentage."""


def validate(v, s, path="$"):
    """Minimal JSON-schema check (object/array/string/enum/required/additionalProperties). Returns error str or None."""
    if "enum" in s and v not in s["enum"]:
        return f"{path}: {v!r} not in enum"
    t = s.get("type")
    if t == "object":
        if not isinstance(v, dict):
            return f"{path}: not an object"
        missing = [k for k in s.get("required", []) if k not in v]
        extra = set(v) - set(s.get("properties", {})) if s.get("additionalProperties") is False else set()
        if missing or extra:
            return f"{path}: missing {missing} extra {sorted(extra)}"
        return next((e for k, sub in s.get("properties", {}).items() if k in v and (e := validate(v[k], sub, f"{path}.{k}"))), None)
    if t == "array":
        if not isinstance(v, list):
            return f"{path}: not an array"
        return next((e for i, x in enumerate(v) if (e := validate(x, s.get("items", {}), f"{path}[{i}]"))), None)
    if t == "string" and not isinstance(v, str):
        return f"{path}: not a string"
    return None


def redact(s):
    for v in SECRETS:
        s = s.replace(v, "<redacted>")
    return re.sub(r"\b(sk-[\w-]{6,}|AIza[\w-]{10,})", "<redacted>", s)


def _post(t, system, user, max_tokens=1500, schema=None, timeout=None):  # schema/timeout: postgame debrief
    body = {"model": t["model"], "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "coach", "strict": True, "schema": schema or SCHEMA}}}
    if "openai.com" in t["host"] or "googleapis" in t["host"]:
        body["reasoning_effort"] = E("LLM_REASONING_EFFORT", "low")  # never sent to Anthropic; temperature never sent
    req = urllib.request.Request(t["base"] + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {t['key'] or 'none'}"})
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
        resp = json.load(r)
    u = resp.get("usage") or {}
    usd = ((u.get("prompt_tokens") or 0) * PRICE_IN + (u.get("completion_tokens") or 0) * PRICE_OUT) / 1e6
    return json.loads(resp["choices"][0]["message"]["content"]), usd, u


def ask(system, user):
    """-> (advice dict | None, usd_est, model | None). Primary, then fallback, then None."""
    usd = 0.0
    for t in TARGETS:
        try:
            out, c, _ = _post(t, system, user)
            usd += c
            err = validate(out, SCHEMA)
            if not err:
                return out, usd, t["model"]
            log.warning("llm %s: bad output: %s", t["model"], err)
        except urllib.error.HTTPError as e:
            log.warning("llm %s: HTTP %s %s", t["model"], e.code, redact(e.read()[:300].decode(errors="replace")))
        except Exception as e:  # ponytail: blind failover; add per-error handling if it flaps
            log.warning("llm %s: %s", t["model"], redact(repr(e))[:300])
    return None, usd, None


def _system(W):
    """Per-game context, rebuilt every call (~400 tokens: below provider caching minimums, so nothing to keep stable)."""
    import data
    me, raw = W["me"], W["_"].get("advice_raw") or {}
    build = data.valve_build(me["hero"]) if me["hero"] else {}
    weak = "\n".join(f"- {r['hero']}: weak {r['weak_score']}, {', '.join(r['reasons']) or '-'}; counter {', '.join(r['counter_items']) or '-'}"
                     for r in W["enemy_report"])
    return (f"{SYSTEM}\n\nMy hero: {me['hero']} (role {me['role'] or '?'}). Abilities: {', '.join(a['name'] for a in me['abilities']) or '-'}.\n"
            f"Valve build: {json.dumps(build) if build else '-'}\nRules item picks now: {', '.join(raw.get('next_items') or []) or '-'}\n"
            f"Enemies (weak/counter table):\n{weak or '- not yet known'}")


def maybe_call(W, now):
    """Check triggers and guards; start at most one background call. Call under world.lock."""
    L, g, me = W["llm"], W["game"], W["me"]
    L["enabled"] = bool(TARGETS) and not DISABLED and not PAUSED
    c = g["clock"]
    if not L["enabled"] or c is None or g["paused"]:
        return
    st = W["_"].setdefault("llm", {"pending": set(), "alive": None, "level": 0, "last_clock": None, "pre": False, "hi": set(), "hi_t": {}})
    pend = st["pending"]
    if not st["pre"] and (W["lineup"]["locked"] or c >= 0):
        st["pre"] = True
        pend.add("pre-game plan")
    if st["alive"] and me["alive"] is False:
        pend.add("I died")
    st["alive"] = me["alive"]
    lv = me["level"] or 0
    if any(st["level"] < k <= lv for k in (6, 12, 18)):
        pend.add(f"reached level {lv}")
    st["level"] = lv
    if me["alive"] and st["last_clock"] is not None and c - st["last_clock"] >= 150:
        pend.add("periodic")
    for a in W["alerts"]:  # once per alert episode (id, at), at most once per rule per 120 s
        if a["level"] == "high" and c - a["at"] >= 10 and (a["id"], a["at"]) not in st["hi"] and c - st["hi_t"].get(a["rule"], -1e9) >= 120:
            st["hi"].add((a["id"], a["at"]))
            st["hi_t"][a["rule"]] = c
            pend.add(f"persistent alert: {a['text']}")
    if not pend or L["in_flight"] or (L["last_at"] and now - L["last_at"] < MIN_INTERVAL) \
            or L["calls"] >= MAX_CALLS or L["usd_est"] >= MAX_USD:
        return
    why, mid = sorted(pend), g["matchid"]
    pend.clear()
    st["last_clock"] = c
    system = _system(W)
    user = f"trigger: {'; '.join(why)}\n{world.to_text()}"
    L.update(in_flight=True, calls=L["calls"] + 1, last_at=now)
    rec.event("llm_req", at_clock=c, trigger=why, user=user)

    def run():
        t0 = time.time()
        out, usd, model = ask(system, user)
        import rules
        with world.lock:
            if W["game"]["matchid"] != mid:
                return  # new game started; W["llm"] was reset
            W["llm"].update(in_flight=False, ok=out is not None, usd_est=round(W["llm"]["usd_est"] + usd, 5))
            late = (W["game"]["clock"] or 0) - c > 60
            if out and not late:
                W["advice"] = W["_"]["llm_advice"] = rules.finish(out, W, "llm") | {"model": model}
            rec.event("llm_resp", at_clock=c, ms=round((time.time() - t0) * 1000), usd=round(usd, 6), model=model, ok=out is not None,
                      late=late, advice=out)

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":  # smoke: one tiny call per provider; prints no secrets
    if not TARGETS:
        raise SystemExit("LLM_BASE_URL/LLM_MODEL not set")
    for t in TARGETS:
        t0 = time.time()
        try:
            out, usd, u = _post(t, "Reply with valid JSON for the schema. Keep every string under 5 words.",
                                "smoke test: t=0:00 me=juggernaut, nothing happening", max_tokens=800)
            print(f"{t['model']} @ {t['host']}: OK {time.time() - t0:.1f}s in={u.get('prompt_tokens')} out={u.get('completion_tokens')} "
                  f"~${usd:.5f} schema={'ok' if not validate(out, SCHEMA) else validate(out, SCHEMA)} headline={out.get('headline')!r}")
        except urllib.error.HTTPError as e:
            print(f"{t['model']} @ {t['host']}: HTTP {e.code} {redact(e.read()[:300].decode(errors='replace'))}")
        except Exception as e:
            print(f"{t['model']} @ {t['host']}: FAIL {redact(repr(e))[:300]}")
