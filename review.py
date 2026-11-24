"""Post-game learning review: one deep Claude call per match that grades the coach itself, not the player's play.
Input: the whole recorded match (every voice line, drop and hold with its reason, alerts, the game state every 3 s, deaths, kill
swings, decision-model answers, push-to-talk turns, and screenshots near the key moments when capture was on).
Output: review.json / review.md next to report.json, and every finding appended to data/profile/reviews.jsonl (kept forever, small),
which is what the next engine change reads ("apply the reviews"). Compaction waits for this (app.py), so the raw data is only
thinned out after it has been learned from.
`uv run review.py [DIR ...]` (default: the latest analyzed match). REVIEW_MODEL= (empty) turns it off."""
import base64, json, logging, os, sys, time
from pathlib import Path
import llm  # noqa: F401  (loads ../.env: ANTHROPIC_API_KEY)
import world
from tools import _jsonl

log = logging.getLogger("coach")
MODEL = os.environ.get("REVIEW_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("REVIEW_EFFORT", "high")
PRICE = (4.0, 20.0)  # claude-opus-5-5 USD per 1M input / output tokens
STEP, MAX_IMAGES = 3, 16  # game seconds between state rows; screenshots sent per match
ROOT = Path(__file__).parent
KINDS = ["late", "early", "wrong", "missed", "spam", "unclear", "good"]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "scores", "findings", "top_changes"], "properties": {
    "summary": {"type": "string"},
    "scores": {"type": "object", "additionalProperties": False, "required": ["timing", "relevance", "coverage", "intelligence"],
               "properties": {k: {"type": "integer"} for k in ("timing", "relevance", "coverage", "intelligence")}},
    "findings": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                 "required": ["clock", "kind", "severity", "said", "what_happened", "better", "change", "area"], "properties": {
                     "clock": {"type": "number"}, "kind": {"type": "string", "enum": KINDS},
                     "severity": {"type": "string", "enum": ["low", "med", "high"]},
                     "said": {"type": "string"}, "what_happened": {"type": "string"}, "better": {"type": "string"},
                     "change": {"type": "string"}, "area": {"type": "string"}}}},
    "top_changes": {"type": "array", "items": {"type": "string"}}}}
SYSTEM = """You review a live Dota 2 voice coach after a match, to improve the coach (its rules, timing and wording), not to coach the player.
You get: how the coach works (its own documentation), the match timeline (game clock in seconds), and screenshots near key moments if any.
Judge every spoken line: was it right, was it on time (a call after the moment has passed is worse than silence), was it actionable,
was it intelligent for this exact situation, was it repeated or noise. Also judge the silences: deaths, lost or won fights, objectives,
skill points or a missing TP where a call was needed and none came, or came too late. Dropped lines and their reasons are in the timeline.
The player's stated preferences: relevant, on-time calls over commentary; routine reminders only after a grace period and only when the
player is actually late; no retreat calls while the team is winning a fight; praise on time; NEVER a spoken death count or
"N left in your budget" line (on screen only; the player found it annoying), even where the documentation below still mentions it;
the coach must never press keys or send input.
For each finding give the clock, what the coach said (or "(silence)"), what actually happened, what it should have done, and a concrete
engine change (which rule or file, which threshold or condition). area = the rule id (R1, R12, ...) or file (voice.py, habits.py, ...).
Do not invent events that are not in the timeline. Include a few "good" findings so working behaviour is kept. Scores are 1-10."""


def _docs():
    t = (ROOT / "README.md").read_text()
    a, b = t.find("## Voice coach"), t.find("## Talk to the coach")
    return t[a:b] if a >= 0 and b > a else ""


def _rows(dirs, name):
    return [r for d in dirs for n in (name, name + ".gz") if (d / n).exists() for r in _jsonl(d / n)]


def timeline(dirs):
    """-> (text, [(w, clock)]) from events + world rows. One line per event, one state line every STEP game seconds."""
    lines, wc = [], []
    for r in _rows(dirs, "world.jsonl"):
        g, me = r.get("game") or {}, r.get("me") or {}
        c = g.get("clock")
        if c is None or g.get("state") not in ("DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "DOTA_GAMERULES_STATE_PRE_GAME"):
            continue
        wc.append((r.get("w") or 0, c))
        if lines and c - lines[-1][0] < STEP and lines[-1][1] == "S":
            continue
        pos = (me.get("x"), me.get("y"))
        near = lambda e, rr: None not in pos and e.get("x") is not None and ((e["x"] - pos[0]) ** 2 + (e["y"] - pos[1]) ** 2) ** .5 <= rr  # noqa: E731
        hs = r.get("heroes") or {}
        foes = [f"{s}@{((e['x'] - pos[0]) ** 2 + (e['y'] - pos[1]) ** 2) ** .5:.0f}" for s, e in hs.items()
                if not e.get("ally") and e.get("visible") and not e.get("dead") and near(e, 2500)]
        allies = sum(1 for s, e in hs.items() if e.get("ally") and s != me.get("hero") and e.get("visible") and near(e, 1500))
        zone = world.zone(*pos, me.get("team")) if None not in pos and me.get("team") else "?"
        al = ",".join(f"{a['id']}:{a['level']}" for a in r.get("alerts") or [] if a.get("level") != "info" or a.get("rule") == "R16")
        att = (r.get("attention") or {}).get("word")
        sk = r.get("skill") or {}
        lines.append((c, "S", f"{'alive' if me.get('alive') else 'DEAD'} hp{me.get('hp_pct')} L{me.get('level')} g{me.get('gold')} "
                              f"kda{me.get('k')}/{me.get('d')}/{me.get('a')} {zone} score{g.get('score')} tp{'+' if me.get('tp') else '-'}"
                              f"{' skillpts' + str(sk['points']) if sk.get('points') else ''} foes[{' '.join(foes)}] allies{allies}"
                              f"{' alerts[' + al + ']' if al else ''}{' card=' + att if att else ''}"))
    for e in _rows(dirs, "events.jsonl"):
        c, k = e.get("clock"), e.get("kind")
        if c is None:
            continue
        if k == "voice" and e.get("op") in ("hit", "miss", "drop", "obeyed", "live"):
            t = e.get("text") or e.get("wanted") or ""
            if e.get("op") == "drop" and not t:
                continue
            lines.append((c, "V", f"VOICE {e['op']} [{e.get('tier')}] \"{t}\" ({e.get('reason')}"
                                  f"{', waited ' + str(e.get('waited')) + 's' if e.get('waited') is not None else ''})"))
        elif k == "alert" and e.get("op") in ("new", "escalate") and e.get("rule") not in ("D", "T"):
            lines.append((c, "A", f"ALERT {e['op']} {e.get('id')} {e.get('level')}: {e.get('text')}"))
        elif k == "debrief":
            lines.append((c, "D", f"DEATH #{e.get('n')} cause={e.get('cause')} by={e.get('by')} called={e.get('called')}"))
        elif k == "jev_resp" and e.get("ok"):
            a = (e.get("decision") or {}).get("action") or {}
            lines.append((c, "J", f"DECISION {a.get('choice')} p={a.get('p')}"))
        elif k in ("talk", "feedback", "postgame"):
            lines.append((c, "T", f"{k.upper()} {json.dumps({x: v for x, v in e.items() if x not in ('w', 'clock', 'kind')}, default=str)[:300]}"))
    lines.sort(key=lambda x: x[0])
    return "\n".join(f"{world.mmss(c)} ({c:.0f}) {t}" for c, _, t in lines), sorted(wc)


def _frames(dirs, wc, rep):
    """Up to MAX_IMAGES full screenshots, nearest to deaths first, as (clock, base64 jpeg)."""
    import bisect
    fs = [(int(f.stem.split("_")[1]) / 1000, f) for d in dirs if (d / "frames").is_dir() for f in sorted((d / "frames").glob("full_*.jpg"))
          if f.stem.split("_")[1].isdigit()]
    if not fs or not wc:
        return []
    ws = [w for w, _ in wc]
    clock = lambda w: wc[min(bisect.bisect_left(ws, w), len(wc) - 1)][1]  # noqa: E731
    keys = [x["clock"] for x in rep.get("deaths") or [] if x.get("clock") is not None]
    fs = sorted(fs, key=lambda x: min((abs(clock(x[0]) - k) for k in keys), default=0))[:MAX_IMAGES]
    return [(clock(w), base64.b64encode(f.read_bytes()).decode()) for w, f in sorted(fs)]


def _md(r):
    out = [f"# Coach review {r['matchid']}", "", f"{r['summary']}", "", "Scores: " + ", ".join(f"{k} {v}/10" for k, v in r["scores"].items()),
           f"Model {r['model']}, ${r['usd']:.3f}, {r['at']}", "", "## Top changes", *[f"- {x}" for x in r["top_changes"]], "", "## Findings"]
    for f in sorted(r["findings"], key=lambda f: f["clock"]):
        out += [f"- **{world.mmss(f['clock'])} {f['kind']} ({f['severity']}, {f['area']})** said: {f['said']}",
                f"  - happened: {f['what_happened']}", f"  - better: {f['better']}", f"  - change: {f['change']}"]
    return "\n".join(out) + "\n"


def done(dirs):
    return not MODEL or any((Path(d) / "review.json").exists() for d in dirs)


def review(dirs, force=False):
    """Review one match (its session dirs). Writes review.json/.md into the last dir; appends findings to data/profile/reviews.jsonl."""
    import anthropic
    dirs = sorted(Path(d) for d in dirs)
    if not MODEL:
        return {"skip": "REVIEW_MODEL is empty"}
    if done(dirs) and not force:
        return {"skip": "already reviewed"}
    rep = json.loads((dirs[-1] / "report.json").read_text()) if (dirs[-1] / "report.json").exists() else {}
    text, wc = timeline(dirs)
    if len(text) < 2000:
        return {"skip": "too little recorded"}
    meta = {k: rep.get(k) for k in ("matchid", "result", "hero", "role", "mode", "duration", "kda", "goal")}
    content = [{"type": "text", "text": f"# How the coach works\n{_docs()}\n\n# Match\n{json.dumps(meta, default=str)}\n\n# Timeline\n"
                                        "S = my state, V = voice, A = alert, D = death, J = decision model, T = talk/feedback\n" + text}]
    for c, b64 in _frames(dirs, wc, rep):
        content += [{"type": "text", "text": f"Screenshot at {world.mmss(c)} ({c:.0f})"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}]
    client = anthropic.Anthropic()
    with client.beta.messages.stream(model=MODEL, max_tokens=64000, system=SYSTEM, messages=[{"role": "user", "content": content}],
                                     output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
                                     betas=["server-side-fallback-2026-07-01"], fallbacks="default") as s:
        m = s.get_final_message()
    if m.stop_reason in ("refusal", "max_tokens"):
        return {"error": f"stop_reason {m.stop_reason}"}
    out = json.loads(next(b.text for b in m.content if b.type == "text"))
    usd = (m.usage.input_tokens * PRICE[0] + m.usage.output_tokens * PRICE[1]) / 1e6
    r = {"matchid": rep.get("matchid") or dirs[-1].name.split("_")[-1], "model": m.model, "usd": round(usd, 4), "at": time.strftime("%Y-%m-%d %H:%M"),
         "images": sum(1 for x in content if x["type"] == "image"), **out}
    (dirs[-1] / "review.json").write_text(json.dumps(r, indent=1))
    (dirs[-1] / "review.md").write_text(_md(r))
    p = world.DATA / "profile" / "reviews.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        for x in out["findings"]:
            f.write(json.dumps({"matchid": r["matchid"], "at": r["at"], "applied": False, **x}) + "\n")
    log.info("review %s: %d findings, $%.3f -> %s", r["matchid"], len(out["findings"]), usd, dirs[-1] / "review.md")
    return {"path": str(dirs[-1] / "review.md"), "findings": len(out["findings"]), "usd": round(usd, 4)}


if __name__ == "__main__":
    import postgame
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    ds = [Path(a) for a in sys.argv[1:] if not a.startswith("-")]
    if not ds:
        ms = postgame.matches(ROOT / "recordings")
        ds = max((v for v in ms.values() if any((d / "report.json").exists() for d in v)), key=lambda v: v[-1].name)
    print(review(ds, force="--force" in sys.argv))
