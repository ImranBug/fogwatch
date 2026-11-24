"""Matchmaking status from the Dota menu: GSI sends nothing there, so the dashboard reads "FINDING MATCH" and the queue line
("NORMAL MATCH | TURBO | REGIONS: 3") off the captured Dota window with Apple's on-device text recognition. Only these
keywords are kept; no other screen text is stored. Runs only while no match data arrives, at most every 2 s."""
import re
import time

MODES = ("RANKED ALL PICK", "ALL PICK", "TURBO", "SINGLE DRAFT", "ALL RANDOM", "CAPTAINS MODE", "CAPTAINS DRAFT", "ABILITY DRAFT")
S = {"at": 0.0, "seen": 0.0, "state": None, "mode": None, "kind": None, "since": None}


def read(frame):
    """Text lines from two full-resolution crops of a BGR frame: the queue corner (bottom right) and the ready-check dialog
    (centre). Vision accurate level. Downscaling the whole frame lost the small queue text."""
    import cv2
    import Vision
    from Foundation import NSData
    h, w = frame.shape[:2]
    out = []
    for crop in (frame[int(h * .85):, int(w * .62):], frame[int(h * .3):int(h * .7), int(w * .3):int(w * .7)]):
        ok, png = cv2.imencode(".png", crop)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0)  # accurate: "fast" read TURBO as "TifftO0"
        Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithBytes_length_(png.tobytes(), len(png)), None) \
            .performRequests_error_([req], None)
        out += [o.topCandidates_(1)[0].string() for o in req.results() or []]
    return out


def parse(lines):
    """-> {state: searching|found|None, mode, kind} from OCR lines ('|' is often read as 'I')."""
    t = " ".join(lines).upper()
    state = "found" if re.search(r"GAME IS READY|MATCH FOUND|\bACCEPT\b", t) else "searching" if "FINDING MATCH" in t else None
    q = re.search(r"(NORMAL|RANKED|UNRANKED)\s+MATCH\W+(?:I\s+)?(.*?)(?:\s+I\s+|\s*\|\s*)RE[GC]ION", t)
    mode = (next((m for m in MODES if q and m in q.group(2)), None) or next((m for m in MODES if m in t), None)) if state else None
    return {"state": state, "mode": mode.title() if mode else None, "kind": q.group(1).title() if q else None}


def tick(frame, W):
    """Called from the capture thread. Updates W['menu'] (None once nothing was seen for 6 s)."""
    now = time.time()
    if now - S["at"] < 2:
        return
    S["at"] = now
    try:
        p = parse(read(frame))
    except Exception:
        return
    if p["state"]:
        S.update(seen=now, since=S["since"] if S["state"] == p["state"] else now, **{k: v or S.get(k) for k, v in p.items()})
    elif now - S["seen"] > 6:
        S.update(state=None, since=None)
    W["menu"] = {"state": S["state"], "mode": S["mode"], "kind": S["kind"], "for_s": round(now - S["since"]) if S["since"] else None} \
        if S["state"] else None


if __name__ == "__main__":
    ok = [parse(["NORMAL MATCH I TURBO I REGIONS: 3", "FINDING MATCH"]), parse(["PLAY DOTA"]), parse(["YOUR GAME IS READY", "ACCEPT"])]
    assert ok == [{"state": "searching", "mode": "Turbo", "kind": "Normal"}, {"state": None, "mode": None, "kind": None},
                  {"state": "found", "mode": None, "kind": None}], ok
    print("menu ok")
