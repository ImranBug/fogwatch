"""Click-through transparent HUD over the Dota 2 window (macOS). Hosts http://127.0.0.1:<port>/overlay in a WKWebView.
uv run overlay.py [--port 3939] [--pos left|right|top] [--scale 1.0] [--screen N] [--mock]

Never takes focus (accessory app, borderless, ignores mouse). Follows the Dota window every 2 s (largest screen if none,
or --screen N to pin it). The coach never sees it: capture.py streams only the Dota window (desktop-independent window filter).
Reloads the page when the coach server comes back. Ctrl-C quits.
"""
import argparse, ctypes, signal, urllib.request
import objc
from AppKit import (NSWorkspace, NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered, NSColor, NSScreen, NSStatusWindowLevel,
                    NSWindow, NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorIgnoresCycle, NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless)
from Foundation import NSMakeRect, NSObject, NSTimer, NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

# ponytail: CGWindowList via ctypes because pyobjc-framework-Quartz is not in the lock (same as capture.py)
_cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cg.CGWindowListCopyWindowInfo.restype, _cg.CGWindowListCopyWindowInfo.argtypes = ctypes.c_void_p, [ctypes.c_uint32, ctypes.c_uint32]
_cf.CFRelease.argtypes = [ctypes.c_void_p]
OWNERS = ("Dota 2", "dota2")


def dota_rect():
    """Largest layer-0 Dota window as (x, y, w, h) in Cocoa coordinates, or None. Not on-screen-only: a fullscreen Dota
    in an inactive Space still counts (the overlay joins all Spaces)."""
    p = _cg.CGWindowListCopyWindowInfo(16, 0)  # kCGWindowListExcludeDesktopElements
    if not p:
        return None
    wins = objc.objc_object(c_void_p=p)  # toll-free bridged NSArray; the proxy holds its own retain
    _cf.CFRelease(p)
    best = None
    for w in wins:
        b = w.get("kCGWindowBounds")
        if w.get("kCGWindowOwnerName") in OWNERS and w.get("kCGWindowLayer") == 0 and b and (not best or b["Width"] * b["Height"] > best[2] * best[3]):
            best = (b["X"], b["Y"], b["Width"], b["Height"])
    if not best:
        return None
    x, y, w, h = best  # CG: top-left origin of the primary display, y down
    return x, NSScreen.screens()[0].frame().size.height - y - h, w, h


def screen_rect(n=None):
    ss = list(NSScreen.screens())
    f = (ss[n] if n is not None and 0 <= n < len(ss) else max(ss, key=lambda s: s.frame().size.width * s.frame().size.height)).frame()
    return f.origin.x, f.origin.y, f.size.width, f.size.height


class Overlay(NSObject):
    @objc.python_method
    def setup(self, a):
        self.a, self.rect, self.up = a, None, False
        self.url = f"http://127.0.0.1:{a.port}/overlay?pos={a.pos}&scale={a.scale}" + ("&mock" if a.mock else "")
        w = self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(NSMakeRect(0, 0, 400, 300), NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False)
        w.setOpaque_(False); w.setBackgroundColor_(NSColor.clearColor()); w.setHasShadow_(False)
        w.setIgnoresMouseEvents_(True); w.setLevel_(NSStatusWindowLevel); w.setReleasedWhenClosed_(False)
        w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary
                                 | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorIgnoresCycle)
        self.web = WKWebView.alloc().initWithFrame_configuration_(w.contentView().bounds(), WKWebViewConfiguration.alloc().init())
        self.web.setValue_forKey_(False, "drawsBackground")
        self.web.setAutoresizingMask_(18)  # width | height sizable
        w.setContentView_(self.web)
        self.tick_(None)
        w.orderFrontRegardless()  # shows without activating this app
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(2.0, self, "tick:", None, True)
        self.ftimer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.3, self, "front:", None, True)
        return self

    def front_(self, _):
        """Show only while Dota is the frontmost app (or with --mock / --always); hide over every other app."""
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        show = self.a.mock or self.a.always or (app is not None and str(app.localizedName()) in OWNERS)
        if show and not self.win.isVisible():
            self.win.orderFrontRegardless()
        elif not show and self.win.isVisible():
            self.win.orderOut_(None)

    def tick_(self, _):
        if self.win.isVisible():  # after sleep / a display change the window can drop off screen while isVisible() stays True: re-front it every 2 s
            self.win.orderFrontRegardless()
        r = screen_rect(self.a.screen) if self.a.screen is not None else dota_rect() or screen_rect()
        if r != self.rect:
            self.rect = r
            self.win.setFrame_display_(NSMakeRect(*r), True)
            print("overlay frame", tuple(int(v) for v in r), flush=True)
        try:  # ponytail: blocking localhost probe on the main thread, refused connections return at once
            urllib.request.urlopen(f"http://127.0.0.1:{self.a.port}/overlay", timeout=0.5).close()
            up = True
        except OSError:
            up = False
        if up and not self.up:  # first start, or the coach server came back: (re)load the page
            self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(self.url)))
            print("loaded", self.url, flush=True)
        self.up = up


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=3939)
    ap.add_argument("--pos", choices=("left", "right", "top"), default="left")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--screen", type=int, help="cover NSScreen N instead of following the Dota window (0 = main)")
    ap.add_argument("--mock", action="store_true", help="load /overlay?mock (preview without a game)")
    ap.add_argument("--always", action="store_true", help="show even when Dota is not the frontmost app")
    a = ap.parse_args()
    for i, s in enumerate(NSScreen.screens()):
        print(f"screen {i}: {s.localizedName()} {int(s.frame().size.width)}x{int(s.frame().size.height)}", flush=True)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon, never steals focus from Dota
    ov = Overlay.alloc().init().setup(a)  # noqa: F841 (keep a reference)
    signal.signal(signal.SIGTERM, lambda *_: AppHelper.stopEventLoop())  # handled at the next 2 s tick
    AppHelper.runEventLoop(installInterrupt=True)  # Ctrl-C stops the loop cleanly
    print("overlay closed", flush=True)


if __name__ == "__main__":
    main()
