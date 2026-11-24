# macOS screen capture and CV runtime for the Dota 2 coach (tested on this Mac, 2026-11-09)

**Recommendation:** capture continuously with a ScreenCaptureKit `SCStream` aimed at the Dota 2 window. Use Python 3.13 managed by uv, FastAPI and uvicorn for the server, and Server-Sent Events (SSE) to push updates to the dashboard. The capture costs about 3 ms of CPU per second at 4 fps. It worked on macOS 26.5.2 even while the Dota window was on another Space. The capture module below was run on this machine and its self-check passed.

Everything below was measured on this Mac unless marked UNVERIFIED or attributed to a web source. Test environment: macOS 26.5.2 (25F84), M3 Pro, uv 0.11.28, pyobjc 13.0, mss 10.2.0, numpy 2.5.4, opencv-python-headless 5.0.0.93. Dota 2 was running in the menu. The test scripts are in `<scratch>/captest/`.

## 0. Corrections to the project context
- **Window owner name.** `kCGWindowOwnerName` is `"Dota 2"`, not `"dota2"` (the binary is `dota2.app/Contents/MacOS/dota2`). The game window also has `kCGWindowName == "Dota 2"` and `kCGWindowLayer == 0`. Dota owns about 10 other helper windows (30–64 px strips), so pick the largest window on layer 0.
- **Display and size.**
  - The Dota window is on the **Studio Display (main, CGDisplayID 3)**, not the built-in display. Its bounds are 0,0 2560×1440 points. That display is 2560×1440 points and 5120×2880 pixels.
  - The built-in display is ID 1: 1728×1117 points, 3456×2234 pixels, at origin **(-1728, 0)**. It sits to the left of the main display, so its coordinates are negative.
  - `userdata/<account-id>/570/local/cfg/video.txt` still says `defaultres 1728 × 1117`, `fullscreen 0`, `nowindowborder 1`. That file is stale or was written for the laptop. A borderless window covers whichever display it is on.
  - Do the coach layout the other way round: Dota on one display, dashboard on the other. Don't hardcode which is which; find the window at runtime.
- **Points vs pixels (Retina).** Every API takes rectangles in points. Output pixels are points × 2 only if you ask for it. Dota is not hi-dpi, so the game renders at about 1× points and macOS upscales it. When I captured the minimap area at 2× (600×600 for 300 pt), the image was visibly soft and upscaled. **Capture at scale 1.0.** 2× costs 4× the pixels and adds no detail. Keep `scale` as a setting you can change.

## 1. Capture methods compared (all run on macOS 26.5.2)

| Method | Still works on 26? | 300×300 pt region | Full 2560×1440 window | CPU in-process | Notes |
|---|---|---|---|---|---|
| **SCStream** (pyobjc ScreenCaptureKit), window filter, 4 fps | Yes | 40/40 frames in 10 s, push model | — | **~3.2 ms per second** (about 0.3% of one core) | Works with Dota on another Space or covered by other windows. Lowest overhead. **Recommended.** |
| SCStream, display filter, 4 fps | Yes | 40/40 frames | — | ~3.2 ms per second | Captures whatever is on top of that screen area |
| `SCScreenshotManager.captureImage…` (macOS 14+) | Yes for displays. **Window capture failed with error -3811** while the window was on another Space | 45 ms | 60 ms (display) | ~1 ms per call | Sets up a stream on every call, so it is slow. Fine for occasional full-HUD snapshots. |
| `CGWindowListCreateImage` (pyobjc Quartz), `kCGWindowListOptionIncludingWindow` + window ID | **Yes at runtime** (still exposed in pyobjc 13) | 32 ms nominal; 128 ms at best/2× | 39 ms (nominal or 2×) | 0.6–1.4 ms per call | Works on an off-screen or other-Space window, but probably returns the last image the window drew (UNVERIFIED whether that is live). Removed from the macOS 15 SDK headers [1]. One report says it was removed in 26 [2]; my test on 26.5.2 contradicts that. Use only as a fallback. |
| `CGDisplayCreateImageForRect` | Yes | 30 ms (2× pixels) | — | ~1 ms | Same deprecation status as above |
| **mss 10.2.0** | Yes | 22–35 ms | 40 ms | 0.8 ms per grab | Calls `CGWindowListCreateImage(rect, OnScreenOnly, 0, NominalResolution)` through ctypes. It always returns **point resolution** and grabs whatever is on screen in that area. `mss.mss()` is deprecated; use `mss.MSS()`. It uses the API Apple is removing. |
| PIL `ImageGrab.grab(bbox)` | Yes | 110 ms | — | — | Starts the `screencapture` CLI and writes a temp file. Returns 2× pixels. |
| `screencapture -x -R…` CLI | Yes | 95 ms | — | process spawn | Too slow for continuous use |

At 2–5 fps any of these is fine on CPU. The difference is in reliability and future-proofing. CGWindowList and mss are deprecated APIs that may be removed. A stream delivers frames when they arrive, which is ideal for a 4 fps loop, and keeps working when windows overlap. The CPU numbers are per Python process. WindowServer/replayd cost was not measured (UNVERIFIED), but it is expected to be small for a 300×300 output.

## 2. Permissions (TCC)
- macOS grants Screen Recording to the **responsible app**, not to python: Terminal, iTerm, Cursor, and so on. Here `CGPreflightScreenCaptureAccess()` returned `True`, because the terminal app already has the permission.
- If you later run the coach from launchd or a LaunchAgent, the python binary itself needs the grant. A uv-managed python lives at a versioned path (`~/.local/share/uv/python/cpython-3.13…`), so upgrading Python means granting again.
- Call `Quartz.CGRequestScreenCaptureAccess()` once to trigger the prompt.
- From macOS 15 onward, the OS shows a recurring (monthly) prompt to re-approve apps that capture without the system picker. Apple first planned weekly prompts and relaxed it to monthly in the 15 betas [3][4]. Whether 26.x still does this is UNVERIFIED.
- Getting `SCShareableContent` with `onScreenWindowsOnly=False` is required to find Dota when it is on another Space.

## 3. Finding the window
- **ScreenCaptureKit (preferred):** `SCShareableContent.windows()`, filtered by `owningApplication().applicationName()=="Dota 2"`, `windowLayer()==0`, then take the largest `frame()`. `SCWindow.windowID()` matches the CGWindowID (36891 in my run). `SCContentFilter.pointPixelScale()` gave `2.0` and `contentRect` gave 2560×1440.
- **Quartz:** `CGWindowListCopyWindowInfo(kCGWindowListOptionAll, kCGNullWindowID)`, filtered by `kCGWindowOwnerName=="Dota 2" and kCGWindowName=="Dota 2"`. `kCGWindowBounds` is in global points with a top-left origin. A second display can have negative X.
- **Use `kCGWindowListOptionAll`, not `OnScreenOnly`.** When Dota moved to another Space, `OnScreenOnly` stopped returning it.
- With a window filter, `sourceRect` is in **window-local points** with a top-left origin. You don't need to know which display the window is on.

## 4. Recommended capture module (run here: 13 frames in 3 s, shape (300,300,3))
Working file: `<scratch>/captest/capture.py`

```python
import threading, numpy as np, objc
import Quartz as Q, CoreMedia as CM, ScreenCaptureKit as SCK, libdispatch
from Foundation import NSObject

Q.CGMainDisplayID()  # opens the CoreGraphics connection; without it SCK aborts with CGS_REQUIRE_INIT

def _wait(fn, *args):
    ev, out = threading.Event(), {}
    def done(*r):                                  # ObjC blocks must return None
        out["r"] = r; ev.set()
    fn(*args, done)
    if not ev.wait(5): raise TimeoutError(fn)
    return out["r"]

def find_dota_window():
    content, err = _wait(SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_, True, False)
    if err: raise RuntimeError(err)  # usually: Screen Recording not granted
    wins = [w for w in content.windows()
            if w.owningApplication() and w.owningApplication().applicationName() == "Dota 2" and w.windowLayer() == 0]
    if not wins: raise LookupError("Dota 2 window not found")
    return max(wins, key=lambda w: w.frame().size.width * w.frame().size.height)

class _Out(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
    def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
        pb = CM.CMSampleBufferGetImageBuffer(sbuf)
        if pb is None: return                       # idle frame: nothing changed
        Q.CVPixelBufferLockBaseAddress(pb, 1)
        try:
            h, w, bpr = Q.CVPixelBufferGetHeight(pb), Q.CVPixelBufferGetWidth(pb), Q.CVPixelBufferGetBytesPerRow(pb)
            raw = Q.CVPixelBufferGetBaseAddress(pb).as_buffer(h * bpr)
            frame = np.frombuffer(raw, np.uint8).reshape(h, bpr)[:, :w*4].reshape(h, w, 4)[..., :3].copy()  # BGR
        finally:
            Q.CVPixelBufferUnlockBaseAddress(pb, 1)
        self.on_frame(frame)
    def stream_didStopWithError_(self, stream, err):
        print("capture stopped:", err)              # window closed / permission revoked -> restart

def start(on_frame, region_pts=None, fps=4, scale=1.0):
    win = find_dota_window()
    x, y, w, h = region_pts or (0, 0, win.frame().size.width, win.frame().size.height)
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setSourceRect_(Q.CGRectMake(x, y, w, h))                       # window-local points
    cfg.setWidth_(int(w * scale)); cfg.setHeight_(int(h * scale))      # output pixels
    cfg.setMinimumFrameInterval_(CM.CMTimeMake(1, fps))
    cfg.setPixelFormat_(int.from_bytes(b"BGRA", "big")); cfg.setQueueDepth_(3); cfg.setShowsCursor_(False)
    out = _Out.alloc().init(); out.on_frame = on_frame
    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(win), cfg, out)
    ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(
        out, SCK.SCStreamOutputTypeScreen, libdispatch.dispatch_queue_create(b"dota.capture", None), None)
    if not ok: raise RuntimeError(err)
    (err,) = _wait(stream.startCaptureWithCompletionHandler_)
    if err: raise RuntimeError(err)
    return stream, out   # keep both referenced or ObjC frees them
```

Things that tripped me up while testing:
1. Without `Q.CGMainDisplayID()` (or another call that opens the CoreGraphics connection) first, the process aborts with `Assertion failed: (did_initialize), CGS_REQUIRE_INIT`.
2. A completion lambda that returns a value crashes the whole process with `did not return None, expecting void return value`.
3. You need `pyobjc-framework-libdispatch` for the dispatch queue.
4. No NSRunLoop is needed. Callbacks arrive on dispatch threads.
5. Frames come in on a background thread. Run the CV in that callback (OpenCV releases the GIL) or hand the frame to a worker.
6. To capture the minimap plus a few HUD regions, run one stream on the full window at 1× and crop with numpy (`frame[y:y+h, x:x+w]`). Cropping is free. Use separate streams only if you need different frame rates.

Fallback for one-off snapshots (works on 26.5.2; deprecated):
```python
img = Q.CGWindowListCreateImage(Q.CGRectNull, Q.kCGWindowListOptionIncludingWindow, wid,
        Q.kCGWindowImageBoundsIgnoreFraming | Q.kCGWindowImageNominalResolution)
w, h, bpr = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img), Q.CGImageGetBytesPerRow(img)
a = np.frombuffer(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(img)), np.uint8)[:h*bpr].reshape(h, bpr)[:, :w*4].reshape(h, w, 4)
```
Rows are padded, so always reshape using `bytesPerRow`. A naive `reshape(h, w, 4)` failed for `CGDisplayCreateImageForRect`.

## 5. Python version and pins
- **System Python 3.9 won't work:**
  - numpy 2.5.4 requires Python ≥3.12.
  - pyobjc-core 13.0 (released 2026-10-30) requires ≥3.11.
  - FastAPI 0.162.2 and uvicorn 0.54.0 require ≥3.10.
- **Use uv-managed Python 3.13.** A binary-only (`--only-binary :all:`) resolve succeeded on both 3.13 and 3.14. 3.12 was actually installed and run. opencv-python-headless ships abi3 `cp37…macosx_13_0_arm64` wheels, so it covers every version.

```toml
# pyproject.toml
[project]
requires-python = ">=3.13,<3.15"
dependencies = [
  "numpy==2.5.4",
  "opencv-python-headless==5.0.0.93",
  "pyobjc-core==13.0",
  "pyobjc-framework-ScreenCaptureKit==13.0",   # pulls Quartz, CoreMedia, Cocoa 13.0
  "pyobjc-framework-libdispatch==13.0",
  "fastapi==0.162.2",
  "uvicorn==0.54.0",                            # plain; no [standard] needed with SSE
]
```
Set it up with `uv init --python 3.13 && uv add …`. I left mss and Pillow out because nothing in this design needs them. Add `mss` only as a quick prototyping tool.

## 6. Web server: FastAPI + uvicorn + SSE
- **Data flow.** GSI is plain HTTP POST JSON. The dashboard only needs updates from server to browser, so SSE is enough. Use WebSocket only if the browser needs to talk back over the same channel, and ordinary POSTs from the browser cover that.
- **No extra dependencies for SSE.**
  - Plain `uvicorn` 0.54 has **no WebSocket support** without `websockets` or `wsproto` installed (`AutoWebSocketsProtocol` is `None`).
  - SSE needs neither.
  - FastAPI 0.162 includes `fastapi.sse.EventSourceResponse`. If a path operation yields dicts, FastAPI JSON-encodes them as `data:` lines.
  - Browser side: `new EventSource('/events')` reconnects automatically.
- **Alternatives.** aiohttp 3.14.5 would work just as well. The stdlib `http.server` would need hand-written threading and SSE, which isn't worth it.

The server below was tested here: a curl POST to `/gsi` showed up on the `/events` stream.
```python
import asyncio
from fastapi import FastAPI, Request
from fastapi.sse import EventSourceResponse

app = FastAPI()
state = {"gsi": None, "minimap": None}
changed = asyncio.Condition()

async def publish(key, value):
    state[key] = value
    async with changed: changed.notify_all()

@app.post("/gsi")
async def gsi(req: Request):
    body = await req.json()
    if body.get("auth", {}).get("token") != "coach-secret": return {}
    await publish("gsi", body)
    return {}

@app.get("/events", response_class=EventSourceResponse)
async def events():
    while True:
        yield state                     # full snapshot; client re-renders
        async with changed: await changed.wait()

@app.on_event("startup")               # bridge capture thread -> event loop
async def _cap():
    loop = asyncio.get_running_loop()
    from capture import start
    app.state.cap = start(lambda f: asyncio.run_coroutine_threadsafe(publish("minimap", parse_minimap(f)), loop), fps=4)
```
`parse_minimap` is your OpenCV function. Have it return small JSON (hero positions), never pixels. Bind to `127.0.0.1` only: `uvicorn server:app --host 127.0.0.1 --port 3000`. Newer FastAPI prefers `lifespan=` over `on_event`; both work.

Matching GSI config file: `…/game/dota/cfg/gamestate_integration/gamestate_integration_coach.cfg`
```
"coach" { "uri" "http://127.0.0.1:3000/gsi" "timeout" "5.0" "buffer" "0.1" "throttle" "0.1" "heartbeat" "30.0"
  "data" { "provider" "1" "map" "1" "player" "1" "hero" "1" "abilities" "1" "items" "1" "draft" "1" "wearables" "0" }
  "auth" { "token" "coach-secret" } }
```
The GSI field set is a summary from memory and wasn't checked against live GSI output here; defer to the GSI researcher.

## 7. Still unverified
- Whether SCStream window frames stay **live** while Dota is fully covered or on another Space. Frames kept arriving, but Dota was sitting idle on a menu. Check during a real match.
- The minimap pixel rectangle at 2560×1440 with the user's HUD scale. Calibrate it from a captured frame and keep it as a config value.
- Whether macOS 26 still shows the monthly re-approval prompt.

Sources:
[1] [eclipsa-audio-plugin #10: CGWindowListCreateImage obsoleted in macOS 15 SDK](https://github.com/google/eclipsa-audio-plugin/issues/10) · [WebKit bug 277564](https://bugs.webkit.org/show_bug.cgi?id=277564)
[2] [nut.js #625 (claims removal in macOS 26; contradicted by my test on 26.5.2)](https://github.gg22.workers.dev/nut-tree/nut.js/issues/625)
[3] [pxlnv: macOS should permit people to take risks (monthly prompt)](https://pxlnv.com/blog/macos-should-permit-risks/)
[4] [tinyapps: disable Sequoia nag](https://tinyapps.org/blog/202409180700_disable_sequoia_nag.html) · [Apple forum: CGWindowListCreateImage → ScreenCaptureKit](https://developer.apple.com/forums/thread/740493)