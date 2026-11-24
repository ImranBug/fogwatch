"""Voice coach v2: silence is the default. The voice protects and cues; teaching happens while dead (debrief) and after the game.
Tiers (every line is an instruction, 1-9 words):
  U urgent  (prio 9): gank / danger / low HP / still in danger / don't go. An earcon is baked into the clip. Cuts in on anything that
                      is not urgent, at once; the only tier allowed in fight mode; the same cue at most once per 25 s.
                      "Back off. Heal up." (laning, R13) is urgent too: two lane opponents in range already count as fight mode,
                      so as a call it could never be spoken when it matters.
  C calls   (prio 5): too deep, defend / let it go, join, take a tower, Roshan, wisdom rune. Never in fight mode,
                      never within 8 s of an urgent line or while R1 is up, >= 20 s after the previous non-urgent line, dropped (never
                      said late) when it can't start within 4 s of becoming true.
  B brief   (prio 4): once before the horn: the game's goal, the lane job, the talk hint (queued by coach.py).
  D debrief (prio 4): while dead, from 1.5 s after the death: one cause line, one budget / buyback line (queued by coach.py).
  P praise  (prio 3): at most once per 3 min, in a quiet moment (queued by coach.py).
  K thinking prompt : one LLM sentence (<= 18 words) only when something just changed (respawn, fight won/lost, push window closed,
                      Roshan, a key enemy item seen, new phase) and the moment is quiet; dropped if it can't start within 7 s.
A call whose instruction is already being obeyed (the player is already leaving / already there) is not spoken.
Budget: per-game cap on non-urgent lines (30 normal, 12 low, 45 high), no exact text twice within 60 s. settings.quiet = nothing but
replies to push-to-talk until the next match. Every clip is LLM-based TTS (Gemini, else OpenAI gpt-4o-mini-tts), cached forever in
data/voice/<provider>-<voice>/<slug>[.vN].wav (N = recorded variant, picked at random) and indexed in data/voice/library.json.
A line that isn't cached yet is NOT spoken (no robotic fallback): it is queued for background generation and plays the next time.
tick(W) runs in app.py's tick loop under world.lock and never blocks.
uv run voice.py --gen [--force] | --list | --stats | --audition | --say "Get out!" | --check"""
import difflib, argparse, array, base64, io, json, logging, math, os, random, re, subprocess, sys, threading, time, urllib.error, urllib.request, wave
from pathlib import Path
import habits, jev, llm, rec, world  # llm loads ../.env and owns redact()

log = logging.getLogger("coach")
llm.load_env(Path(__file__).parent / "voice.env")  # saved audio settings; real env vars still win
E = os.environ.get
PROVIDERS = {"gemini": {"model": E("VOICE_GEMINI_MODEL", "gemini-3.8-flash-tts"), "voice": E("VOICE_GEMINI_VOICE", "Kore"), "key": "GEMINI_API_KEY",
                        "audition": "Kore Charon Puck Fenrir Aoede Orus Alnilam Sadachbia".split()},
             "openai": {"model": E("VOICE_OPENAI_MODEL", "gpt-4o-mini-tts"), "voice": E("VOICE_OPENAI_VOICE", "cedar"), "key": "OPENAI_API_KEY",
                        "audition": "cedar marin ash onyx sage coral verse ballad".split()}}
ORDER = ["gemini", "openai"] if E("VOICE_PROVIDER", "auto") == "auto" else [E("VOICE_PROVIDER")]  # auto: Gemini first, then OpenAI
# One voice for every line, so clips played back to back sound like one person: calm and steady. Urgent lines only get firmer.
STYLE = E("VOICE_STYLE", "Calm, experienced esports coach on team voice chat. Steady, low-key, confident; even pitch and volume; natural "
          "conversational pace. Never excited, never anxious, never dramatic.")
URGENT_STYLE = E("VOICE_STYLE_URGENT", STYLE + " This is a warning: a little firmer and quicker, clear and decisive, but still calm. No shouting, "
                 "no panic.")
COMM_STYLE = E("VOICE_COMMENTARY_STYLE", STYLE)


def style_for(tier):
    return URGENT_STYLE if tier == "U" else STYLE
COMMENTARY, THINK_MAX = E("VOICE_COMMENTARY", "1") == "1", int(E("VOICE_THINK_MAX", 8))  # thinking prompts per game (x2 at verbosity high)
THINK_LLM = E("VOICE_THINK_LLM", "fallback")  # which llm.TARGETS entry writes them: the fallback model answers in 1-2 s, the primary in 4-5 s
COMM_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["say"], "properties": {"say": {"type": "string"}}}
COMM_SYSTEM = """You are a calm, experienced Dota 2 coach on team voice chat. Something just changed in the player's game (the "Moment").
Reply JSON {"say": "..."}: ONE spoken sentence of at most 18 words that tells the player what to do, or asks what they plan to do, in the
next minute because of that change, e.g. "Three of them are dead, so take the bot tower with your team." or
"Where do you want to be standing when they come back?"
It must fit the player's role, the phase and where they are right now. Never describe the current state, never list facts, no
percentages, no model names. If nothing useful follows from the moment, reply {"say": ""}.
Only use facts from the snapshot; never claim enemy cooldowns. Never tell them to type in chat or ping. Never say a number of seconds
or a timer (it is stale by the time it is heard). If an alert says DEFEND with level high, the job is defending with the team: never farm.
A hero respawns in the fountain at full health and mana: never tell them to heal after respawning or mention full mana.
After laning (phase mid or late) never assign a lane; think towers, Roshan, Aegis, grouping. Never mention runes unless the snapshot has a rune timer."""
VOLUME = float(E("VOICE_VOLUME", 1.0))  # afplay -v; multiplied by settings.volume (0-1)
GEN_GAP, DAILY, QUEUE_MAX = 60 / float(E("VOICE_GEN_PER_MIN", 12)), int(E("VOICE_GEN_DAILY", 300)), 250
ROOT = Path(__file__).parent / "data/voice"  # a cache like data/cache: shared by --data-dir test runs
DRY = E("VOICE_DRYRUN") == "1"  # log only, never play; app.py turns it on for --replay/--frames/--data-dir runs
GEN = True  # False: never queue generation (selfcheck)
IN_PROGRESS, PRE_GAME = "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "DOTA_GAMERULES_STATE_PRE_GAME"

# ---- the bank: every fixed line. Anything not listed here is not spoken. ---------------------------------------------
NUM = "Zero One Two Three Four Five Six Seven Eight Nine Ten".split()
U = {"get_out": "Get out!", "danger": "Get out. Now.", "lost": "Don't go. It's lost.", "gank": "Gank coming. Back to tower.",
     "gank_river": "Gank from the river. Back.", "gank_jungle": "Gank from the jungle. Back.", "gank_behind": "Behind you. Move.",
     "back_off": "Back off. Heal up.", "still": "Still in danger. Leave."}
C = {"too_deep": "You're too deep. Come back.", "defend_top": "Defend top.", "defend_mid": "Defend mid.",
     "defend_bot": "Defend bot.", "defend_base": "Defend the base!", "let_go": "Let it go. Stay with your team.",
     "base_next": "Base is next. Stay with your team.", "join": "Your team is fighting. Join them.",
     "objective": "They lost three. Take a tower.", "objective_4": "They lost four. Take a tower.", "objective_5": "All five are dead. Take a tower.", "tp": "No teleport scroll. Buy one.", "rosh": "They may be on Roshan. Check it.", "wisdom": "Wisdom rune soon. Go grab it.",
     "ult_soon": "Six is close. Skill your ult right away."}
B = {**{f"goal_{n}": f"One goal this game: {NUM[n].lower()} deaths or fewer." for n in range(3, 11)},
     "lane_support": "Lane: harass and protect your carry. Pull when you can.", "lane_core": "Farm first. Fight when your team is with you.",
     "lane_mid": "Mid: last hits first. Don't trade into their mid.", "lane_off": "Offlane: soak experience. Don't die for creeps.",
     "lane_any": "Lane: last hits first. Trade only when it's safe.",
     "talk_hint": "Hold your talk key if you want to ask me something."}
D = {"warned": "I called it early. Next time, leave on the call.", "alone": "You were alone. Next fight, stay near a teammate.",
     "first_in": "You went in before your team. Let a teammate start the fight.", "outnumbered": "They had numbers. Let that tower go next time.",
     "stayed_low": "You stayed too low. Go heal at half health.", "good_trade": "Good trade. That one was worth it.",
     "coach_miss": "My miss. I didn't see that one coming.", "streak": "Reset. Next fight is the one that counts.",
     "bb_up": "Buyback is up. Keep it.", "bb_no": "No buyback. Play safe.",
     # alternatives: "<cause>~n" -> the same lesson in other words (the same sentence every death was "very annoying")
     "warned~2": "That was the call to leave. Go on the first warning.", "warned~3": "I warned you there. Trust the call next time.",
     "alone~2": "Nobody was with you there. Move with a teammate.", "alone~3": "Too far from your team. Stay closer next fight.",
     "first_in~2": "You went in first. Let someone else start it.", "first_in~3": "Wait for your team to commit, then go.",
     "outnumbered~2": "Too many of them there. Save yourself next time.", "stayed_low~2": "Too low to stay. Back off earlier and heal.",
     "good_trade~2": "Fair fight. That one was worth it.", "good_trade~3": "Good fight. You took them with you.",
     "coach_miss~2": "That one surprised me too. I'll watch closer.",
     "coach_sent": "My call sent you there. That one's on me.", "coach_sent~2": "Bad call from me. Don't go in low next time.",
     "burst": "Burst from full. Keep more distance from their heroes.", "burst~2": "No time to react there. Stand further back next fight.",
     "late_call": "I called it too late. I'll warn you earlier.", "late_call~2": "My call came late there. I'll be quicker.",
     "ganked": "Lane gank. Back off sooner next time.", "ganked~2": "They ganked your lane. Play closer to your tower.",
     "lane_loss": "You lost that lane fight. Stay near your tower.", "lane_loss~2": "They win that trade. Play near your tower.", "fight~1": "Team fight death. That's fine, keep fighting together."}
P = {"got_out": "Good. You got out.", "obeyed": "Good call backing off.", "nice_fight": "Nice fight."}
TALK = {"empty": "Didn't catch that.", "ease": "Okay, easing off those calls.", "quiet": "Okay, quiet until next game.", "back": "I'm back.",
        "test": "I'm here. You'll hear me like this.", "why_R1s": "Enemies were on you.", "why_R1m": "Enemies vanished near you.",
        "why_R11": "You were alone and deep.", "why_R13": "You were low next to them.", "why_R10": "Your tower was dropping.",
        "why_R12": "Your team was fighting close by.", "turbo": "Got it, Turbo game.", "normal": "Okay, normal game.", "mic_off": "Mic off.",
        "low": "Okay, only the important stuff.", "high": "Okay, I'll talk more."}
BANK = {"U": U, "C": C, "B": B, "D": D, "P": P, "T": TALK}
PRIO = {"U": 9, "C": 5, "B": 4, "D": 4, "P": 3, "T": 2.6, "K": 2.5}
VARIANTS = {"U": 3, "C": 2, "D": 2, "P": 2}  # recorded takes per line, picked at random so it never sounds like a tape
CAP = {"low": 12, "normal": 30, "high": 45}  # non-urgent lines per game
RULE = {"get_out": "HP", "danger": "R1", "gank": "R1", "still": "R1", "lost": "R15", "too_deep": "R11", "back_off": "R13", "defend": "R10",
        "join": "R12", "objective": "R14", "rosh": "R4", "wisdom": "T", "tp": "R16", "ult_soon": "T"}  # cue -> rule ("bad call" eases it, "why" explains it)
WORD = {"get_out": "GET OUT", "danger": "GET OUT", "gank": "BACK", "still": "LEAVE", "lost": "DON'T GO", "too_deep": "COME BACK",
        "back_off": "BACK OFF"}  # the retreat calls, as the dead screen quotes them ("I called BACK 9 s before")
LIGHT = ("skill", "buy")  # level-up and can-afford hints: once per level / item, outside the line budget, never delaying a call
GO = ("join", "objective")  # "go" calls: never within GO_AFTER s of a retreat call
U_GAP, NU_GAP, AFTER_U, GO_AFTER, SAME, FRESH, THINK_S, THINK_GAP, PER_MIN = 3, 20, 8, 12, 60, 4, 7, 90, 4  # game-clock seconds
# THINK_S 7: measured 4.9-6.3 s from trigger to a ready clip (LLM 1.3-2.1 s + TTS 3.6-4.3 s); slower than that = dropped, never said late
S, _lock = {}, threading.RLock()


def _reset(match=None):
    S.update(match=match, fight=False, proc=None, prio=0, busy_until=-1e9, last_c=-1e9, last_u=-1e9, last_nu=-1e9, last_retreat=-1e9,
             n_nu=0, n_u=0, times=[], last={}, spoken=None, fight_c=-1e9, said=[], calls=[], queue=[], first={}, pend={}, held=set(), ob_at={}, bank_t=0.0,
             base_next=False, vol=1.0,
             comm={"n": 0, "said": 0, "usd": 0.0, "busy": False, "ready": None, "last": [], "last_c": -1e9, "moment": None, "fight_from": None,
                   "score": None, "phase": None, "r14": False, "items": None, "respawn": None})


def _load_lib():
    try:
        v = json.loads((ROOT / "library.json").read_text())
        return v if isinstance(v, dict) and isinstance(v.get("clips"), dict) else {"clips": {}, "daily": {}}
    except (OSError, ValueError):
        return {"clips": {}, "daily": {}}


_reset()
S.update(want={}, seq=0, failed=set(), gen=False, blocked={}, warned=set(), lib=_load_lib(), secs={},
         ready={f"{p.parent.name}/{p.name[:-4]}" for p in ROOT.glob("*/*.wav") if "-" in p.parent.name and not p.parent.name.startswith(".")})  # skip .live/ commentary
for k in S["ready"] - S["lib"]["clips"].keys():  # a clip whose index entry was lost: re-index it (text unknown)
    S["lib"]["clips"][k] = {"text": k.split("/")[1].replace("_", " "), "file": f"{k}.wav", "provider": k.split("-")[0],
                            "voice": k.split("/")[0].split("-", 1)[1], "source": "unknown", "created": None, "uses": 0, "last_used": None}
if E("VOICE_PROVIDER", "auto") == "auto":  # keep one voice: the provider whose bank is biggest goes first (ties: Gemini)
    ORDER.sort(key=lambda p: -sum(k.startswith(f"{p}-{PROVIDERS[p]['voice']}/") for k in S["ready"]))


# ---- library -------------------------------------------------------------------------------------------------------
def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def bank(p, voice=None):
    return f"{p}-{voice or PROVIDERS[p]['voice']}"


def bank_lines():
    """Every fixed line: [(text, tier)], urgent first."""
    return [(t, tier) for tier, lines in BANK.items() for t in lines.values()]


def _takes(text, tier, p):
    """Library keys of the recorded takes of `text` in provider p's bank. An urgent line only counts with its earcon baked in."""
    s, lib = slug(text), S["lib"]["clips"]
    ks = [f"{bank(p)}/{s}" + (f".v{n}" if n > 1 else "") for n in range(1, 4)]
    return [k for k in ks if k in S["ready"] and (tier != "U" or (lib.get(k) or {}).get("earcon"))]


def find(text, tier=None):
    """Library key of a cached clip for this text (providers in ORDER; a random take when there are several), or None."""
    return next((random.choice(ks) for p in ORDER if p in PROVIDERS and (ks := _takes(text, tier, p))), None)


def secs(key):
    """Clip length in seconds (from the WAV header, cached)."""
    if key not in S["secs"]:
        try:
            with wave.open(str(ROOT / f"{key}.wav"), "rb") as w:
                S["secs"][key] = w.getnframes() / w.getframerate()
        except Exception:
            S["secs"][key] = 1.8
    return S["secs"][key]


def _today():
    return S["lib"]["daily"].setdefault(time.strftime("%Y-%m-%d"), {"n": 0, "usd": 0.0})


def _save():
    """Merge into the on-disk index first: the live server and test replays share data/voice/ (last writer must not drop clips)."""
    with _lock:
        disk = _load_lib()
        disk["clips"].update(S["lib"]["clips"])
        disk["daily"].update(S["lib"]["daily"])  # ponytail: per-process daily counters, the last writer wins for today
        S["lib"] = disk
        ROOT.mkdir(parents=True, exist_ok=True)
        tmp = ROOT / ".library.json.tmp"
        tmp.write_text(json.dumps(S["lib"], indent=1))
        tmp.replace(ROOT / "library.json")


def _use(key):
    with _lock:
        e = S["lib"]["clips"].get(key)
        if e:
            e.update(uses=e.get("uses", 0) + 1, last_used=time.strftime("%Y-%m-%dT%H:%M:%S"))
            _save()


def earcon(kind="urgent", rate=24000):
    """A short two-tone blip as 16-bit mono PCM (numpy, no asset): 'urgent' rises 880 -> 1320 Hz in 150 ms and is prepended to
    every Tier U clip when it is stored; 'on' / 'off' are the softer push-to-talk blips."""
    import numpy as np
    f0, f1 = {"urgent": (880, 1320), "on": (660, 990), "off": (990, 660)}[kind]
    t = np.arange(int(rate * 0.07)) / rate
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.008)  # 8 ms fades: no clicks
    x = np.concatenate([np.sin(2 * np.pi * f0 * t) * env, np.zeros(int(rate * 0.01)), np.sin(2 * np.pi * f1 * t) * env])
    return (x * (0.45 if kind == "urgent" else 0.25) * 32767).astype("<i2").tobytes()


def with_earcon(wav):
    """WAV bytes -> the same clip with the urgent earcon and 60 ms of silence in front."""
    with wave.open(io.BytesIO(wav), "rb") as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(earcon("urgent", rate) + bytes(2 * int(rate * 0.06)) + pcm)
    return out.getvalue()


def earcon_file(kind):
    """data/voice/earcon_listen_on.wav / _off.wav (written once): listen.py plays them on push-to-talk key down / up."""
    f = ROOT / f"earcon_listen_{kind}.wav"
    if not f.exists():
        ROOT.mkdir(parents=True, exist_ok=True)
        with wave.open(str(f), "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000)
            w.writeframes(earcon(kind))
    return f


# ---- TTS -----------------------------------------------------------------------------------------------------------
def to_wav(pcm, rate=24000, thresh=400, pad_ms=40):
    """16-bit mono PCM -> WAV bytes, leading/trailing silence trimmed (snappier in a fight)."""
    if pcm[:4] == b"RIFF":  # Gemini returns a whole WAV with a C2PA signature chunk after the audio; read as PCM that is a burst of static
        with wave.open(io.BytesIO(pcm), "rb") as w:
            pcm, rate = w.readframes(w.getnframes()), w.getframerate()
    a = array.array("h", pcm[:len(pcm) // 2 * 2])
    loud = [i for i, v in enumerate(a) if abs(v) > thresh]  # a 1-2 s clip: a plain scan is fine
    i0, i1 = (loud[0], loud[-1]) if loud else (0, len(a) - 1)
    pad = rate * pad_ms // 1000
    b, out = a[max(0, i0 - pad):i1 + pad + 1], io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(b.tobytes())
    return out.getvalue()


def tts(text, p, voice, style=None):
    """One TTS call -> (wav bytes, usd estimate). Raises on any failure. Keys are sent in headers, never logged."""
    P, key = PROVIDERS[p], E(PROVIDERS[p]["key"]) or ""
    if p == "gemini":
        # Structured prompt: this model reads a plain "<style> Say: <text>" instruction aloud (10 s clips); only # TRANSCRIPT is spoken.
        prompt = f"# AUDIO PROFILE\n{style or STYLE}\n\n# DIRECTOR'S NOTES\nSpeak only the transcript. Never read the profile or these notes aloud.\n\n# TRANSCRIPT\n{text}"
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
        req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{P['model']}:generateContent",
                                     json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.load(r)
        part = resp["candidates"][0]["content"]["parts"][0]["inlineData"]
        rate = re.search(r"rate=(\d+)", part.get("mimeType", ""))
        u = resp.get("usageMetadata") or {}
        usd = (u.get("promptTokenCount") or 0) * 0.5e-6 + (u.get("candidatesTokenCount") or 0) * 10e-6  # ponytail: 2.5-flash-tts list price
        wav = to_wav(base64.b64decode(part["data"]), int(rate.group(1)) if rate else 24000)
        secs, cap = (len(wav) - 44) / 2 / (int(rate.group(1)) if rate else 24000), 2.5 + 0.7 * len(text.split())  # calm style is slower; a style read aloud is still 3x longer
        if secs > cap:  # the model spoke more than the line (e.g. read the style aloud): never let that into the library
            raise ValueError(f"clip too long for its text ({secs:.1f} s > {cap:.1f} s)")
        return wav, usd
    body = {"model": P["model"], "voice": voice, "input": text, "instructions": style or STYLE, "response_format": "pcm"}  # pcm: 24 kHz s16le mono
    req = urllib.request.Request("https://api.openai.com/v1/audio/speech", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        pcm = r.read()
    return to_wav(pcm, 24000), len(text) / 4 * 0.6e-6 + len(pcm) / 48000 / 60 * 0.015  # ponytail: ~$0.015/min audio list price


def provider():
    """First provider in ORDER that has a key and isn't blocked this run."""
    return next((p for p in ORDER if p in PROVIDERS and p not in S["blocked"] and E(PROVIDERS[p]["key"])), None)


def _vkey(text, n):
    return slug(text) + (f".v{n}" if n > 1 else "")


def _missing(text, tier, p):
    """Take numbers of `text` still to record in provider p's bank (1..VARIANTS[tier])."""
    have = set(_takes(text, tier, p))
    return [n for n in range(1, VARIANTS.get(tier, 1) + 1) if f"{bank(p)}/{_vkey(text, n)}" not in have]


def _gen_one(text, source, p=None, voice=None, n=1, tier=None):
    """Generate + cache + index one take of a clip; falls through to the next provider on auth/billing errors. Returns the key or None."""
    s = _vkey(text, n)
    for p in [p] if p else ORDER:
        if p not in PROVIDERS or p in S["blocked"] or not E(PROVIDERS[p]["key"]):
            continue
        v = voice or PROVIDERS[p]["voice"]
        for attempt in range(3):
            with _lock:
                _today()["n"] += 1
            try:
                wav, usd = tts(text, p, v, style_for(tier))
            except urllib.error.HTTPError as e:
                msg = llm.redact(e.read()[:200].decode(errors="replace"))
                if e.code in (400, 401, 403, 404):
                    S["blocked"][p] = f"HTTP {e.code}"
                    log.warning("voice: %s TTS unavailable (HTTP %d %s); trying the next provider", p, e.code, msg)
                    break
                time.sleep(15 * (attempt + 1))  # 429 / 5xx
                continue
            except Exception as e:
                log.warning("voice: %s TTS %r failed (%s)", p, text, llm.redact(repr(e))[:200])
                time.sleep(3)
                continue
            if tier == "U":
                wav = with_earcon(wav)
            key, f = f"{bank(p, v)}/{s}", ROOT / bank(p, v) / f"{s}.wav"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.with_suffix(".tmp").write_bytes(wav)
            f.with_suffix(".tmp").replace(f)
            with _lock:
                S["ready"].add(key)
                S["secs"].pop(key, None)
                S["lib"]["clips"][key] = {"text": text, "file": f"{bank(p, v)}/{s}.wav", "provider": p, "model": PROVIDERS[p]["model"],
                                          "voice": v, "source": source, "tier": tier, "take": n, "earcon": tier == "U", "secs": round(secs(key), 2),
                                          "style": style_for(tier), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "uses": 0, "last_used": None,
                                          "usd": round(usd, 6)}
                _today()["usd"] = round(_today()["usd"] + usd, 6)
                _save()
            return key
        else:
            S["failed"].add(s)  # 3 transient failures: skip this take for this run
            return None
    return None


def _gen_loop():
    while True:
        with _lock:
            capped = _today()["n"] >= DAILY
            if capped and "cap" not in S["warned"]:
                S["warned"].add("cap")
                log.warning("voice: daily TTS cap %d reached (VOICE_GEN_DAILY); library stops growing until tomorrow", DAILY)
            if not S["want"] or not provider() or capped:
                S["gen"] = False
                return
            s = min(S["want"], key=S["want"].get)
            _, _, text, source, n, tier = S["want"].pop(s)
        if n in _missing(text, tier, provider()):
            _gen_one(text, source, n=n, tier=tier)
            time.sleep(GEN_GAP)


def want(text, source="fixed", prio=2, tier=None):
    """Queue the missing takes of a line for background generation (lower prio first). No-op if cached, failed, queued or no provider works."""
    p = provider()
    if not GEN or not p or not slug(text):
        return
    for n in _missing(text, tier, p):
        s = _vkey(text, n)
        with _lock:
            if s in S["failed"] or s in S["want"] or len(S["want"]) >= QUEUE_MAX:
                continue
            S["seq"] += 1
            S["want"][s] = (prio, S["seq"], text, source, n, tier)
            if not S["gen"]:
                S["gen"] = True
                threading.Thread(target=_gen_loop, daemon=True).start()


def _busy(c=None):
    """A clip is playing: the afplay process, or in a dry run the clip's length on the game clock."""
    p = S["proc"]
    return bool(p and p.poll() is None) or (DRY and c is not None and c < S["busy_until"])


def _afplay(path, prio, c=None, dur=1.8):
    try:
        if not DRY:
            S["proc"] = subprocess.Popen(["afplay", "-v", str(round(VOLUME * S["vol"], 3)), str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("voice: afplay failed (%r)", e)
        return False
    S.update(prio=prio, busy_until=(c if c is not None else -1e9) + dur)
    return True


def play(texts, prio, source="fixed", c=None, tier=None):
    """Non-blocking afplay of the first cached text of `texts`; uncached ones are queued for generation.
    One at a time; a higher priority cuts the current line. Returns (how, key, text):
    how = hit | miss (nothing cached: queued, nothing played) | dry-hit | dry-miss | None (busy / failed)."""
    texts = [texts] if isinstance(texts, str) else texts
    key, text = next(((k, t) for t in texts if (k := find(t, tier))), (None, texts[0]))
    for t in texts[:texts.index(text) if key else len(texts)]:
        want(t, source, 2, tier)
    if key is None:
        return ("dry-miss" if DRY else "miss"), None, text
    if _busy(c):
        if prio <= S["prio"]:
            return None, key, text
        if S["proc"] and S["proc"].poll() is None:
            S["proc"].terminate()
    if not _afplay(ROOT / f"{key}.wav", prio, c, secs(key)):
        return None, key, text
    return ("dry-hit" if DRY else "hit"), key, text


def test():
    """POST /api/voice-test: one calm line so the player can check the sound. False when its clip isn't recorded yet (never robotic)."""
    how, _, _ = play(TALK["test"], PRIO["T"], "fixed")
    return bool(how) and how.endswith("hit")


def queue(cue, tier, text, after, until, **kw):
    """coach.py hands over a line (brief, debrief, praise): spoken between game clocks `after` and `until`, else dropped."""
    S["queue"].append({"cue": cue, "tier": tier, "texts": text if isinstance(text, list) else [text], "reason": cue, "keys": [], "after": after, "since": after, "until": until, **kw})


# ---- lines ---------------------------------------------------------------------------------------------------------
def _d(a, b):
    return math.dist(a, b) if a and b and None not in (*a, *b) else math.inf


def _moving(W, c, to=None, away=False, s=2.0, by=300):
    """True if over the last `s` seconds I closed (away: opened) >= `by` units on point `to` (default: my own fountain).
    That is the 'already obeying' test: a retreat call is not spoken to a player who is already leaving."""
    me = W["me"]
    old = next((x for x in W["_"].get("hist") or [] if x[0] >= c - s), None)
    to = to or world.FOUNTAIN.get(me["team"])
    if not old or c - old[0] < s / 2 or me["x"] is None or not to or _d(old[1:3], (me["x"], me["y"])) > 800 * (c - old[0]):
        return False  # no history yet, or a teleport: a jump is not "walking away"
    return (_d(old[1:3], to) - _d((me["x"], me["y"]), to)) * (-1 if away else 1) >= by * (c - old[0]) / s


def hero_name(short):
    import data
    return ((data.S.get("heroes") or {}).get(short) or {}).get("localized_name")


def named_line(name):
    return f"{name} is close. Back off."


def lane_line(name):
    return f"{name} wins that trade. Stay near your tower."


def skill_line(n):
    return "Level up. Take stats." if n.get("name") == "attribute" else "Level up. Pick a talent." if n["slot"] == "talent" else \
        f"Level up. Take {n['label']}."


def buy_line(it):
    return f"You can buy {it['name']} now."


def _hints(W, c, add):
    """Level-up and can-afford hints (LIGHT). Their clips are named per hero and item, so they are recorded as soon as the abilities
    and the item list are known; a hint whose clip isn't there yet stays silent. A skill hint is only spoken from the hero's build
    order (or for the ultimate): the fallback rule's guess at a basic spell is shown, not said."""
    import skills
    me, sk = W["me"], W.get("skill") or {}
    items = [i for i in (W.get("advice") or {}).get("next_items") or [] if i.get("key")][:3]
    key = (me["hero"], tuple(i["key"] for i in items))
    foes = sorted({hero_name(s) for s, e in W["heroes"].items() if e.get("team") and not e["ally"]} - {None})
    key = (*key, tuple(foes))
    if S.get("warm") != key:
        S["warm"] = key
        for n in foes:
            want(named_line(n), "dynamic", 1, "U")
            want(lane_line(n), "dynamic", 1, "D")
        for a in me.get("abilities") or []:
            if not a["name"].startswith("plus_") and "generic_hidden" not in a["name"]:
                want(skill_line({"slot": "", "label": skills.label(a["name"], me["hero"])}), "dynamic", 3, "C")
        for n in ("talent", "attribute"):
            want(skill_line({"slot": "talent", "name": n}), "dynamic", 3, "C")
        for i in items:
            if (i.get("cost") or 0) >= 1000:
                want(buy_line(i), "dynamic", 3, "C")

    def first(name, ident):  # the clock at which this level / this item first came up
        ident = (W["game"]["matchid"], ident)
        if name not in S or S[name][0] != ident:
            S[name] = (ident, c)
        return S[name][1]

    if sk.get("points") and (n := sk.get("next")) and (sk.get("src") == "llm" or n["slot"] == "R"):
        t0 = first("skill_at", me["level"])  # habits.grace: room to do it alone first; a player who forgets gets the short wait
        g = habits.grace("ult" if n["slot"] == "R" else "skill")
        pos = (me["x"], me["y"]) if me["x"] is not None else None
        foe = pos and any(e.get("team") and not e["ally"] and e["visible"] and not e["dead"] and _d((e["x"], e["y"]), pos) <= 1200
                          for e in W["heroes"].values())
        k = int((c - t0 - g) // 90) if c >= t0 + g else 0
        if c >= t0 + g and not foe and k <= 1:  # not in a fight (27:53); unspent: once more after 90 s in a quiet moment, not again
            add("skill", "C", skill_line(n), f"level {me['level']}: {n['name']} (waited {g:.0f} s)", [(f"skill:{me['level']}:{k}", 1e9)],
                since=t0 + g + 90 * k, until=t0 + g + 90 * k + 20, source="dynamic")
    xp = me.get("xp")  # pre-reminder, only for a player who is habitually late with the ultimate: just before level 6
    if me["level"] == 5 and habits.ult_slow() and ((xp or 0) >= habits.XP6 - 250 or (xp is None and c - first("lvl5_at", 5) >= 45)):
        t0 = first("ult_soon_at", 5)
        add("ult_soon", "C", C["ult_soon"], f"level 5, {xp} xp, ult usually waits {habits.median('ult', 0):.0f} s",
            [(f"ult_soon:{W['game']['matchid']}", 1e9)], since=t0, until=t0 + 30)
    full = all(me.get("items") or [None]) and all(me.get("backpack") or [None])  # nowhere to put it: no "buy" line
    shop = me["alive"] is False or (me["x"] is not None and world.zone(me["x"], me["y"], me["team"]) == "base")
    if items and items[0].get("affordable") and (items[0].get("cost") or 0) >= 1000 and shop and not full:  # not for a Magic Stick
        t0 = first("buy_at", items[0]["key"])
        add("buy", "C", buy_line(items[0]), f"{items[0]['key']} {items[0]['cost']}g", [(f"buy:{items[0]['key']}", 1e9)], since=t0, until=t0 + 30,
            source="dynamic")


def cues(W, c):
    """Candidate lines from the game state now: [{cue, tier, texts, reason, keys: [(rate key, seconds)], rule, obeyed, since}],
    most important first. Urgent cues live as long as their condition; calls are dropped by _speak when they can't start in time.
    Sets S['fight'] (fight mode: only urgent lines)."""
    me, out, soft = W["me"], [], W["_"].get("soft", ())
    named = None  # "<Hero> is close. Back off." when exactly one enemy hero is the threat (set below)

    def add(cue, tier, text, reason, keys=(), obeyed=False, **kw):
        if RULE.get(cue) not in soft or kw.get("force"):  # eased by "bad call" or demoted by the precision gate (force: too clear)
            out.append({"cue": cue, "tier": tier, "texts": ([named] if cue in ("gank", "back_off") and named else []) + [text], "reason": reason, "keys": list(keys), "rule": RULE.get(cue), "obeyed": obeyed, **kw})

    if me["alive"] is not True or None in (me["x"], me["y"]) or me["hp_pct"] == 0:  # 0 HP = dead a tick before GSI says so
        S["fight"] = False  # dead: the debrief comes through the queue
        return out
    pos, team, hp = (me["x"], me["y"]), me["team"], me["hp_pct"] if me["hp_pct"] is not None else 100
    vis = sorted((_d((e["x"], e["y"]), pos), s) for s, e in W["heroes"].items()
                 if e.get("team") and not e["ally"] and s != me["hero"] and e["visible"] and not e["dead"])
    near = [(r, s) for r, s in vis if r <= 1500]
    d = W.get("decision")
    fp = d["fight"]["p"] if d and c - d["at_clock"] <= jev.STALE else 0
    S["fight"] = len(near) >= (2 if W.get("phase") == "laning" else 1) or (  # laning: one opponent isn't a fight
        (fp >= 0.6 or W["attention"]["level"] == "high") and any(r <= 2500 for r, _ in vis))  # no enemy in sight = no fight (it stuck)
    if S["fight"]:
        S["fight_c"] = c
    A = {a["rule"]: a for a in W["alerts"] if a["level"] != "info"}
    home, zone = _moving(W, c), world.zone(*pos, team)

    def ep(a):  # once per alert episode (an escalation is a new episode)
        return (f"ep:{a['id']}:{a['level']}:{a['at']}", 1e9)

    # ---- Tier U: get out (low HP) > danger > don't go > gank > still in danger
    alone = not any(e["ally"] and s != me["hero"] and e["visible"] and not e["dead"] and _d((e["x"], e["y"]), pos) <= 1200
                    for s, e in W["heroes"].items())
    on_me = bool(near) and near[0][0] <= 400  # in melee range: burst kills from 50% ("Get out!" at 25-29% came 3-4 s before deaths)
    old = next((x for x in W["_"].get("hist") or [] if x[0] >= c - 3), None)
    falling = (old[3] - hp) if old else 0  # HP lost in the last 3 s
    if near and near[0][0] <= 700 and falling >= 0 and hp < (50 if (on_me and alone) or falling >= 25 else 35 if c >= 1200 else 30) \
            and c - S["last_retreat"] >= 10:  # one per 10 s; not at 10% and healing with Morphling at 1320 (1:27)
        e = W["heroes"][near[0][1]]
        add("get_out", "U", U["get_out"], f"hp {hp}%, {near[0][1]} at {near[0][0]:.0f}u", [("get_out", 25)],
            obeyed=not on_me and falling < 10 and (home or _moving(W, c, (e["x"], e["y"]), away=True)), why="R13")
    r1 = A.get("R1")
    th = (W.get("hud") or {}).get("threats") or []
    gone = falling < 10 and (home or (bool(th) and _moving(W, c, (sum(t["x"] for t in th) / len(th), sum(t["y"] for t in th) / len(th)),
                                                         away=True)))  # losing HP = not escaping (1:40: "obeyed" while 51 -> 8%)
    why = "R1s" if r1 and r1.get("seen") else "R1m"
    if r1 and r1["level"] == "high":
        add("danger", "U", U["danger"], r1["text"], [("danger", 25), ("r1", 8), ep(r1)], obeyed=gone, since=r1["at"], why=why)
    if a := A.get("R15"):
        add("lost", "U", U["lost"], a["text"], [("lost", 25), ep(a)], since=a["at"])
    seen_near = sorted((_d((t["x"], t["y"]), pos), t["hero"]) for t in th if t.get("kind") == "seen" and t.get("hero"))
    named = named_line(n) if seen_near and len(seen_near) == 1 and (n := hero_name(seen_near[0][1])) else None  # one hero: say who
    if r1 and r1["level"] == "warn":
        add("gank", "U", U.get(f"gank_{r1.get('dir')}") or U["gank"], r1["text"], [("gank", 25), ("r1", 8), ep(r1)], obeyed=gone, since=r1["at"],
            why=why)  # "r1": a warn that turns high a second later is one event, not two lines; "Get out. Now." follows after 8 s if it lasts
    if (a := A.get("R13")) and c - S["last_retreat"] >= 10:  # one retreat family: "Get out!" then "Back off" 4 s later (2:29)
        e = W["heroes"][vis[0][1]] if vis else None
        add("back_off", "U", U["back_off"], a["text"], [("back_off", 25), ep(a)], since=a["at"], why="R13",
            obeyed=home or (bool(e) and _moving(W, c, (e["x"], e["y"]), away=True)))
    S["r1_hi"] = (S.get("r1_hi") or c) if r1 and r1["level"] == "high" else None  # a refire resets r1["at"]; the danger didn't stop
    if r1 and r1["level"] == "high" and c - S["r1_hi"] >= 10 and c - S["last_retreat"] >= AFTER_U:  # once per episode, only if still not leaving
        add("still", "U", U["still"], f"R1 high for {c - S['r1_hi']:.0f} s, not leaving", [(f"ep:still:{S['r1_hi']}", 1e9)], obeyed=gone, why=why)
    # ---- Tier C
    if a := A.get("R11"):
        close = bool(vis) and vis[0][0] <= 1000 and alone  # dropped in fight mode with CM at 933 (16:20): urgent then
        add("too_deep", "U" if close else "C", C["too_deep"], a["text"], [("too_deep", 45), ep(a)], obeyed=home, since=a["at"], why="R11", force=a.get("force"))
    busy_here = bool(vis) and vis[0][0] <= 800  # an enemy on me: "Defend top" with Razor at 239 units (review 8:13)
    lost_cause = (a := A.get("R10")) and ((a.get("tier") or 0) < 3 and (a.get("hp") or 100) < 30 and a.get("zone") != zone  # 18%, far away
                                          or "fort" in str(a.get("building")) and (a.get("hp") or 100) < 20)  # the game ends in seconds
    if a and a.get("defend") is not False and (a.get("tier") or 0) < 3 and a.get("zone") != zone and (hp < 60 or me.get("tp") is False):
        lost_cause = True  # a 37% Sniper TP'd onto Morphling on "Defend mid." (7:12); no TP = can't get there (9:35)
    if (a := A.get("R10")) and not busy_here and not lost_cause and ((a.get("foes") or 0) > 0 or (a.get("hp") or 100) < 30 or (a.get("tier") or 0) >= 3):  # creeps alone at 43%: screen only (17:18)  # structured fields from rules.defend; LET IT GO is its own line (it used to be read as "Defend the base!")
        there = a.get("zone") == zone
        if a.get("defend") is False:
            text, done = C["let_go"], not there  # only worth saying to a player who is standing in it
        elif (a.get("tier") or 0) >= 3:
            text, done = (C["defend_base"] if S["base_next"] else C["base_next"]), there
        else:
            text, done = C.get(f"defend_{a.get('lane')}") or C["defend_base"], there
        add("defend", "C", text, a["text"], [("defend", 45), ep(a)], obeyed=done, since=a["at"], why="R10")
    if (a := A.get("R12")) and (f := (W.get("hud") or {}).get("fight")):  # the alert outlives its fight by up to 8 s: say it only while it's on
        add("join", "C", C["join"], a["text"], [("join", 60), ep(a)], obeyed=bool(f) and _moving(W, c, (f["x"], f["y"])), since=a["at"], why="R12")
    if a := A.get("R14"):
        add("objective", "C", C.get(f"objective_{a.get('dead')}") or C["objective"], a["text"], [("objective", 90), ep(a)], since=a["at"],
            obeyed=(pos[0] + pos[1]) * (1 if team == "radiant" else -1) > 3000)  # already on their side of the map
    if a := next((a for a in W["alerts"] if a["rule"] == "R16"), None):  # an info alert: not in A
        add("tp", "C", C["tp"], a["text"], [("tp", 180), ep(a)], since=a["at"])
    if (a := A.get("R4")) and a["level"] == "high":
        add("rosh", "C", C["rosh"], a["text"], [("rosh", 180), ep(a)], since=a["at"])
    _hints(W, c, add)
    for a in W["alerts"]:  # the one timer that is spoken (rules.timers already filters it to roles 3-5)
        unseen = sum(1 for e in W["heroes"].values() if e.get("team") and not e["ally"] and not e["dead"] and not e["visible"])
        friend = any(e["ally"] and s != me["hero"] and e["visible"] and not e["dead"] and _d((e["x"], e["y"]), pos) <= 1500 for s, e in W["heroes"].items())
        if a["rule"] == "T" and a["id"].split(":")[1] == "Wisdom shrine" and (pos[0] + pos[1] > 0) == (team == "dire") \
                and (friend or unseen <= 1):  # sent a support alone to the shrine with 4 unseen; dead at 6:59
            add("wisdom", "C", C["wisdom"], a["text"], [(f"al:{a['id']}", 600)], since=a["at"])
    return out


def _blocked(W, c, x, busy):
    """Why this line may not start now (None = it may). Nothing but another urgent line ever holds up an urgent line."""
    t = x["tier"]
    if t == "U":
        return "urgent gap" if c - S["last_u"] < U_GAP else None
    if busy:
        return "busy"
    if t == "P" and (W["me"]["hp_pct"] or 100) < 40:
        return "low hp"  # "Nice fight" 3 s before dying (review 2026-11-12)
    if x["cue"] == "nice_fight" and c - S["last_u"] >= AFTER_U:  # praise for a fight we are winning: on time or never, also mid-fight
        return None
    if S["fight"]:
        return "fight"
    if t in ("B", "D"):  # the brief and the debrief are sequences at a safe moment (pre-horn, dead): only a playing clip holds them
        return None
    if c - S["last_u"] < AFTER_U:
        return "after urgent"
    if any(a["rule"] == "R1" and a["level"] != "info" for a in W["alerts"]):
        return "danger up"
    if sum(c - t0 < 60 for t0 in S["times"]) >= PER_MIN - 1:
        return "minute full"  # calls, praise and prompts stop at 3 lines in a rolling minute (urgent lines and the debrief do not)
    if x["cue"] in GO and c - S["last_retreat"] < GO_AFTER:
        return "after retreat"
    return "gap" if c - S["last_nu"] < NU_GAP else None


def _capped(W, x):
    """Per-game budget: the cap counts every non-urgent line; once it is used up, and at verbosity low from the start, only
    urgent lines and the debrief's cause line are left."""
    verb = W["settings"].get("verbosity") or "normal"
    return x["tier"] != "U" and not x.get("cause") and x["cue"] not in LIGHT and (verb == "low" or S["n_nu"] >= CAP.get(verb, CAP["normal"]))


def _drop(x, c, since, why):
    rec.event("voice", op="drop", cue=x["cue"], tier=x["tier"], text=x["texts"][0], reason=why, waited=round(c - since, 2))
    log.info("voice %s: (dropped, %s) %s", world.mmss(c), why, x["texts"][0])
    if x in S["queue"]:
        S["queue"].remove(x)


def _speak(W, c, cands):
    """Speak at most one line: the most important candidate that is wanted (not rate-limited, not capped), not already being
    obeyed, and allowed to start now. A waiting non-urgent line is dropped once it is late; nothing is ever said late."""
    busy, seen = _busy(c), set()
    for x in sorted(cands, key=lambda x: (-PRIO[x["tier"]], x["cue"] in LIGHT)):  # a real call before a level-up / buy hint
        k, tier, text = x["cue"], x["tier"], x["texts"][0]
        keys = x["keys"] + ([] if tier == "U" else [(f"t:{slug(text)}", SAME)])  # the exact same line not within 60 s (urgent: 25 s per cue)
        if any(c - S["last"].get(kk, -1e9) < s for kk, s in keys) or _capped(W, x):
            continue
        seen.add(k)
        if x.get("obeyed"):
            S["ob_at"][k] = c
            if k not in S["held"]:
                S["held"].add(k)
                rec.event("voice", op="obeyed", cue=k, tier=tier, text=text, reason=x["reason"])
            S["first"].pop(k, None)
            continue
        if c - S["ob_at"].get(k, -1e9) < (2 if tier == "U" else 3):  # flicker: logged "obeyed", then spoken 1 s later (0:35, 16:48)
            S["first"].pop(k, None)
            continue
        if tier != "U" and "after" not in x and k not in S["first"] and c - x.get("since", c) > FRESH:  # held then un-held = late too ("Base is next" 13 s after the rax fell)
            for kk, sec in keys:  # it became true while its cooldown ran: too late now, this episode stays silent
                if sec >= 600:
                    S["last"][kk] = c
            _drop(x, c, x["since"], "cooldown")
            continue
        new = x.get("since") is not None and (c - x["since"] <= 0.5 or "after" in x) and k not in S["held"]
        since = S["first"].setdefault(k, x["since"] if new else c)
        why = _blocked(W, c, x, busy)
        if c > x.get("until", since + FRESH if tier != "U" else math.inf):
            for kk, s in keys:
                if s >= 600:  # this episode is over for the voice; the cue's own cooldown only starts when it is spoken
                    S["last"][kk] = c
            _drop(x, c, since, (S["pend"].pop(k, None) or {}).get("why") or why or "late")
            continue
        how, key, t = play(x["texts"], PRIO[tier], x.get("source", "fixed"), c, tier) if not why else (None, None, text)
        if how is None:
            S["pend"][k] = {"x": x, "since": since, "why": why or "busy"}
            continue
        for kk, _ in keys:
            S["last"][kk] = c  # a miss counts too: no re-logging every tick
        S["pend"].pop(k, None), S["first"].pop(k, None)
        dur = secs(key) if key else 0
        rec.event("voice", op="miss" if how.endswith("miss") else "hit", cue=k, tier=tier, text=t, wanted=text, reason=x["reason"], prio=PRIO[tier],
                  how=how, clip=key, source=x.get("source", "fixed"), delay=round(max(0, c - since), 2), fight=S["fight"], secs=round(dur, 2))
        if x in S["queue"]:
            S["queue"].remove(x)
        if how.endswith("miss"):
            log.info("voice %s: (not recorded yet, queued) %s  (%s)", world.mmss(c), text, x["reason"])
            continue  # silent; a lesser cached line may still speak
        if how == "hit":
            _use(key)
        me = W["me"]
        S.update(last_c=c, spoken={"text": t, "cue": k, "tier": tier, "at_clock": c}, **({"last_u": c, "n_u": S["n_u"] + 1} if tier == "U" else
                                                                                       {"last_nu": c} if k in LIGHT else {"last_nu": c, "n_nu": S["n_nu"] + 1}))  # a hint holds the next call 20 s too (BKB + "They lost three" 2 s apart)
        S["times"] = [t for t in S["times"] if c - t < 60] + ([] if k in LIGHT or tier in ("U", "D") else [c])
        if k in WORD:
            S["last_retreat"] = c
        if t == C["base_next"]:
            S["base_next"] = True
        S["calls"] = (S["calls"] + [{"c": c, "cue": k, "tier": tier, "rule": x.get("rule"), "word": WORD.get(k), "why": x.get("why"), "text": t,
                                     "home": world.home_dist(me["x"], me["y"], me["team"])}])[-40:]
        S["said"] = (S["said"] + [t])[-4:]
        W["voice"]["last"] = S["spoken"]
        log.info("voice %s: [%s] %s  (%s; %s)", world.mmss(c), tier, t, x["reason"], how)
        return k
    for k in [k for k in S["pend"] if k not in seen]:  # the condition ended while the line was waiting
        p = S["pend"].pop(k)
        if p["x"] not in S["queue"]:  # a queued line lives until its own deadline (the GSI clock can step back a few hundredths)
            _drop(p["x"], c, p["since"], p["why"] + ", then over")
    S["first"] = {k: v for k, v in S["first"].items() if k in seen}
    S["held"] &= seen
    return None


def _bank(W):
    """Warm-up: queue every fixed line that has no clip yet (urgent first)."""
    for text, tier in bank_lines():
        want(text, "fixed", {"U": 0, "C": 1, "D": 1}.get(tier, 2), tier)


def tick(W):
    """Call every tick under world.lock. Speaks at most one line; sets W['voice']."""
    g, c, st = W["game"], W["game"]["clock"], W["settings"]
    on = st.get("voice") is not False  # default on
    if S["match"] != g["matchid"]:
        _reset(g["matchid"])
    S["vol"] = max(0.0, min(1.0, v)) if isinstance(v := st.get("volume"), (int, float)) else 1.0
    if on and GEN and time.time() - S["bank_t"] >= 5 and provider():
        S["bank_t"] = time.time()
        _bank(W)
    K, verb = S["comm"], st.get("verbosity") or "normal"
    W["voice"] = {"enabled": on, "dry": DRY, "fight": S["fight"], "last": S["spoken"], "clips_ready": len(S["ready"]),
                  "queue": len(S["want"]), "provider": provider(), "blocked": dict(S["blocked"]), "today": dict(_today()),
                  "spoken": S["n_nu"], "urgent": S["n_u"], "cap": CAP.get(verb, CAP["normal"]),
                  "commentary": {"on": _comm_enabled(), "n": K["said"], "max": THINK_MAX * (2 if verb == "high" else 1), "usd": K["usd"],
                                 "busy": K["busy"], "last": K["last"][-1] if K["last"] else None}}
    pre = g["state"] == PRE_GAME
    if not on or st.get("quiet") or c is None or g["paused"] or not (pre or (g["state"] == IN_PROGRESS and c > 0)) or (W["src_age"]["gsi_s"] or 0) > 5:
        S["queue"] = []  # off, quiet, paused or no game: what was waiting is gone, not postponed
        return None
    S["queue"] = [q for q in S["queue"] if not (q.get("alive") and W["me"]["alive"] is False)]  # praise is for the living
    sp = S["spoken"] or {}
    if W["me"]["alive"] is True and sp.get("tier") in ("D", "K") and "respawn" in str(sp.get("text", "")).lower() and _busy(c):
        S["proc"].terminate()  # a "you respawn ..." line still playing once the hero is back: stop it
    cue = _speak(W, c, ([] if pre else cues(W, c)) + [q for q in S["queue"] if c >= q["after"]])
    S["queue"] = [q for q in S["queue"] if c <= q["until"]]
    if not pre:
        _think(W, c, cue)
    return cue


# ---- thinking prompts ----------------------------------------------------------------------------------------------
def _comm_enabled():
    return COMMENTARY and not llm.DISABLED and not llm.PAUSED and bool(llm.TARGETS)


def _sentence(text, n=18):
    """At most n words, cut at the last full sentence (never mid-sentence); None when no full sentence fits."""
    w = str(text or "").split()
    if len(w) <= n:
        return " ".join(w) or None
    m = re.match(r".*[.!?]", " ".join(w[:n]), re.S)
    return m[0] if m else None


def _moments(W, c):
    """Something relevant just changed -> S['comm']['moment'] = (trigger clock, what happened). There is no timer:
    when nothing changed, nothing is said."""
    K, me, sc = S["comm"], W["me"], W["game"]["score"]

    def mark(what, at=c):
        K["moment"] = (at, what)
    if me["alive"] is not False:
        K["respawn"] = None
    elif (me["respawn_s"] or 99) <= 12 and not K["respawn"]:  # LLM + TTS take ~6 s: asked now so it plays before the hero is back
        K["respawn"] = True
        K["resp_n"] = K.get("resp_n", 0) + 1
        if K["resp_n"] <= 3:  # 14 deaths = 14 near-identical prompts, and the Roshan prompt found the budget used up
            mark("you respawn soon: where to go and what to do first")
    my = 0 if me["team"] == "radiant" else 1
    if S["fight"]:
        if not K["fight_from"]:
            K["fight_from"], K["score"] = c, list(sc)
    elif K["fight_from"] and c - S["fight_c"] >= 4:
        k, dth = sc[my] - K["score"][my], sc[1 - my] - K["score"][1 - my]
        if S["fight_c"] - K["fight_from"] >= 5 and k != dth and k + dth >= 2 and me["alive"]:  # first blood elsewhere is not "a fight lost" (1:17)
            mark(f"your team just {'won' if k > dth else 'lost'} a fight ({k} kills for {dth} deaths)")
        K["fight_from"] = None
    r14 = any(a["rule"] == "R14" for a in W["alerts"])
    if K["r14"] and not r14 and me["alive"]:
        mark("the push window just closed: the dead enemies are coming back")
    K["r14"] = r14
    ph = W.get("phase")
    if K["phase"] in ("laning", "mid") and ph in ("mid", "late") and ph != K["phase"]:
        mark(f"the game just moved from {K['phase']} to {ph} game")
    K["phase"] = ph
    for e in W["events"]:
        if e.get("type") in ("roshan_killed", "aegis_picked_up") and 0 <= c - (e.get("clock") or -1e9) <= 1:
            mark(f"Roshan just died (killed by {e.get('killed_by_team') or '?'})" if e["type"] == "roshan_killed" else "the Aegis was just picked up")
    seen = {(h, i) for h, v in ((W.get("inspect") or {}).get("seen") or {}).items() for i in v.get("items") or [] if i}
    if K["items"] is not None and seen - K["items"]:
        import data
        big = [(h, i) for h, i in sorted(seen - K["items"]) if (world.num((data.S["items"].get(i) or {}).get("cost"), 0) or 0) >= 2000]
        if big:
            mark(f"you just saw that enemy {big[0][0].replace('_', ' ')} has {big[0][1].replace('_', ' ')}")
    K["items"] = seen


def _comm_input(W, c, reason):
    d = W.get("decision")
    jv = (f"{d['action']['choice']} at {d['where']['choice']}" if d and c - d["at_clock"] <= 20 and d["action"]["choice"] and d["action"]["p"] >= 0.6 else "-")
    seen = "; ".join(f"{h}: {', '.join(i for i in (s.get('items') or []) if i)}" for h, s in ((W.get("inspect") or {}).get("seen") or {}).items())
    al = "; ".join(f"{a['level']} {a['text']}" for a in W["alerts"] if c - a["at"] <= 60)
    return (f"Moment: {reason}\nGame: {world.to_text()}\nGoal this game: {(W.get('goal') or {}).get('text') or '-'} (deaths so far {W['me']['d']})\n"
            f"Alerts: {al[:400] or '-'}\nDecision model suggests: {jv}\nEnemy items seen: {seen[:400] or '-'}\n"
            f"Short calls you just made: {' | '.join(S['said'][-4:]) or '-'}\nYour recent prompts (don't repeat): {' | '.join(S['comm']['last'][-3:]) or '-'}\n")


def _comm_start(W, c, moment):
    """One background LLM call (4 s limit) -> one sentence, then live TTS (not cached) -> S['comm']['ready']."""
    K, mid, user = S["comm"], W["game"]["matchid"], _comm_input(W, c, moment[1])
    hero, role = W["me"]["hero"], W["me"]["role"]
    K.update(busy=True, n=K["n"] + 1, moment=None)

    def run():
        text, usd = None, 0.0
        try:
            import postgame
            hints = postgame.profile_hints(hero, role)
        except Exception:
            hints = ""
        for t in llm.TARGETS[-1:] if THINK_LLM == "fallback" else llm.TARGETS[:1]:  # one try: a second would be late anyway
            try:
                out, u, _ = llm._post(t, COMM_SYSTEM, user + (f"Player tendencies: {hints}\n" if hints else ""), 600, COMM_SCHEMA, 4)
                usd += u
                text = _sentence(out.get("say"))
                if text and any(difflib.SequenceMatcher(None, text.lower(), x.lower()).ratio() > 0.7 for x in S["comm"]["last"]):
                    text = None  # the third near-identical respawn prompt (23:42)
                if text and re.search(r"\bseconds?\b|\d", text):
                    text = None  # "You respawn in twelve seconds" was heard with 8 s left (review 25:09, 29:08)
            except Exception as e:
                log.warning("voice prompt %s: %s", t["model"], llm.redact(repr(e))[:200])
        wav, p = None, provider()
        if text and not DRY and p:
            try:
                wav, u = tts(text, p, PROVIDERS[p]["voice"], COMM_STYLE)
                usd += u
            except Exception as e:
                log.warning("voice prompt TTS: %s", llm.redact(repr(e))[:200])
        with world.lock:
            if S["match"] != mid:
                return
            K.update(busy=False, usd=round(K["usd"] + usd, 5))
            if text and (wav or DRY):
                f = None
                if wav:
                    f = ROOT / ".live" / f"c{K['n'] % 4}.wav"  # 4 rotating files; never in the library
                    f.parent.mkdir(parents=True, exist_ok=True)
                    f.write_bytes(wav)
                K["ready"] = (f, text, moment[0], moment[1], usd, (len(wav) - 44) / 48000 if wav else 0.4 * len(text.split()))
    threading.Thread(target=run, daemon=True).start()


def _think(W, c, spoke):
    """Thinking prompt: only right after a moment (see _moments), only in a quiet moment, started within 2 s and spoken within
    THINK_S of the trigger, else dropped. At most THINK_MAX per game (x2 at verbosity high, none at low)."""
    K, st = S["comm"], W["settings"]
    _moments(W, c)
    verb = st.get("verbosity") or "normal"
    x = {"cue": "think", "tier": "K", "texts": [""]}
    if not _comm_enabled() or verb == "low" or st.get("commentary") is False or _capped(W, x) or K["said"] >= THINK_MAX * (2 if verb == "high" else 1):
        K["moment"] = K["ready"] = None
        return
    if r := K["ready"]:  # (file, text, trigger clock, what, usd, seconds)
        why = "spoke" if spoke else _blocked(W, c, x, _busy(c))
        if c > r[2] + THINK_S or (r[3].startswith("you respawn") and W["me"]["alive"] is True):  # already back: too late
            K["ready"] = None
            _drop({"cue": "think", "tier": "K", "texts": [r[1]]}, c, r[2], why or "late")
        elif not why and _afplay(r[0], PRIO["K"], c, r[5]):
            K.update(ready=None, said=K["said"] + 1, last=(K["last"] + [r[1]])[-3:], last_c=c)
            S.update(last_c=c, last_nu=c, n_nu=S["n_nu"] + 1, times=S["times"] + [c], spoken={"text": r[1], "cue": "think", "tier": "K", "at_clock": c})
            W["voice"]["last"] = S["spoken"]
            log.info("voice %s: [K] %s  (%s; %s)", world.mmss(c), r[1], r[3], "dry" if DRY else "live")
            rec.event("voice", op="live", cue="think", tier="K", text=r[1], reason=r[3], usd=round(r[4], 5), how="dry" if DRY else "live",
                      delay=round(max(0, c - r[2]), 2), fight=S["fight"], secs=round(r[5], 2))
        return
    m = K["moment"]
    if not m or K["busy"]:
        return
    if c - K["last_c"] < THINK_GAP or K["n"] >= 3 * THINK_MAX:
        K["moment"] = None  # one just spoke: this change goes by without a word
        return
    quiet = not S["fight"] and c - S["last_u"] >= AFTER_U - 3 and c - S["last_nu"] >= NU_GAP - 3 and not _busy(c) \
        and sum(c - t < 57 for t in S["times"]) < PER_MIN - 1 \
        and not any(a["rule"] in ("R1", "R11", "R13", "R15") and a["level"] != "info" for a in W["alerts"])
    if quiet and c >= m[0] - 3:  # only asked for when it could be spoken once it is ready (about 3 s)
        _comm_start(W, c, m)
    elif c - m[0] > 2:  # no quiet moment in time: the prompt would be late, so it is never asked for
        K["moment"] = None
        rec.event("voice", op="drop", cue="think", tier="K", text="", reason="not quiet", waited=round(c - m[0], 2))


# ---- self-check / CLI ----------------------------------------------------------------------------------------------
def selfcheck():
    """[(name, ok, detail)]: WAV + earcon, library/provider fallthrough and takes (fake TTS), tiers, budget, relevance, queue,
    thinking prompts. No audio, no network."""
    global DRY, GEN, ROOT, ORDER, tts, find, secs, _comm_enabled, _comm_start
    import copy, tempfile
    res, saved = [], (DRY, GEN, ROOT, ORDER, tts, find, secs, dict(S), dict(os.environ), _comm_enabled, _comm_start, rec.event)
    _comm_enabled, started, evs = (lambda: False), [], []
    _comm_start = lambda W, c, m: (started.append((c, m[1])), S["comm"].update(moment=None, n=S["comm"]["n"] + 1))  # noqa: E731 (no network)
    rec.event = lambda kind, **kw: evs.append((kind, kw))  # noqa: E731
    DRY, GEN, ROOT, ORDER = True, False, Path(tempfile.mkdtemp(prefix="voice-check-")), ["gemini", "openai"]
    S.update(want={}, seq=0, failed=set(), gen=True, blocked={}, lib={"clips": {}, "daily": {}}, ready=set(), secs={})  # gen=True: no thread
    os.environ.update(GEMINI_API_KEY="dummy-g", OPENAI_API_KEY="dummy-o")
    try:
        tone = array.array("h", [0] * 12000 + [int(8000 * math.sin(i / 5)) for i in range(4800)] + [0] * 12000).tobytes()
        with wave.open(io.BytesIO(to_wav(tone)), "rb") as w:
            res.append(("voice: WAV 24 kHz mono 16-bit, silence trimmed", (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
                        and 4800 <= w.getnframes() <= 4800 + 2 * 960, f"{w.getnframes()} frames"))
        res.append(("voice: a WAV with a trailing signature chunk (Gemini C2PA) is unwrapped, not played as noise",
                    to_wav(to_wav(tone) + b"C2PA" + (4096).to_bytes(4, "little") + os.urandom(4096)) == to_wav(tone), ""))
        cut = [_sentence("Take the bot tower now. " + "word " * 30), _sentence("word " * 30), _sentence("Go mid.")]
        res.append(("voice: slug; a prompt is cut at the last full sentence, never mid-sentence", slug("Don't go. It's lost.") == "don_t_go_it_s_lost"
                    and cut == ["Take the bot tower now.", None, "Go mid."], str(cut)))

        def fake(text, p, voice, style=None):
            if p == "gemini":
                raise urllib.error.HTTPError("u", 403, "billing", {}, io.BytesIO(b"{}"))
            return to_wav(tone), 0.0003
        tts = fake
        key = _gen_one("Get out!", "fixed", tier="U")
        e = json.loads((ROOT / "library.json").read_text())["clips"].get(key or "-") or {}
        res.append(("voice: Gemini 403 -> OpenAI clip cached + indexed in library.json", key == f"openai-{PROVIDERS['openai']['voice']}/get_out"
                    and (ROOT / f"{key}.wav").exists() and e.get("provider") == "openai" and e.get("source") == "fixed"
                    and S["blocked"].get("gemini") == "HTTP 403", f"{key} {e}"))
        plain = _gen_one("Defend mid.", "fixed", tier="C")
        k2, k3 = _gen_one("Get out!", "fixed", n=2, tier="U"), _gen_one("Get out!", "fixed", n=3, tier="U")
        got = {find("Get out!", "U") for _ in range(40)}
        S["lib"]["clips"][key]["earcon"] = False
        res.append(("voice: urgent clips carry the earcon (0.21 s longer); 3 takes picked at random; a take without earcon is not used",
                    abs(secs(key) - secs(plain) - 0.21) < 0.02 and e.get("earcon") and got == {key, k2, k3} and k2.endswith("get_out.v2")
                    and {find("Get out!", "U") for _ in range(40)} == {k2, k3} and _missing("Get out!", "U", "openai") == [1],
                    f"{secs(key):.2f} vs {secs(plain):.2f} s, takes {sorted(got)}"))
        S["lib"]["clips"][key]["earcon"] = True
        _use(key)
        res.append(("voice: cached line plays from the library (dry-hit), uses counted", play("Get out!", 9, tier="U")[0] == "dry-hit"
                    and S["lib"]["clips"][key]["uses"] == 1, ""))
        DRY, GEN = False, True
        how, _, _ = play("Some brand new line.", 1, "dynamic")
        res.append(("voice: uncached line -> silent miss (no fallback voice), queued", how == "miss" and S["proc"] is None
                    and "some_brand_new_line" in S["want"], how))
        DRY = True
        S["want"] = {}
        with world.lock:
            world.reset()
            base = copy.deepcopy(world.W)
        _bank(base)
        n_takes = sum(VARIANTS.get(t, 1) for _, t in bank_lines())
        res.append(("voice: warm-up queues every fixed line with its takes; nothing that was removed is in the bank",
                    len(S["want"]) == n_takes - 4 and {"get_out_now.v3", "back_off_heal_up.v2", "one_goal_this_game_six_deaths_or_fewer"} <= set(S["want"])
                    and not any(re.search(r"missing|smoke|talent|stack|careful|push now|can buy", t, re.I) for t, _ in bank_lines()),
                    f"{len(S['want'])} queued of {n_takes} takes, {len(bank_lines())} lines"))
        GEN = False
        find = lambda text, tier=None: f"x/{slug(text)}"  # noqa: E731 (steady state: every line has a clip)
        secs = lambda key: 1.5  # noqa: E731
        base["game"].update(state=IN_PROGRESS, clock=900, matchid="t", paused=False)
        base["src_age"]["gsi_s"] = 0.5
        base["settings"] = {"voice": True}  # the real data/settings.json may have it off
        base["phase"] = "mid"
        base["me"].update(hero="sven", team="radiant", alive=True, hp_pct=80, x=0, y=0, role=5, gold=100, buyback_cost=2000, d=0)
        base["heroes"] = {"lion": {"team": "dire", "ally": False, "visible": False, "dead": False, "x": 800, "y": 0},
                          "sniper": {"team": "dire", "ally": False, "visible": False, "dead": False, "x": None, "y": None}}

        def run(steps, W=None):
            """steps: [(clock, edit fn)] -> [spoken text or None]; fresh state each run."""
            W, got = W or copy.deepcopy(base), []
            _reset("t")
            for c, f in steps:
                W["game"]["clock"] = c
                f and f(W)
                tick(W)
                got.append(S["spoken"]["text"] if S["spoken"] and S["spoken"]["at_clock"] == c else None)
            return got

        def al(rule, lvl, text="x", **kw):
            return lambda W: W.__setitem__("alerts", [a for a in W["alerts"] if a["rule"] != rule] + (
                [{"id": rule, "rule": rule, "level": lvl, "text": text, "at": W["game"]["clock"], "ttl": 20, **kw}] if lvl else []))

        def lion(**kw):
            return lambda W: W["heroes"]["lion"].update(**kw)

        def me(**kw):
            return lambda W: W["me"].update(**kw)
        g = run([(900, al("R1", "warn", dir="river")), (901, al("R1", "high")), (904, None), (908, None), (909, al("R1", None)),
                 (912, lambda W: (me(hp_pct=20)(W), lion(visible=True, x=500)(W))), (918, None), (919, None)])
        res.append(("voice: urgent tier: gank by direction; a warn that turns high is one line, 'Get out. Now.' follows after 8 s if it "
                    "lasts; low HP 'Get out!' not within 10 s of another retreat call; each cue once per episode",
                    g == ["Gank from the river. Back.", None, None, "Get out. Now.", None, None, "Get out!", None], str(g)))
        g = run([(900, al("R1", "high")), (905, None), (910, None), (911, None), (930, None)])
        g2 = run([(900, al("R1", "warn", dir="behind")), (901, al("R15", "high"))])
        why = S["pend"].get("lost", {}).get("why")
        g3 = run([(900, al("R11", "warn")), (900.5, al("R1", "high"))])
        res.append(("voice: 'Still in danger. Leave.' once when R1 high lasts 10 s; 'Behind you. Move.'; an urgent line waits only for "
                    "another urgent line and cuts in on a call at once",
                    g == ["Get out. Now.", None, "Still in danger. Leave.", None, None] and g2 == ["Behind you. Move.", None]
                    and why == "urgent gap" and g3 == ["You're too deep. Come back.", "Get out. Now."], f"{g} {g2} {why} {g3}"))
        d10 = dict(lane="mid", tier=2, zone="mid", defend=True, foes=1)  # a hero on the tower (creep damage alone is not spoken)
        g = run([(900, lambda W: (lion(visible=True)(W), al("R10", "warn", **d10)(W))), (902, None), (906, lion(visible=False)), (930, al("R10", None)),
                 (931, al("R10", "warn", **d10))])
        drops = [kw for k, kw in evs if kw.get("op") == "drop"]
        res.append(("voice: fight mode allows urgent lines only: a call waiting in a fight is dropped after 4 s, never said late",
                    g == [None, None, None, None, "Defend mid."] and [(x["cue"], x["reason"]) for x in drops] in ([("defend", "fight")], [("defend", "cooldown")])
                    and S["fight"] is False, f"{g} {drops}"))
        g = run([(900, al("R11", "warn")), (905, al("R10", "warn", **d10)), (910, None), (921, lambda W: (al("R12", "warn")(W), W.__setitem__("hud", {"fight": {"x": 0, "y": 3000}}))), (925, al("R14", "warn")),
                 (960, lambda W: (al("R1", "warn")(W), al("R10", "warn", lane="top", tier=1, zone="off_lane", defend=True, foes=1)(W))), (961, None),
                 (963, al("R1", None)), (966, None), (969, al("R14", "warn")),
                 (970, al("R10", "warn", lane="bot", tier=1, zone="safe_lane", defend=True, foes=1))])
        res.append(("voice: calls: 20 s after the previous non-urgent line, not while R1 is up, not within 8 s of an urgent line, "
                    "no 'go' call within 12 s of a retreat call",
                    g == ["You're too deep. Come back.", None, None, "Your team is fighting. Join them.", None, "Gank coming. Back to tower.",
                          None, None, None, None, "Defend bot."] and S["pend"].get("objective", {}).get("why") == "after retreat", f"{g} {S['pend']}"))
        g = run([(900 + 25 * i, al("R10", "warn", **d10) if i % 2 == 0 else al("R10", None)) for i in range(8)])
        g2 = run([(900, lambda W: (W["settings"].update(verbosity="low"), al("R11", "warn")(W))), (901, al("R1", "high"))])
        S["n_nu"] = CAP["normal"]
        W = copy.deepcopy(base)
        al("R11", "warn")(W)
        capped = (tick(W), S["n_nu"])
        res.append(("voice: budget: the exact same line not within 60 s; verbosity low = urgent only; per-game cap on non-urgent lines",
                    g == ["Defend mid.", None, None, None, "Defend mid.", None, None, None] and g2 == [None, "Get out. Now."]
                    and capped == (None, CAP["normal"]), f"{g} {g2} {capped}"))
        home = lambda dx: lambda W: (W["me"].update(x=W["me"]["x"] + dx, y=W["me"]["y"] + dx), W["_"].setdefault("hist", []).append(  # noqa: E731
            (W["game"]["clock"], W["me"]["x"], W["me"]["y"], 80)))
        ob = len([1 for k, kw in evs if kw.get("op") == "obeyed"])
        g = run([(900, home(0)), (901, home(-300)), (902, lambda W: (home(-300)(W), al("R1", "warn")(W))), (903, home(-300)), (904, home(0)),
                 (905, home(0)), (906, home(0))])
        res.append(("voice: relevance: a retreat call is not spoken to a player who is already leaving; it is when they stop",
                    g == [None, None, None, None, None, None, "Gank coming. Back to tower."]  # 2 s anti-flicker after the last "obeyed"
                    and len([1 for k, kw in evs if kw.get("op") == "obeyed"]) == ob + 1, str(g)))
        base3 = dict(lane="mid", tier=3, zone="base", defend=True)
        g = run([(900, al("R10", "warn", lane="bot", tier=2, zone="safe_lane", defend=False, foes=3)), (903, me(x=3000, y=-6000)), (930, al("R10", None)),
                 (960, lambda W: (me(x=0, y=0)(W), al("R10", "high", **base3)(W))), (990, al("R10", None)), (1020, al("R10", "high", **base3)),
                 (1050, al("R10", None)), (1080, lambda W: (me(x=-6000, y=-5500)(W), al("R10", "high", **base3)(W)))])
        res.append(("voice: defend: LET IT GO has its own line (only said to a player standing there); 'Base is next' once, then "
                    "'Defend the base!'; not to a player already there",
                    g == [None, "Let it go. Stay with your team.", None, "Base is next. Stay with your team.", None, "Defend the base!", None, None], str(g)))

        def dead(W):
            W["me"].update(alive=False)
            queue("cause:alone", "D", D["alone"], 901.5, 912, cause=True)
            queue("buyback", "D", D["bb_no"], 901.5, 912)
        g = run([(900, dead), (901, None), (901.5, None), (902, None), (903.5, None), (920, None)])
        g2 = run([(900, lambda W: (W["settings"].update(verbosity="low"), dead(W))), (902, None), (904, None)])
        res.append(("voice: debrief: queued lines start 1.5 s after the death, one after the other; verbosity low keeps the cause only",
                    g == [None, None, D["alone"], None, D["bb_no"], None] and g2 == [None, D["alone"], None], f"{g} {g2}"))
        brief = lambda W: (W["game"].update(state=PRE_GAME), queue("goal", "B", B["goal_6"], -40, -15), queue("lane", "B", B["lane_support"], -40, -15))  # noqa: E731
        g = run([(-40, brief), (-38, None), (-36, None)])
        g += run([(900, lambda W: (W["settings"].update(voice=False), al("R1", "high")(W)))])
        g += run([(900, lambda W: (W["settings"].update(quiet=True), al("R1", "high")(W)))])
        g += run([(900, lambda W: (W["game"].update(paused=True), al("R1", "high")(W)))])
        res.append(("voice: the brief is spoken before the horn; silent when off / quiet / paused",
                    g == [B["goal_6"], B["lane_support"], None, None, None, None], str(g)))
        _comm_enabled = lambda: True  # noqa: E731
        ready = lambda at: lambda W: S["comm"].update(ready=(None, "Take the bot tower with your team.", at, "x", 0, 2.0))  # noqa: E731
        won = lambda W: (lion(visible=False)(W), W["game"].update(score=[2, 0]))  # noqa: E731
        g = run([(900, None), (990, None), (1080, lion(visible=True)), (1086, None), (1087, won), (1091, None), (1092, ready(1091)),
                 (1200, ready(1190)), (1201, None)])
        res.append(("voice: thinking prompt: no timer (nothing changed = nothing asked); asked when a fight is won, spoken at once; "
                    "dropped when it would start more than 7 s after its trigger",
                    [m for _, m in started] == ["your team just won a fight (2 kills for 0 deaths)"] and started[0][0] == 1091
                    and g == [None, None, None, None, None, None, "Take the bot tower with your team.", None, None]
                    and [kw["reason"] for k, kw in evs if kw.get("op") == "drop" and kw["cue"] == "think"] == ["late"], f"{started} {g}"))
    finally:
        DRY, GEN, ROOT, ORDER, tts, find, secs, s0, env, _comm_enabled, _comm_start, rec.event = saved
        S.clear()
        S.update(s0)
        os.environ.clear()
        os.environ.update(env)
        _reset()
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gen", action="store_true", help="record every fixed line of the bank now, with its takes (cached clips are skipped)")
    ap.add_argument("--force", action="store_true", help="with --gen: re-record bank clips made with an older STYLE instruction")
    ap.add_argument("--list", action="store_true", help="every line of the bank with the length of each recorded take")
    ap.add_argument("--stats", action="store_true", help="library summary from data/voice/library.json")
    ap.add_argument("--say", metavar="TEXT", help="play one line through afplay (generated first if not cached)")
    ap.add_argument("--audition", action="store_true", help="render 'Get out!' + 'Back off. Heal up.' in several voices of the active provider")
    ap.add_argument("--check", action="store_true", help="run selfcheck()")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if a.gen:
        n0, p = _today()["usd"], provider()
        earcon_file("on"), earcon_file("off")  # the push-to-talk blips (local, no TTS)
        for text, tier in bank_lines() if p else []:
            old = [k for k in _takes(text, tier, p) if a.force and S["lib"]["clips"].get(k, {}).get("style") != style_for(tier)]
            for n in sorted({*_missing(text, tier, p), *(int(k.rsplit(".v", 1)[1]) if ".v" in k else 1 for k in old)}):
                key = _gen_one(text, "fixed", n=n, tier=tier)
                print(f"{'OK  ' + key + f' {secs(key):.2f} s' if key else 'FAIL'}  [{tier}] {text}", flush=True)
            if not provider():
                break
        print(f"{len(S['ready'])} clips in the library, this run ~${_today()['usd'] - n0:.4f}; blocked: {S['blocked'] or '-'}")
    if a.list:
        p, tot = provider() or ORDER[0], 0.0
        for text, tier in bank_lines():
            ks = sorted(_takes(text, tier, p))
            tot += sum(S["lib"]["clips"].get(k, {}).get("usd", 0) for k in ks)
            print(f"[{tier}] {text}  " + (" / ".join(f"{secs(k):.2f} s" for k in ks) or "NOT RECORDED") + f"  ({len(ks)}/{VARIANTS.get(tier, 1)})")
        print(f"{len(bank_lines())} lines in {bank(p)}, ${tot:.4f}")
    if a.audition:
        p = provider()
        for v in PROVIDERS[p]["audition"] if p else []:
            keys = [_gen_one(t, "audition", p, v) for t in ("Get out!", "Back off. Heal up.")]
            print(f"{p} {v}: {ROOT / keys[0]}.wav" if all(keys) else f"{p} {v}: FAILED {S['blocked']}")
        print(f"pick one: VOICE_{(p or '?').upper()}_VOICE=<voice> uv run voice.py --gen   (afplay -v 0.3 <wav> to listen)")
    if a.say:
        key = find(a.say) or _gen_one(a.say, "dynamic")
        if not key:
            sys.exit(f"no TTS clip ({S['blocked'] or 'no provider key'}); nothing played")
        print(f"played {key} via {play(a.say, 9)[0]}")
        S["proc"] and S["proc"].wait()
    if a.stats:
        clips = S["lib"]["clips"]
        by = {}
        for k, e in clips.items():
            by.setdefault(k.split("/")[0], []).append(e)
        print(f"{len(clips)} clips in {ROOT / 'library.json'}; generating with {provider() or 'nothing (no key)'} (order {ORDER})")
        for b, es in sorted(by.items()):
            src = {s: sum(1 for e in es if e.get("source") == s) for s in ("fixed", "template", "dynamic", "audition")}
            print(f"  {b}: {len(es)} clips {src}, {sum(e.get('uses', 0) for e in es)} plays, {sum(1 for e in es if not e.get('uses'))} never played,"
                  f" ${sum(e.get('usd', 0) for e in es):.4f}")
        for k, e in sorted(clips.items(), key=lambda kv: -kv[1].get("uses", 0))[:10]:
            print(f"  {e.get('uses', 0):4d}x  {e['text']}  ({k}, last {e.get('last_used') or '-'})")
        for day, v in sorted(S["lib"]["daily"].items())[-7:]:
            print(f"  {day}: {v['n']} requests, ${v['usd']:.4f} (cap {DAILY}/day)")
    if a.check:
        r = selfcheck()
        for n, ok, d in r:
            print(f"{'PASS' if ok else 'FAIL'}  {n}" + (f"  [{d}]" if d else ""))
        sys.exit(any(not ok for _, ok, _ in r))


if __name__ == "__main__":
    main()
