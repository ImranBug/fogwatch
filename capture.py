"""Dota 2 window capture: ScreenCaptureKit stream (macOS 14+), PNG replay, one-shot snap.

start()/snap() raise LookupError (no Dota window) or RuntimeError (no Screen Recording permission, SCK error).
Callers retry; handle["dead"] is set when a live stream stops (window closed, permission revoked).
"""
import logging, threading, time
from pathlib import Path
import cv2, numpy as np

log = logging.getLogger("capture")
_asked = False

_APP = "Dota 2"  # kCGWindowOwnerName / SCRunningApplication name

try:
    import ctypes, objc, CoreMedia as CM, ScreenCaptureKit as SCK, libdispatch
    from Foundation import NSObject, NSMakeRect
    # ponytail: CoreGraphics/CoreVideo via ctypes because pyobjc-framework-Quartz is not in the lock; add it to drop this
    _cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    _cv = ctypes.CDLL("/System/Library/Frameworks/CoreVideo.framework/CoreVideo")
    _cg.CGPreflightScreenCaptureAccess.restype = _cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    for f in ("CVPixelBufferGetHeight", "CVPixelBufferGetWidth", "CVPixelBufferGetBytesPerRow"):
        getattr(_cv, f).restype = ctypes.c_size_t; getattr(_cv, f).argtypes = [ctypes.c_void_p]
    _cv.CVPixelBufferGetBaseAddress.restype = ctypes.c_void_p; _cv.CVPixelBufferGetBaseAddress.argtypes = [ctypes.c_void_p]
    _cv.CVPixelBufferLockBaseAddress.argtypes = _cv.CVPixelBufferUnlockBaseAddress.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    import warnings; warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)
    _cg.CGMainDisplayID()  # opens the CoreGraphics connection; without it SCK aborts with CGS_REQUIRE_INIT
    _IMPORT_ERR = None
except Exception as e:  # non-mac / missing pyobjc: live capture unavailable, replay still works
    _IMPORT_ERR, NSObject = e, object


def _wait(fn, *args):
    ev, out = threading.Event(), {}
    def done(*r):                                   # ObjC blocks must return None
        out["r"] = r; ev.set()
    fn(*args, done)
    if not ev.wait(5): raise TimeoutError(fn)
    return out["r"]


def find_dota_window():
    global _asked
    if _IMPORT_ERR: raise RuntimeError(f"ScreenCaptureKit unavailable: {_IMPORT_ERR}")
    if not _cg.CGPreflightScreenCaptureAccess():
        if not _asked: _asked = True; _cg.CGRequestScreenCaptureAccess()   # shows the system prompt once
        raise RuntimeError("Screen Recording permission not granted to this terminal app (System Settings > Privacy & Security > "
                           "Screen Recording; then quit and reopen the terminal app)")
    content, err = _wait(SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_, True, False)
    if err or content is None: raise RuntimeError(f"SCShareableContent failed: {err}")
    wins = [w for w in content.windows()
            if w.owningApplication() and w.owningApplication().applicationName() == _APP and w.windowLayer() == 0]
    if not wins: raise LookupError("Dota 2 window not found")
    return max(wins, key=lambda w: w.frame().size.width * w.frame().size.height)


if _IMPORT_ERR is None:
    class _Out(NSObject, protocols=[objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]):
        def stream_didOutputSampleBuffer_ofType_(self, stream, sbuf, kind):
            try: frame = _to_bgr(sbuf)
            except Exception:                       # an exception escaping into ObjC aborts the whole process
                if not self.handle.get("warned"): self.handle["warned"] = True; log.exception("frame decode failed (logged once)")
                return
            if frame is not None: _call(self.on_frame, frame, self.handle)  # serial queue: slow callback -> SCK drops frames
        def stream_didStopWithError_(self, stream, err):
            log.warning("capture stopped: %s", err)
            self.handle["dead"].set()


def _to_bgr(sbuf):
    pb = CM.CMSampleBufferGetImageBuffer(sbuf)
    if pb is None: return None                      # idle frame: nothing changed
    p = getattr(pb, "pointerAsInteger", None) or pb.__c_void_p__().value  # opaque PyObjCPointer, or a typed CF proxy once Quartz is installed
    _cv.CVPixelBufferLockBaseAddress(p, 1)          # 1 = read-only
    try:
        h, w, bpr = _cv.CVPixelBufferGetHeight(p), _cv.CVPixelBufferGetWidth(p), _cv.CVPixelBufferGetBytesPerRow(p)
        raw = (ctypes.c_uint8 * (h * bpr)).from_address(_cv.CVPixelBufferGetBaseAddress(p))
        return np.frombuffer(raw, np.uint8).reshape(h, bpr)[:, :w * 4].reshape(h, w, 4)[..., :3].copy()  # BGRA -> BGR
    finally:
        _cv.CVPixelBufferUnlockBaseAddress(p, 1)


def _call(on_frame, frame, handle):
    try: on_frame(frame)
    except Exception:                               # never let a CV bug kill the capture thread
        if not handle.get("warned"): handle["warned"] = True; log.exception("on_frame failed (logged once)")


def start(on_frame, fps=4, scale=1.0):
    """Stream the whole Dota window at `scale` x points (1.0 = Dota's real render res). Returns a handle; keep it."""
    win = find_dota_window()
    w, h = win.frame().size.width, win.frame().size.height
    cfg = SCK.SCStreamConfiguration.alloc().init()
    cfg.setSourceRect_(NSMakeRect(0, 0, w, h))    # window-local points
    cfg.setWidth_(int(w * scale)); cfg.setHeight_(int(h * scale))
    cfg.setMinimumFrameInterval_(CM.CMTimeMake(1, int(fps)))
    cfg.setPixelFormat_(int.from_bytes(b"BGRA", "big")); cfg.setQueueDepth_(3); cfg.setShowsCursor_(False)
    handle = {"dead": threading.Event()}
    out = _Out.alloc().init(); out.on_frame = on_frame; out.handle = handle
    stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
        SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(win), cfg, out)
    queue = libdispatch.dispatch_queue_create(b"dota.capture", None)
    ok, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(out, SCK.SCStreamOutputTypeScreen, queue, None)
    if not ok: raise RuntimeError(f"addStreamOutput failed: {err}")
    (err,) = _wait(stream.startCaptureWithCompletionHandler_)
    if err: raise RuntimeError(f"startCapture failed: {err}")
    handle.update(stream=stream, out=out, queue=queue)  # ObjC frees them if unreferenced
    return handle


def start_files(dir, on_frame, fps=4, pattern="*.png", load=None):
    """Replay sorted `pattern` files in `dir` as BGR frames at `fps`, looping, on a daemon thread. load(path) -> frame."""
    files = sorted(Path(dir).glob(pattern))
    if not files: raise LookupError(f"no {pattern} in {dir}")
    load = load or (lambda f: cv2.imread(str(f), cv2.IMREAD_COLOR))
    handle = {"dead": threading.Event(), "stop": threading.Event()}
    def run():
        while not handle["stop"].is_set():
            for f in files:
                t0 = time.monotonic()
                frame = load(f)
                if frame is not None: _call(on_frame, frame, handle)
                if handle["stop"].wait(max(0.0, 1 / fps - (time.monotonic() - t0))): return
    handle["thread"] = threading.Thread(target=run, name="capture-files", daemon=True)
    handle["thread"].start()
    return handle


def stop(handle):
    if not handle: return
    if "stop" in handle: handle["stop"].set()
    if handle.get("stream") is not None:
        try: _wait(handle["stream"].stopCaptureWithCompletionHandler_)
        except Exception as e: log.warning("stopCapture: %s", e)
    handle["dead"].set()


def snap(timeout=3.0):
    """One BGR frame of the Dota window (starts a short-lived stream)."""
    got, ev = [], threading.Event()
    h = start(lambda f: (got.append(f), ev.set()), fps=4)
    try:
        if not ev.wait(timeout): raise TimeoutError("no frame from Dota window (minimised? idle?)")
        return got[0]
    finally:
        stop(h)


if __name__ == "__main__":
    import sys, tempfile
    logging.basicConfig(level=logging.INFO)
    try:
        f = snap(); print("snap ok", f.shape)
    except (LookupError, RuntimeError, TimeoutError) as e:
        print("live capture unavailable (expected without Dota):", type(e).__name__, e)
    d = Path(tempfile.mkdtemp())
    for i in range(3): cv2.imwrite(str(d / f"{i:03}.png"), np.full((20, 30, 3), i * 50, np.uint8))
    got = []
    h = start_files(d, got.append, fps=20); time.sleep(0.5); stop(h)
    assert len(got) >= 6 and got[0].shape == (20, 30, 3) and got[1][0, 0, 0] == 50, len(got)
    print("start_files ok", len(got), "frames"); sys.exit(0)
