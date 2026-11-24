"""Talk to the coach. Default: PUSH-TO-TALK (hold W.settings.ptt_key, default mouse4 = the back side button) -> free on-device macOS
speech-to-text -> one LLM call -> short spoken reply (voice TTS). Optional W.settings.mic_mode "always" (utterance = 1.2 s pause)
or "off"; W.settings.mic false = mute everything. No realtime voice API.
The hotkey is read by listen_helper.swift through a LISTEN-ONLY event tap: this app never posts, injects or presses any key or button.
Cloud fallback only when on-device recognition is unavailable (e.g. Siri & Dictation off): gpt-4o-transcribe on the recorded
press / VAD chunk (never silence, never while muted).
Dev mode (TALK_DEV=1, default): EVERY utterance goes to the LLM and is logged as events kind "voice_cmd" (text, intent, emotion,
response, settings_changed, ms, usd) to train a cheap keyword -> intent matcher later.
tick(W) runs in app.py's tick loop under world.lock; the helper reader, LLM and TTS run on threads and hand results to tick."""
import collections, difflib, json, logging, os, queue, re, subprocess, threading, time, urllib.request, uuid
from pathlib import Path
import llm, rec, voice, world

log = logging.getLogger("coach")
E = os.environ.get
ROOT = Path(__file__).parent
SRC, BIN = ROOT / "listen_helper.swift", ROOT / "data/listen-helper"  # fixed path: a rebuild = new code hash = macOS asks again
DEV = E("TALK_DEV", "1") == "1"
MAX_USD = float(E("TALK_MAX_USD_PER_GAME", 2.0 if DEV else 0.30))  # LLM + reply TTS + fallback transcription; then keywords only
VENT_GAP, STALE_S, DONE_S = 45, 12, 6  # venting replies <= 1 per 45 s; a reply older than 12 s is dropped; "done" shown 6 s
STT_PRICE = 0.006  # $/min gpt-4o-transcribe (fallback only; the mini model misheard too much, player feedback 2026-11-12)
MERGE_S = 2.5  # open mic: a thinking pause splits one thought into several chunks; finals this close together are one utterance
MODES, DEFAULT_KEY = ("ptt", "always", "off"), "mouse4"
DEFAULTS = {"verbosity": "normal", "commentary": True, "voice": True, "mic": True, "quiet": False}  # effective value of an unset setting
WORDS = ("coach,Roshan,Aegis,Tormentor,smoke,gank,BKB,Black King Bar,blink,buyback,ward,sentry,dust,gem,TP,mid,top,bot,safe lane,"
         "offlane,jungle,stack,pull,rotate,push,tower,rax,high ground,pos 1,pos 2,pos 3,pos 4,pos 5,ult,glimmer,Linken's")
PLIST = """<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>local.live-coach.listen</string><key>CFBundleName</key><string>live-coach listen</string>
<key>NSMicrophoneUsageDescription</key><string>The Dota coach records while you hold the push-to-talk key, so you can talk to it.</string>
<key>NSSpeechRecognitionUsageDescription</key><string>The Dota coach turns your speech into text on this Mac.</string></dict></plist>"""
# macOS virtual key codes (kVK_*) and CGEventFlags masks; mouse3 = middle, mouse4/5 = side buttons (CG button numbers 2/3/4)
KEYCODES = {**{f"f{i}": c for i, c in enumerate([122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90], 1)},
            **dict(zip("asdfhgzxcv", range(10))), **dict(zip("bqweryt", range(11, 18))), "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
            "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
            "space": 49, "tab": 48, "`": 50, "grave": 50, "capslock": 57}
MOUSE = {"mouse3": 2, "mouse4": 3, "mouse5": 4}
MODS = {"ctrl": 0x40000, "control": 0x40000, "alt": 0x80000, "option": 0x80000, "opt": 0x80000, "shift": 0x20000, "cmd": 0x100000}
SYSTEM = """You are the player's Dota 2 coach on voice comms. They talk to you by push-to-talk (or an open mic): commands, questions,
info about the game, or thinking out loud and venting. Understand what they mean and how they feel. Output JSON for the schema.
reply: spoken aloud. <= 20 words, natural spoken English with contractions, warm coach-on-comms tone, specific to the game state.
Never parrot their words back. Never tell them to type in chat or ping. Never name the decision model ("jev") or quote percentages.
- question, request_advice, command_*: always reply (answer briefly; confirm a settings change in 2-5 words).
- facts about items, abilities or mechanics: only from ITEM FACTS or the game snapshot; if they don't cover it, say you're not sure. Never guess.
- info (they tell you something): a short ack only if it adds value (what it means, what to do now), else "".
- venting or thinking aloud: usually ""; reply only when a short steadying or useful line clearly helps AND vent_ok is yes.
- frustrated or tilted: short, steadying and specific, e.g. "Breathe. One fight at a time. Farm bot safe for two minutes."
- excited: match the energy briefly, then the next objective. Confused: one clear instruction.
- not talking to you (teammates, background, fragments): intent other, reply "".
intent: question (asks something) | info (tells you a game fact) | command_settings (talk less/more, commentary, voice, role, turbo) |
command_mute (stop listening / mute the mic) | venting | request_advice ("what do I do", "what should I buy") |
app_feedback (they critique or suggest changes to YOU, the coach app: wrong calls, "you said X is dead but he wasn't", "too many alerts",
"the item suggestion is bad", "you should tell me when...") -> reply a 3-6 word acknowledgement like "Noted, I'll pass that on." | other.
settings: only what they ask for, else null. verbosity low = only urgent calls, no commentary; normal; high = more commentary.
commentary false/true when they ask for no/more commentary. voice false only if they want you completely silent. mic false only
for command_mute. role 1-5 only if they state their position. turbo true/false only if they say this game is (not) Turbo. note: a game fact they told you, short, third person ("enemy Lion has
blink", "player going Roshan", "player is pos 4"), "" if none; never feelings. focus: what they want you to focus on ("call mid
rotations"), "" if nothing new. emotion: their tone."""
INTENTS = ["question", "info", "command_settings", "command_mute", "venting", "request_advice", "app_feedback", "other"]
NUL = lambda t: {"type": [t, "null"]}  # noqa: E731
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["reply", "settings", "note", "focus", "emotion", "intent"],
          "properties": {"reply": {"type": "string"},
                         "settings": {"type": "object", "additionalProperties": False,
                                      "required": ["verbosity", "commentary", "voice", "mic", "role", "turbo"],
                                      "properties": {"verbosity": {"type": ["string", "null"], "enum": ["low", "normal", "high", None]},
                                                     "commentary": NUL("boolean"), "voice": NUL("boolean"), "mic": NUL("boolean"),
                                                     "role": NUL("integer"), "turbo": NUL("boolean")}},
                         "note": {"type": "string"}, "focus": {"type": "string"},
                         "emotion": {"type": "string", "enum": ["calm", "frustrated", "tilted", "excited", "confused", "tired"]},
                         "intent": {"type": "string", "enum": INTENTS}}}
FAST = [  # (regex on the normalized text, action, value, any length?); first hit wins. Applied at once, no LLM needed.
    (r"\b(coach (mute|stop listening)|stop listening|mute( the)? mic|mic off)\b", "mic", False, True),
    (r"\b(repeat( that)?|say (that|it) again|come again|what did you say)\b", "repeat", None, False),
    (r"\b(not (a )?turbo|normal (game|mode)|turbo off)\b", "turbo", False, True),
    (r"\b(turbo (game|mode|match|on)|(it'?s|this is|playing) (a )?turbo)\b", "turbo", True, True),
    (r"\b(bad call|wrong call|that was wrong|that s wrong)\b", "ease", None, False),  # eases the last spoken rule for 5 minutes
    (r"^(coach )?why( was that| that call| did you (say|call) that)?$", "why", None, False),  # the reason for the last call
    (r"\b(talk again|you can talk|unmute yourself|speak again)\b", "quiet", False, False),
    (r"\b(talk more|more (talking|commentary|info))\b", "verbosity", "high", False),
    (r"\b(less talking|talk less|too much talking)\b", "verbosity", "low", False),
    (r"\b(quiet|stop talking|shut up|silence)\b", "quiet", True, False),  # nothing but replies until the next match
]
CONFIRM = {("turbo", True): voice.TALK["turbo"], ("turbo", False): voice.TALK["normal"], ("mic", False): voice.TALK["mic_off"],
           ("verbosity", "low"): voice.TALK["low"], ("verbosity", "high"): voice.TALK["high"], ("quiet", True): voice.TALK["quiet"],
           ("quiet", False): voice.TALK["back"]}  # all recorded in the voice bank: a confirmation plays at once
EASE_S = 300
Q = queue.Queue()  # (kind, payload) from helper / LLM / TTS threads -> tick()
H = {"proc": None, "state": "off", "err": None, "engine": None, "tap": None, "im": None, "partial": "", "paused": False, "idle_since": 0.0,
     "warned": set(), "want": False, "sent": None}
T = {}  # per-match conversation + pipeline stage


def _reset(mid=None):
    T.clear()
    T.update(match=mid, turns=collections.deque(maxlen=10), usd=0.0, n=0, busy=False, pending=[], heard=None, reply=None,
             last_vent=-1e9, emotions=collections.deque(maxlen=5), focus="", ready=None, live_n=0, stage="idle", stage_at=0.0,
             t_up=None, tts_wait=False, speak_proc=None)


_reset()


def parse_key(s):
    """'mouse4' | 'f13' | 'ctrl+f' | 'alt+`' -> helper spec 'key|mouse CODE MODS', or None if unknown."""
    *mods, k = [p.strip() for p in str(s or "").lower().split("+")]
    if any(m not in MODS for m in mods):
        return None
    m = sum({MODS[x] for x in mods})
    return f"mouse {MOUSE[k]} {m}" if k in MOUSE else f"key {KEYCODES[k]} {m}" if k in KEYCODES else None


def mode_of(s):
    return "off" if s.get("mic") is False else s.get("mic_mode") if s.get("mic_mode") in MODES else "ptt"


def item_facts(text):
    """Dota's own item data for every item the player named (stats, actives, notes such as "range bonuses do not stack").
    The talk model said Hydra's Breath and Hurricane Pike stack range; the item notes say they don't (2026-11-13)."""
    import data
    t, out = norm(text), []
    for k, it in (data.S.get("items") or {}).items():
        n = norm(str(it.get("dname") or ""))
        if n and not k.startswith("recipe") and re.search(rf"\b{re.escape(n)}\b", t):
            attrib = ", ".join(str(a.get("display") or a.get("key", "")).replace("{value}", str(a.get("value", ""))) for a in it.get("attrib") or [])
            abil = " ".join(f"{a.get('title')}: {a.get('description')}" for a in it.get("abilities") or [])
            out.append(f"{it['dname']} ({it.get('cost')}g, built from {', '.join(it.get('components') or []) or '-'}): {attrib}. {abil} "
                       f"Notes: {it.get('notes') or '-'}")
    return ("\nITEM FACTS (Dota's own data; answer item questions from these only, and say you're not sure when they don't cover it):\n"
            + "\n".join(x[:900] for x in out[:4])) if out else ""


def live(W):
    """A match is on (draft to the end screen) and GSI is fresh, or Dota shows "Finding match" / the ready check (menu.py OCR).
    The open mic listens only then; push-to-talk always works."""
    return (W.get("menu") or {}).get("state") in ("searching", "found") or W["game"].get("state") not in (None, "DOTA_GAMERULES_STATE_POST_GAME", "DOTA_GAMERULES_STATE_DISCONNECT") \
        and (W.get("src_age") or {}).get("gsi_s") is not None and W["src_age"]["gsi_s"] < 30


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("'", " "))).strip()


def fast(text):
    """Keyword fast-path -> (action, value) or None."""
    t = norm(text)
    return next(((act, val) for rx, act, val, anylen in FAST if (anylen or len(t.split()) <= 6) and re.search(rx, t)), None)


def is_echo(text, spoken):
    """True if `text` is (mostly) one of the coach's own recent lines picked up by the mic (open-mic mode)."""
    t = norm(text)
    return bool(t) and any(t in s or difflib.SequenceMatcher(None, t, s).ratio() >= 0.6 for s in map(norm, spoken) if s)


def _stage(s):
    T["stage"], T["stage_at"] = s, time.time()


# ---- helper process -----------------------------------------------------------------------------------------------------
def _build():
    """Compile the helper once per source change (content hash next to it). Returns the binary path or raises."""
    import hashlib
    h = hashlib.sha1(SRC.read_bytes() + PLIST.encode()).hexdigest()[:12]
    stamp = BIN.with_suffix(".sha")
    if BIN.exists() and stamp.exists() and stamp.read_text() == h:
        return BIN
    BIN.parent.mkdir(parents=True, exist_ok=True)
    pl, tmp = BIN.with_suffix(".plist"), BIN.with_suffix(".tmp")
    pl.write_text(PLIST)
    subprocess.run(["swiftc", "-O", "-swift-version", "5", str(SRC), "-o", str(tmp), "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist", "-Xlinker", str(pl)], check=True, capture_output=True, timeout=300)
    subprocess.run(["codesign", "-s", "-", "-f", "-i", "local.live-coach.listen", str(tmp)], check=True, capture_output=True)
    tmp.replace(BIN)
    stamp.write_text(h)
    log.info("listen: built %s", BIN)
    return BIN


def _warn(key, msg, *a):
    if key not in H["warned"]:
        H["warned"].add(key)
        log.warning(msg, *a)


def _run(extra=()):
    """Keep one helper alive; read its JSON lines into Q. Retries every 30 s (60 s after a denial)."""
    while H["want"]:
        try:
            with world.lock:
                s = dict(world.W.get("settings") or {})
            p = H["proc"] = subprocess.Popen([str(_build()), "--words", WORDS, "--chunk-dir", str(BIN.parent), "--mode", mode_of(s),
                                              "--key", parse_key(s.get("ptt_key")) or parse_key(DEFAULT_KEY), *extra],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            H.update(state="starting", err=None, paused=False, sent=None)
            for line in p.stdout:
                try:
                    Q.put(("helper", json.loads(line)))
                except ValueError:
                    pass
            p.wait()
            if H["state"] != "denied":
                H.update(state="off", err=f"helper exited ({p.returncode})")
                _warn(f"exit{p.returncode}", "listen: helper exited (%s); retrying every 30 s", p.returncode)
        except Exception as e:
            H.update(state="off", err=f"helper unavailable: {e!r}"[:200])
            _warn("build", "listen: speech helper unavailable (%r); voice input off, retrying every 30 s", e)
        H["proc"] = None
        for _ in range(60 if H["state"] == "denied" else 30):
            if not H["want"]:
                return
            time.sleep(1)


def start(extra=()):
    """Start the helper in the background (app.py lifespan). Never raises, never blocks."""
    if not H["want"]:
        H["want"] = True
        threading.Thread(target=_run, args=(tuple(extra),), daemon=True, name="listen").start()


def stop():
    H["want"] = False
    p = H["proc"]
    if p:
        try:
            p.stdin.close()  # helper exits on EOF
            p.terminate()
        except Exception:
            pass


def _send(cmd):
    p = H["proc"]
    try:
        if p and p.poll() is None:
            p.stdin.write(cmd + "\n")
            p.stdin.flush()
    except (OSError, ValueError):
        pass


# ---- cloud fallback (only when on-device recognition is unavailable) ----------------------------------------------------
def _transcribe(path, dur):
    """gpt-4o-transcribe on one recording -> Q ("final", (text, usd)). The file is deleted either way."""
    text, usd = "", 0.0
    try:
        key, b = E("OPENAI_API_KEY"), uuid.uuid4().hex
        body = (f"--{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{E('TALK_STT_MODEL', 'gpt-4o-transcribe')}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nDota 2 player talking to their coach: {WORDS}\r\n"
                f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() \
            + Path(path).read_bytes() + f"\r\n--{b}--\r\n".encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", body,
                                     {"Content-Type": f"multipart/form-data; boundary={b}", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text, usd = json.load(r).get("text") or "", dur / 60 * STT_PRICE
        hint = set(re.findall(r"[a-z0-9']+", WORDS.lower())) | {"dota", "player", "talking", "coach", "chatgpt"}
        words = re.findall(r"[a-z0-9']+", text.lower())
        if words and (all(w in hint for w in words) or sum(w in hint for w in words) / len(words) > 0.6 and len(words) > 8):
            log.info("listen: dropped a prompt echo from the transcriber (silent/noisy press)")
            text = ""  # near-silent audio: the model returns its own vocabulary hint
    except Exception as e:
        _warn("stt", "listen: cloud transcription failed (%s)", llm.redact(repr(e))[:200])
    finally:
        Path(path).unlink(missing_ok=True)
        Q.put(("final", (text, usd)))


# ---- speaking -----------------------------------------------------------------------------------------------------------
def _spoken():
    S = voice.S
    return [*(S.get("said") or []), *((S.get("comm") or {}).get("last") or []), *[t["coach"] for t in T["turns"] if t.get("coach")],
            (S.get("spoken") or {}).get("text") or ""]


def _playing():
    p = voice.S.get("proc")
    return bool(p and p.poll() is None)


def say(text, W):
    """Speak a reply: a cached library clip if there is one, else live TTS (voice.tts, the commentary path) on a thread."""
    if W["settings"].get("voice") is False or not text:
        return
    _stage("speaking")
    if voice.DRY or voice.find(text):
        T["ready"] = (None if voice.DRY else "clip", text, 0.0, time.time())
        return
    p = voice.provider()
    if not p:
        return
    T["live_n"] += 1
    T["tts_wait"] = True
    f, mid = voice.ROOT / ".live" / f"talk{T['live_n'] % 3}.wav", T["match"]

    def run():
        try:
            wav, usd = voice.tts(text, p, voice.PROVIDERS[p]["voice"], getattr(voice, "COMM_STYLE", None))
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(wav)
            Q.put(("ready", (f, text, usd, time.time(), mid)))
        except Exception as e:
            log.warning("listen: reply TTS failed (%s)", llm.redact(repr(e))[:200])
            Q.put(("ready", (None, None, 0.0, 0.0, mid)))
    threading.Thread(target=run, daemon=True).start()


def _play_ready(W):
    r = T["ready"]
    if not r or _playing():
        return
    T["ready"] = None
    kind, text, _, at = r
    if not text or time.time() - at > STALE_S:
        return
    c = W["game"]["clock"]
    if kind == "clip":
        voice.play([text], 2.6, "talk")
        T["speak_proc"] = voice.S.get("proc")
    elif kind is not None and not voice.DRY:
        if not voice._afplay(kind, 2.6):
            return
        T["speak_proc"] = voice.S["proc"]
    voice.S.update(prio=2.6, spoken={"text": text, "cue": "talk", "at_clock": c})  # urgent cues (>= 3) still cut in
    if c is not None:
        voice.S["last_c"] = c
    log.info("talk %s: coach: %s (%s)", world.mmss(c), text, "dry" if kind is None or voice.DRY else "live")


# ---- utterances ---------------------------------------------------------------------------------------------------------
def _setting(W, k, v):
    """Apply one setting if it changes something; returns {k: v} or {}."""
    cur = W["settings"].get(k)
    if k == "turbo":  # compare with the effective mode, and always pin it to this match (manual override)
        if v is None or (v == world.turbo() and W["settings"].get("turbo_match") == W["game"]["matchid"]):
            return {}
        world.set_settings(turbo=bool(v))
        return {k: v}
    if v is None or v == (DEFAULTS.get(k) if cur is None else cur) or (k == "role" and v not in range(1, 6)):
        return {}
    world.set_settings(**{k: v})
    return {k: v}


def _blip(W, kind):
    """Push-to-talk earcon (key down / up): a local wav at low volume, its own afplay so it never cuts a coach line."""
    s = W["settings"]
    if voice.DRY or s.get("voice") is False or mode_of(s) != "ptt":
        return
    try:
        v = s.get("volume")
        subprocess.Popen(["afplay", "-v", str(round(0.3 * voice.VOLUME * (v if isinstance(v, (int, float)) else 1.0), 3)),
                          str(voice.earcon_file(kind))], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        _warn("blip", "listen: earcon failed (%r)", e)


def _fast(W, text, act, val):
    if act == "repeat":
        if last := (voice.S.get("spoken") or {}).get("text"):
            say(last, W)
        return {}
    calls = voice.S.get("calls") or []
    if act == "ease":  # "bad call": that rule is shown amber and not spoken for 5 minutes (rules.alert reads W['_']['eased'])
        last = next((k for k in reversed(calls) if k.get("rule")), None)
        if not last:
            return {}
        W["_"].setdefault("eased", {})[last["rule"]] = (W["game"]["clock"] or 0) + EASE_S
        if rec.on():
            rec.mark(fb="bad", text=text[:200], ref=last["rule"])  # counts against the rule in the post-game scorecard
        say(voice.TALK["ease"], W)
        return {"eased": last["rule"]}
    if act == "why":
        last = next((k for k in reversed(calls) if k.get("why")), None)
        if last:
            say(voice.TALK[f"why_{last['why']}"], W)
        return {}
    changed = _setting(W, act, val)
    if (act, val) in CONFIRM:
        say(CONFIRM[(act, val)], W)
    return changed


def _heard(W, text, usd=0.0):
    """One finished utterance (on-device final, or the cloud transcript)."""
    T["usd"] += usd
    t_final, text = time.time(), text.strip()
    stt_ms = round((t_final - T["t_up"]) * 1000) if T["t_up"] else None
    T["t_up"] = None
    if not text:
        T["heard"], T["reply"] = "(didn't catch that)", None
        if mode_of(W["settings"]) == "ptt":  # the player held the key and got nothing: say so (an open mic would say it all game)
            T["reply"] = voice.TALK["empty"]
            say(T["reply"], W)
        return T["stage"] == "speaking" or _stage("done")
    if mode_of(W["settings"]) == "always" and is_echo(text, _spoken()):
        return rec.event("voice_cmd", op="echo", text=text)
    T["heard"], T["reply"] = text, None
    hit = fast(text)
    changed = _fast(W, text, *hit) if hit else {}
    log.info("talk %s: you: %s%s", world.mmss(W["game"]["clock"]), text, f" -> {hit}" if hit else "")
    if (hit and not DEV) or not llm.TARGETS or llm.DISABLED or llm.PAUSED or T["usd"] >= MAX_USD:
        if not hit:
            _warn("nollm", "listen: LLM off or talk budget used up; only keyword commands work")
        rec.event("voice_cmd", text=text, intent=None, emotion=None, response="", settings_changed=changed, fast=hit and hit[0],
                  ms=0, stt_ms=stt_ms, usd=0.0, engine=H["engine"])
        return T["stage"] == "speaking" or _stage("done")
    T["pending"].append((text, hit, changed, stt_ms, t_final))
    if T["stage"] != "speaking":
        _stage("thinking")
    _ask(W)


def _ask(W):
    if T["busy"] or not T["pending"]:
        return
    batch, T["pending"] = T["pending"], []
    text = " ".join(b[0] for b in batch)
    hit, changed, stt_ms, t0 = next((b[1] for b in batch if b[1]), None), {k: v for b in batch for k, v in b[2].items()}, batch[0][3], batch[0][4]
    c, s, me = W["game"]["clock"], W["settings"], W["me"]
    conv = "\n".join(f"[{world.mmss(t['clock'])}] player: {t['you']}\n  coach: {t['coach'] or '(silent)'}" for t in T["turns"]) or "-"
    user = (f"Conversation this match (oldest first):\n{conv}\nPlayer just said: \"{text}\"\n"
            + (f"(Already handled by a keyword command: {hit[0]}={hit[1]}. Label intent/emotion only; reply \"\", settings all null.)\n" if hit else "")
            + ("" if W["game"].get("state") == "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS" else
               f"MATCH STATUS: not in a running game ({W['game'].get('state') or 'menu / no game'}). No in-game calls (Roshan, ganks, "
               "lanes, items to buy now); answer about the draft, the plan or the last game instead.\n")
            + f"Game: {world.to_text()}\nCoach's recent spoken lines: {' | '.join(_spoken()[-5:]) or '-'}\n"
            f"Settings: verbosity={s.get('verbosity') or 'normal'} commentary={s.get('commentary') is not False} voice={s.get('voice') is not False}\n"
            f"Player mood (last turns): {', '.join(T['emotions']) or '-'}. vent_ok: {'yes' if time.time() - T['last_vent'] >= VENT_GAP else 'no'}"
            + item_facts(text))
    system = f"{SYSTEM}\nPlayer's hero: {me['hero'] or '?'} (role {me['role'] or '?'})."
    T.update(busy=True, n=T["n"] + 1)
    mid = T["match"]

    def run():
        out, usd, model = None, 0.0, None
        for t in llm.TARGETS:
            try:
                o, u, _ = llm._post(t, system, user, 1200, SCHEMA, 10)
                usd += u
                if not (err := llm.validate(o, SCHEMA)):
                    out, model = o, t["model"]
                    break
                log.warning("listen %s: bad output: %s", t["model"], err)
            except Exception as e:
                log.warning("listen %s: %s", t["model"], llm.redact(repr(e))[:200])
        Q.put(("llm", dict(text=text, out=out, usd=usd, model=model, c=c, mid=mid, hit=hit, changed=changed, stt_ms=stt_ms, t0=t0)))
    threading.Thread(target=run, daemon=True).start()


def apply(W, text, out, usd=0.0, c=None, hit=None, changed=None, stt_ms=None, t0=None, model=None):
    """Apply one LLM answer: settings, note, focus, mood, reply (venting rate-limited). Returns the reply actually spoken ('' = none).
    hit = a keyword command already applied for this utterance: then only intent/emotion are taken from the answer."""
    T["usd"] += usd
    changed, ok, out = dict(changed or {}), bool(out), out or {}
    if not hit:
        for k, v in (out.get("settings") or {}).items():
            changed |= _setting(W, k, v)
    note, focus = (out.get("note") or "").strip()[:120], (out.get("focus") or "").strip()[:120]
    emo, intent = out.get("emotion"), out.get("intent")
    if note and norm(note) not in {norm(n["text"]) for n in W.get("player_notes") or []}:
        W["player_notes"] = ((W.get("player_notes") or []) + [{"clock": c, "text": note}])[-8:]
    if focus:
        T["focus"] = focus
    if emo:
        T["emotions"].append(emo)
    reply = "" if hit else " ".join(str(out.get("reply") or "").split()[:25])
    if intent in ("venting", "other") and reply:
        if time.time() - T["last_vent"] < VENT_GAP:
            reply = ""
        else:
            T["last_vent"] = time.time()
    if reply:
        T["reply"] = reply
        say(reply, W)
    elif T["stage"] == "thinking":
        _stage("done")
    T["turns"].append({"clock": c, "you": text, "coach": reply})
    ms = round((time.time() - t0) * 1000) if t0 else None
    log.info("talk %s: coach: %s [%s/%s, %s ms]%s%s", world.mmss(c), reply or "-", intent, emo, ms,
             f" settings {changed}" if changed else "", f" note: {note}" if note else "")
    rec.event("voice_cmd", text=text, intent=intent, emotion=emo, response=reply, settings_changed=changed, fast=hit and hit[0], note=note,
              focus=focus, ms=ms, stt_ms=stt_ms, usd=round(usd, 6), model=model, ok=ok, engine=H["engine"])
    if intent == "app_feedback":  # durable dev backlog, survives compaction: data/profile/app_feedback.jsonl
        try:
            p = world.DATA / "profile" / "app_feedback.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps({"wall": time.strftime("%Y-%m-%d %H:%M:%S"), "matchid": W["game"]["matchid"], "clock": c,
                                    "text": text, "hero": W["me"]["hero"], "attention": (W.get("attention") or {}).get("text"),
                                    "alerts": [a["text"] for a in W.get("alerts") or []][:5]}) + "\n")
        except OSError as e:
            log.warning("app_feedback write failed: %s", e)
    return reply


def tick(W):
    """Every tick under world.lock: drain thread results, sync mode/key to the helper, echo pause, play replies, publish W['talk']."""
    if T["match"] != W["game"]["matchid"]:
        _reset(W["game"]["matchid"])
    while True:
        try:
            kind, x = Q.get_nowait()
        except queue.Empty:
            break
        if kind == "helper":
            _helper_msg(W, x)
        elif kind == "final" and mode_of(W["settings"]) == "always" and not live(W):
            pass  # open mic outside a live match: room talk, not for the coach (it answered 17 min of household chat, 2026-11-12)
        elif kind == "final" and mode_of(W["settings"]) == "always" and x[0]:  # hold it: the player may only be pausing
            p = T.get("pend") or ["", 0.0, 0.0]
            T["pend"] = [(p[0] + " " + x[0]).strip(), p[1] + x[1], time.time()]
        elif kind == "final":
            _heard(W, *x)
        elif kind == "llm":
            T["busy"] = False
            if x.pop("mid") == T["match"]:
                apply(W, **x)
            _ask(W)
        elif kind == "ready" and x[4] == T["match"]:
            T["usd"] += x[2]
            T["tts_wait"] = False
            T["ready"] = (x[0], x[1], x[2], x[3])
    if (p := T.get("pend")) and time.time() - p[2] >= MERGE_S:
        T["pend"] = None
        _heard(W, p[0], p[1])
    _play_ready(W)
    now, s = time.time(), W["settings"]
    if T["stage"] == "speaking" and not T["tts_wait"] and not T["ready"] and not (T["speak_proc"] and T["speak_proc"].poll() is None):
        _stage("done")
    if T["stage"] == "done" and now - T["stage_at"] > DONE_S:
        _stage("idle")
    mode, key = mode_of(s), parse_key(s.get("ptt_key")) or parse_key(DEFAULT_KEY)
    if H["state"] == "on" and H["sent"] != (mode, key):
        _send(f"mode {mode}")
        _send(f"key {key}")
        H["sent"] = (mode, key)
    if mode == "always" and (_playing() or not live(W)):  # open mic: not the coach's own voice, and only during a match
        H["idle_since"] = 0.0
        if not H["paused"]:
            H["paused"] = True
            _send("pause")
    elif H["paused"]:
        H["idle_since"] = H["idle_since"] or now
        if now - H["idle_since"] >= 0.6 or mode != "always":  # room echo tail
            H["paused"] = False
            _send("resume")
    mood = collections.Counter(T["emotions"]).most_common(1)
    W["talk"] = {"mic": s.get("mic") is not False, "mode": mode, "key": s.get("ptt_key") or DEFAULT_KEY, "state": H["state"], "err": H["err"],
                 "engine": H["engine"], "tap": H["tap"], "input_monitoring": H["im"],
                 "listening": H["state"] == "on" and mode == "always" and not H["paused"], "stage": T["stage"],
                 "stage_age_s": round(now - T["stage_at"], 1) if T["stage_at"] else None, "partial": H["partial"] if mode != "off" else "",
                 "heard": T["heard"], "reply": T["reply"], "turns": T["n"], "busy": T["busy"], "usd": round(T["usd"], 5), "dev": DEV,
                 "emotion": T["emotions"][-1] if T["emotions"] else None, "mood": mood[0][0] if mood else None, "focus": T["focus"]}


def _helper_msg(W, m):
    if m.get("ptt") == "down":
        H["partial"], T["heard"], T["reply"] = "", None, None
        _stage("recording")
        _blip(W, "on")
    elif m.get("ptt") == "up":
        T["t_up"] = time.time()
        _stage("transcribing")
        _blip(W, "off")
    elif "partial" in m:
        H["partial"] = m["partial"][-160:]
        if T["stage"] in ("idle", "done"):  # open mic: speech started
            _stage("recording")
    elif "final" in m:
        H["partial"] = ""
        if T["stage"] == "recording":  # open mic: utterance ended
            T["t_up"] = time.time()
        _heard(W, m["final"])
    elif "chunk" in m:
        if mode_of(W["settings"]) == "off" or not E("OPENAI_API_KEY") or T["usd"] >= MAX_USD:
            Path(m["chunk"]).unlink(missing_ok=True)  # muted / no key / over budget: the audio never leaves the machine
            _warn("nokey", "listen: on-device speech unavailable and no OpenAI key/budget: voice input can't be transcribed")
            if T["stage"] == "transcribing":
                _stage("done")
        else:
            T["t_up"] = T["t_up"] or time.time()
            _stage("transcribing")
            threading.Thread(target=_transcribe, args=(m["chunk"], float(m.get("dur") or 0)), daemon=True).start()
    elif m.get("status") == "ready":
        H.update(state="on", err=None, engine="on-device" if m.get("ondevice") else "cloud", sent=None)
        H["tap"], H["im"] = m.get("tap", H["tap"]), m.get("input_monitoring", H["im"])
        if m.get("reason") == "dictation disabled":
            _warn("dictation", "listen: Siri & Dictation is off, so on-device speech-to-text is unavailable; using gpt-4o-transcribe. "
                  "For free on-device STT turn on System Settings > Keyboard > Dictation, then restart the coach.")
        if "tap" in m:
            log.info("listen: voice input ready (%s STT, mode %s, push-to-talk %s)", H["engine"], mode_of(W["settings"]),
                     W["settings"].get("ptt_key") or DEFAULT_KEY)
            if not m.get("tap") or not m.get("input_monitoring"):
                _warn("im", "listen: push-to-talk hotkey needs Input Monitoring: System Settings > Privacy & Security > Input Monitoring > "
                      "enable 'listen-helper', then restart the coach (mouse side buttons may work without it).")
    elif m.get("status") == "denied":
        what = {"mic": "Microphone", "speech": "Speech Recognition"}.get(m.get("what"), m.get("what"))
        H.update(state="denied", err=f"{what} permission denied")
        _warn(f"denied-{what}", "listen: %s permission denied. Allow 'listen-helper' in System Settings > Privacy & Security > %s "
              "(re-checked every 60 s).", what, what)
    elif m.get("status") == "error":
        H["err"] = str(m.get("error"))[:200]
        _warn("err" + H["err"][:20], "listen: helper error: %s", H["err"])


# ---- self-check ---------------------------------------------------------------------------------------------------------
def selfcheck():
    """[(name, ok, detail)]: hotkey parsing, keyword fast-path, echo suppression, LLM answer application + stages (mock).
    No mic, no network, no audio."""
    import copy
    saved = (voice.DRY, copy.copy(T), world.set_settings, rec.event)
    res, calls, evs = [], [], []
    voice.DRY = True
    try:
        keys = [parse_key(k) for k in ("mouse4", "F13", "ctrl+f", "alt+`", "cmd+shift+space", "hyper+x", "f21", "")]
        res.append(("listen: push-to-talk key parsing", keys == ["mouse 3 0", "key 105 0", f"key 3 {0x40000}", f"key 50 {0x80000}",
                                                                 f"key 49 {0x120000}", None, None, None], str(keys)))
        modes = [mode_of(s) for s in ({}, {"mic_mode": "always"}, {"mic_mode": "always", "mic": False}, {"mic_mode": "bogus"})]
        res.append(("listen: mic mode (default ptt, mute wins)", modes == ["ptt", "always", "off", "ptt"], str(modes)))
        cases = [("coach mute", ("mic", False)), ("okay stop listening for now", ("mic", False)), ("Quiet please", ("quiet", True)),
                 ("less talking", ("verbosity", "low")), ("talk more", ("verbosity", "high")), ("repeat that", ("repeat", None)),
                 ("the map is quiet so I'm going to farm the bottom jungle now", None), ("where is their carry", None),
                 ("Bad call.", ("ease", None)), ("that was wrong", ("ease", None)), ("Why?", ("why", None)), ("why did you say that", ("why", None)),
                 ("why is their carry so farmed", None), ("okay talk again", ("quiet", False))]
        got = [fast(t) for t, _ in cases]
        res.append(("listen: keyword fast-path (mute/quiet/talk again/talk more/repeat/bad call/why)", got == [w for _, w in cases], str(got)))
        spoken = ["Three missing. Back to tower.", "Careful, Lion!", "Breathe. One fight at a time."]
        e = [is_echo("three missing back to tower", spoken), is_echo("careful lion", spoken), is_echo("where is lion", spoken),
             is_echo("I'm going roshan now", spoken)]
        res.append(("listen: echo suppression (own lines ignored, player's kept)", e == [True, True, False, False], str(e)))
        world.set_settings = lambda **kw: (calls.append(kw), Wm["settings"].update(kw))  # noqa: E731
        rec.event = lambda kind, **kw: evs.append((kind, kw))  # noqa: E731
        Wm = {"settings": {"verbosity": "normal", "role": 4}, "game": {"clock": 600.0, "matchid": "x"}, "me": {"hero": "lion", "role": 4}}
        _reset("x")
        _helper_msg(Wm, {"ptt": "down"})
        st = [T["stage"]]
        _helper_msg(Wm, {"ptt": "up"})
        st.append(T["stage"])
        _stage("thinking")
        out = {"reply": "Good call. I'll keep it short.", "settings": {"verbosity": "low", "commentary": None, "voice": None, "mic": None, "role": 4, "turbo": None},
               "note": "enemy Lion has blink", "focus": "call mid rotations", "emotion": "frustrated", "intent": "command_settings"}
        r1 = apply(Wm, "less chatter please, and their lion has blink", out, 0.001, 600.0, t0=time.time() - 0.8)
        st.append(T["stage"])
        nul = {"verbosity": None, "commentary": None, "voice": None, "mic": None, "role": None, "turbo": None}
        vent = dict(out, settings=nul, note="", focus="", intent="venting", reply="Breathe. Farm bot safe for two minutes.", emotion="tilted")
        r2, r3 = apply(Wm, "ugh I keep dying", vent, 0, 610.0), apply(Wm, "this game is lost", vent, 0, 620.0)
        bad = apply(Wm, "where is their carry", {**out, "settings": {**nul, "role": 9, "commentary": True, "voice": True}, "intent": "question",
                                                 "note": "Enemy Lion has blink."}, 0, 630.0)
        handled = apply(Wm, "coach mute", {**out, "settings": {**nul, "voice": False}, "intent": "command_mute"}, 0, 640.0,
                        hit=("mic", False), changed={"mic": False})
        ev = evs[0][1] if evs else {}
        ok = (r1 == out["reply"] and calls == [{"verbosity": "low"}] and [n["text"] for n in Wm["player_notes"]] == ["enemy Lion has blink"]
              and T["focus"] == "call mid rotations" and r2 and r3 == "" and bad and handled == ""
              and list(T["emotions"]) == ["frustrated", "tilted", "tilted", "frustrated", "frustrated"] and len(T["turns"]) == 5
              and st == ["recording", "transcribing", "speaking"] and evs[0][0] == "voice_cmd" and ev.get("intent") == "command_settings"
              and ev.get("settings_changed") == {"verbosity": "low"} and ev.get("ms", 0) >= 700 and evs[-1][1]["settings_changed"] == {"mic": False})
        res.append(("listen: LLM answer applied (settings diff, bad role ignored, keyword-handled = labels only, note, focus, mood, "
                    "venting <= 1/45 s, stages, voice_cmd event)", ok, f"{r1!r} {r2!r} {r3!r} {handled!r} {calls} {st} {list(T['emotions'])} {ev}"))
        v = llm.validate(out, SCHEMA)
        res.append(("listen: schema accepts a full answer", v is None, str(v)))
        _reset("x")
        Wm.update(settings={}, _={})
        calls[:] = []
        vs, off, llm.DISABLED = dict(voice.S), llm.DISABLED, True  # keywords only: no LLM call from a self-check
        voice.S["calls"] = [{"c": 590, "cue": "gank", "rule": "R1", "why": "R1m", "word": "BACK"}, {"c": 598, "cue": "join", "rule": "R12", "why": "R12", "word": None}]
        said = []
        for t in ("why", "bad call", "", "quiet", "talk again"):
            T["ready"] = None
            _heard(Wm, t) if t else (_helper_msg(Wm, {"ptt": "up"}), _heard(Wm, t))
            said.append(T["ready"] and T["ready"][1])
        voice.S.clear(), voice.S.update(vs)
        llm.DISABLED = off
        res.append(("listen: 'why' speaks the last call's reason; 'bad call' eases that rule for 5 min; an empty press says so; "
                    "'quiet' until next match and 'talk again'",
                    said == [voice.TALK["why_R12"], voice.TALK["ease"], voice.TALK["empty"], voice.TALK["quiet"], voice.TALK["back"]]
                    and Wm["_"]["eased"] == {"R12": 900.0} and calls == [{"quiet": True}, {"quiet": False}], f"{said} {Wm['_']} {calls}"))
    finally:
        voice.DRY, world.set_settings, rec.event = saved[0], saved[2], saved[3]
        T.clear()
        T.update(saved[1])
    return res


if __name__ == "__main__":  # uv run listen.py [--mode ptt|always] [--key "mouse 3 0"] [--file AUDIO] [--chunks]: print helper output
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    p = subprocess.Popen([str(_build()), "--words", WORDS, "--chunk-dir", str(BIN.parent), *sys.argv[1:]], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        print(line.rstrip(), flush=True)
