"""Classical CV on captured Dota frames (spec §4): HUD regions, top-bar lineup, dead state,
minimap hero icons -> world x,y. No AI, no game memory: only rendered pixels plus the VPK sprite sheet.

Coordinates: minimap u,v are pixels inside the minimap crop (icon centre, pixel-edge convention);
"minimap_rect" gives the crop in frame pixels. World x,y are GSI coordinates.
Minimap roster: top-bar slots identified with score >= T_SLOT, plus (until all 10 lock) heroes already
in W["heroes"], plus your own hero (your icon feeds the affine fit).
"""
import json, logging, math, os, re, struct, tempfile, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch
from functools import lru_cache
from pathlib import Path
import cv2, numpy as np

log = logging.getLogger("vision")
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"; CACHE = DATA / "cache"; CALIB = DATA / "calib.json"
STEAM = Path.home() / "Library/Application Support/Steam"
VPK = STEAM / "steamapps/common/dota 2 beta/game/dota/pak01_dir.vpk"
CDN = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/"
B = 9490.0  # ponytail: default-mapping map half-size, from 25 live CV-vs-GSI pairs (2026-11, 1 match); the fitted affine replaces it
ILLUSION = {"phantom_lancer", "naga_siren", "terrorblade", "chaos_knight", "meepo", "arc_warden"}
SLOT_BGR = [(255, 117, 51), (191, 255, 102), (191, 0, 191), (11, 240, 243), (0, 107, 255),
            (194, 134, 254), (71, 180, 161), (247, 217, 101), (33, 131, 0), (0, 105, 164)]  # dotastyles.css
T_DET, T_GATE, T_OVERLAP, T_COARSE, MARGIN, T_SLOT = 0.70, 0.55, 0.75, 0.45, 1.15, 0.75
ASPECT = None  # Dota render w/h override; None = from video.txt, 0 = same as the captured content (synthetic frames)

_LOCK = threading.RLock()
_S, _SPR, _P, _warned, _nofetch = {}, {}, {}, set(), set()


def _warn(msg):
    if msg not in _warned: _warned.add(msg); log.warning(msg)


# ---------- geometry ----------

def minimap_settings():
    """(side 'left'|'right', extra_large, hero_size) from the newest user_convars vcfg; Dota defaults if absent."""
    files = sorted(STEAM.glob("userdata/*/570/local/cfg/user_convars_0_slot0.vcfg"), key=os.path.getmtime)
    txt = files[-1].read_text(errors="ignore") if files else ""
    if not files: _warn("no user_convars vcfg found; assuming minimap on the left, normal size")
    get = lambda k, d: m.group(1) if (m := re.search(rf'"{k}"\s+"([^"]*)"', txt)) else d
    try: hs = int(float(get("dota_minimap_hero_size", "600")))
    except ValueError: hs = 600
    return ("right" if get("dota_minimap_position_option", "0") == "1" else "left",
            get("dota_hud_extra_large_minimap", "0") in ("1", "true"), hs)


def render_aspect():
    """Dota's render width/height from the newest userdata video.txt, or None."""
    files = sorted(STEAM.glob("userdata/*/570/local/cfg/video.txt"), key=os.path.getmtime)
    txt = files[-1].read_text(errors="ignore") if files else ""
    w, h = (re.search(rf'"setting.{k}"\s+"(\d+)"', txt) for k in ("defaultres", "defaultresheight"))
    return int(w.group(1)) / int(h.group(1)) if w and h and int(h.group(1)) else None


def content_geom(frame, aspect=None):
    """(sx, sy, ox, oy, cw, ch): HUD scale (x, y) and the content rect in frame px; None if the frame is too dark to tell.
    Black edge rows/cols are bands. A band on one side only = picture shifted, the far end clipped (measured live, macOS
    1728x1117 render in a 2560x1440 window: 43 px black on top, bottom 43 px cut off); bands on both sides = letterbox.
    The HUD scales with render height (1080 ref); a render aspect (video.txt) other than the content's = stretched picture."""
    H, W = frame.shape[:2]
    lit = frame.max(2) >= 20
    rows, cols = np.flatnonzero(lit.any(1)), np.flatnonzero(lit.any(0))
    if not len(rows): return None
    t, b, l, r = (int(v) if v >= 3 else 0 for v in (rows[0], H - 1 - rows[-1], cols[0], W - 1 - cols[-1]))
    if max(t, b) > H / 4 or max(l, r) > W / 4: return None
    ch, cw = H - t - b if b else H, W - l - r if r else W
    sy = ch / 1080; sx = sy * (cw / ch) / (aspect or cw / ch)
    return (round(sx, 4), round(sy, 4), l, t, cw, ch)


def _same_geom(a, b):
    return a is not None and b is not None and len(a) == len(b) == 6 and \
        all(abs(p - q) <= 0.01 * max(p, q) for p, q in zip(a[:2], b[:2])) and all(abs(p - q) <= 3 for p, q in zip(a[2:], b[2:]))


def regions(W, H, mm_side="left", xl=False, s=None, g=None):
    """HUD rectangles (x,y,w,h) in frame px (hud report CSS): 10 top-bar slots (radiant 0-4, dire 5-9), minimap.
    g = content_geom(); default: the HUD fills the frame at s = H/1080. The minimap rect may reach below the frame."""
    sx, sy, ox, oy, cw, ch = g or (s or H / 1080, s or H / 1080, 0, 0, W, H)
    C = ox + cw / 2
    xs = [C + (-416 + 62 * k) * sx for k in range(5)] + [C + (102 + 62 * k) * sx for k in range(5)]
    mw, mh = round((280 if xl else 244) * sx), round((280 if xl else 244) * sy)
    return {"s": sx, "sy": sy, "xl": xl, "slots": [(round(x), round(oy + 4 * sy), round(66 * sx), round(36 * sy)) for x in xs],
            "minimap": (ox + cw - mw if mm_side == "right" else ox, oy + ch - mh, mw, mh)}


def mm_crop(frame, reg):
    """Minimap crop as an m x m square (m = rect width): rows below the frame stay black, a stretched picture is
    resampled so icons are square again. Detections' u,v and the affine live in this crop."""
    x, y, w, h = reg["minimap"]
    c = np.zeros((h, w, 3), np.uint8); sub = frame[y:y + h, x:x + w]; c[:sub.shape[0], :sub.shape[1]] = sub
    return c if h == w else cv2.resize(c, (w, w), interpolation=cv2.INTER_LINEAR)


def default_affine(m, xl=False):
    """world -> minimap-crop px. The map panel is 16 HUD units wider than the visible box (8 each side)."""
    s = m / (280 if xl else 244); p = (296 if xl else 260) * s / (2 * B)
    return np.array([[p, 0, -8 * s + p * B], [0, -p, -8 * s + p * B]])


def world_to_px(A, x, y): return float(A[0, 0] * x + A[0, 1] * y + A[0, 2]), float(A[1, 0] * x + A[1, 1] * y + A[1, 2])
def px_to_world(A, u, v): return world_to_px(cv2.invertAffineTransform(A), u, v)
def _ppu(A): return math.sqrt(abs(np.linalg.det(A[:, :2])))  # px per world unit


def fit_affine(pairs):
    """pairs [(x, y, u, v)] -> 2x3 world->px affine (RANSAC 2 px) or None."""
    P = np.float32(pairs)
    if len(P) < 3: return None
    A, _ = cv2.estimateAffine2D(P[:, None, :2].copy(), P[:, None, 2:].copy(), ransacReprojThreshold=2.0)
    return A


# ---------- assets ----------

def _base(n): return "brewmaster" if n.startswith("brewmaster_") else re.sub(r"_(alt\d*|persona\d+|carnival|tempest_double)$", "", n)


def _extract_sprites(d):
    import vpk
    pak = vpk.open(str(VPK))
    sheet = next(n for n in pak if fnmatch(n, "materials/vgui/hud/minimap_hero_sheet_psd_*.vtex_c"))  # NOT the stale minimap_hero_sheet.vtex_c
    img = cv2.cvtColor(np.frombuffer(pak[sheet].read()[-512 * 512 * 4:], np.uint8).reshape(512, 512, 4), cv2.COLOR_RGBA2BGRA)
    txt = pak["scripts/mod_textures.txt"].read().decode("utf-8-sig", "ignore")
    for m in re.finditer(r'"minimap_heroicon_npc_dota_hero_(\w+)"\s*\{([^}]*)\}', txt):
        kv = dict(re.findall(r'"(\w+)"\s+"([^"]*)"', m.group(2)))
        x, y, w, h = (int(kv[k]) for k in ("x", "y", "width", "height"))
        cell = img[y:y + h, x:x + w]
        if cell.shape[:2] != (32, 32): cell = cv2.resize(cell, (32, 32), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(d / f"{m.group(1)}.png"), cell)


def load_sprites():
    """{sprite name (incl. variants like pudge_persona1): 32x32 BGRA} from the installed VPK sheet, cached as PNGs."""
    with _LOCK:
        if _SPR: return _SPR
        d = CACHE / "sprites"; d.mkdir(parents=True, exist_ok=True)
        files = list(d.glob("*.png"))
        stale = VPK.exists() and (len(files) < 100 or min(f.stat().st_mtime for f in files) < VPK.stat().st_mtime)
        if stale:
            try: _extract_sprites(d); files = list(d.glob("*.png"))
            except Exception as e: _warn(f"VPK sprite sheet unreadable ({e}); using cached/CDN icons")
        for f in files:
            img = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
            if img is not None and img.ndim == 3 and img.shape[2] == 4: _SPR[f.stem] = img
        return _SPR


def _fetch(url, path, flags=cv2.IMREAD_COLOR):
    """Cached CDN image or None (each URL tried once per process)."""
    if path.exists(): return cv2.imread(str(path), flags)
    if url in _nofetch: return None
    try:
        raw = urllib.request.urlopen(url, timeout=10).read()
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), flags)
        if img is None: raise ValueError("not an image")
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return img
    except Exception as e:
        _nofetch.add(url); _warn(f"fetch failed {url}: {e}"); return None


def sprite(name):
    spr = load_sprites()
    if name not in spr:  # VPK missing: CDN 32px icon fallback (base heroes only)
        img = _fetch(f"{CDN}icons/{name}.png", CACHE / "sprites" / f"{name}.png", cv2.IMREAD_UNCHANGED)
        if img is not None and img.ndim == 3 and img.shape[2] == 4: spr[name] = cv2.resize(img, (32, 32), interpolation=cv2.INTER_AREA)
    return spr.get(name)


def variants(hero):
    if _S.get("var_n") != len(load_sprites()):
        _S["var"] = {}
        for n in _SPR: _S["var"].setdefault(_base(n), []).append(n)
        _S["var_n"] = len(_SPR)
    return _S["var"].get(hero) or [hero]


def hero_names():
    names = sorted({_base(n) for n in load_sprites()})
    if names: return names
    p = CACHE / "hero_names.json"  # no VPK and no cache: OpenDota hero list
    try:
        if not p.exists():
            hs = json.load(urllib.request.urlopen("https://api.opendota.com/api/heroes", timeout=10))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps([h["name"].removeprefix("npc_dota_hero_") for h in hs]))
        return json.loads(p.read_text())
    except Exception as e:
        _warn(f"no hero list ({e})"); return []


def _vtex_bgr(b):
    """Decode a Source 2 .vtex_c panorama image (single mip) -> BGR uint8, or None. Formats seen for hero portraits:
    2 = DXT5 (YCoCg, portraits are opaque), 16 = embedded PNG, 4 = raw RGBA8888, 28 = raw BGRA8888."""
    fs, _, _, bo, bc = struct.unpack_from("<IHHII", b, 0)
    for i in range(bc):
        t, o, _ = struct.unpack_from("<4sII", b, 8 + bo + i * 12)
        if t == b"DATA":
            d = 8 + bo + i * 12 + 4 + o
            w, h = struct.unpack_from("<HH", b, d + 20); fmt = b[d + 26]
            break
    else: return None
    if fmt == 16: return cv2.imdecode(np.frombuffer(b[fs:], np.uint8), cv2.IMREAD_COLOR)
    if fmt in (4, 28) and len(b) - fs >= w * h * 4:  # 4 = RGBA8888, 28 = BGRA8888 (checked against CDN portraits)
        return cv2.cvtColor(np.frombuffer(b[-w * h * 4:], np.uint8).reshape(h, w, 4), cv2.COLOR_RGBA2BGR if fmt == 4 else cv2.COLOR_BGRA2BGR)
    if fmt != 2 or w % 4 or h % 4 or len(b) - fs < w * h: return None
    blk = np.frombuffer(b[-w * h:], np.uint8).reshape(-1, 16)  # mip 0 is stored last; DXT5 = 8 B alpha + 8 B colour per 4x4
    n, sh = len(blk), np.arange(16, dtype=np.uint64)
    c = blk[:, 8:12].copy().view("<u2").astype(np.int32)
    pal0 = np.stack([(c >> 11 & 31) * 255 / 31, (c >> 5 & 63) * 255 / 63, (c & 31) * 255 / 31], -1).astype(np.float32)  # (n, 2, rgb)
    p0, p1 = pal0[:, 0], pal0[:, 1]
    pal = np.stack([p0, p1, (2 * p0 + p1) / 3, (p0 + 2 * p1) / 3], 1)
    ix = (blk[:, 12:16].copy().view("<u4").astype(np.uint64) >> (2 * sh)) & 3
    rgb = pal[np.arange(n)[:, None], ix.astype(np.intp)]  # (n, 16, 3)
    a0, a1 = blk[:, 0].astype(np.float32), blk[:, 1].astype(np.float32)
    k = np.arange(1, 7, dtype=np.float32)
    apal = np.where((a0 > a1)[:, None], np.concatenate([a0[:, None], a1[:, None], ((7 - k) * a0[:, None] + k * a1[:, None]) / 7], 1),
                    np.concatenate([a0[:, None], a1[:, None], ((5 - k[:4]) * a0[:, None] + k[:4] * a1[:, None]) / 5, np.zeros((n, 1)), np.full((n, 1), 255.)], 1))
    abits = np.zeros(n, np.uint64)
    for j in range(6): abits |= blk[:, 2 + j].astype(np.uint64) << np.uint64(8 * j)
    al = apal[np.arange(n)[:, None], ((abits[:, None] >> (3 * sh)) & 7).astype(np.intp)]  # (n, 16)
    # panorama DXT5 portraits are YCoCg: R = Co, G = Cg, B = scale, A = Y
    sc = rgb[..., 2] / (255 / 8) + 1
    co, cg, y = (rgb[..., 0] - 128) / sc, (rgb[..., 1] - 128) / sc, al
    px = np.clip(np.stack([y + co - cg, y + cg, y - co - cg], -1), 0, 255)
    img = px.reshape(h // 4, w // 4, 4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(h, w, 3)
    return np.ascontiguousarray(img[..., ::-1]).round().astype(np.uint8)


def _extract_portraits(d):
    """Top-bar portraits straight from the installed game, incl. alt/persona/arcana variants the CDN doesn't have."""
    import vpk
    pak = vpk.open(str(VPK)); n = 0
    for p in pak:
        m = re.fullmatch(r"panorama/images/heroes/npc_dota_hero_(\w+)_png\.vtex_c", p)
        if m and (img := _vtex_bgr(pak[p].read())) is not None:
            cv2.imwrite(str(d / f"{m.group(1)}.png"), img); n += 1
    return n


def overview_png():
    """Path to the game's own minimap terrain (resource/overviews/dota.txt: pos -9472,9472, 18.5 u/px at 1024 px), decoded
    from the installed VPK and re-extracted when Dota updates; None without a VPK and no cache."""
    out = CACHE / "overview.png"
    if VPK.exists() and (not out.exists() or out.stat().st_mtime < VPK.stat().st_mtime):
        try:
            import vpk
            pak = vpk.open(str(VPK))
            for n in ("materials/overviews/dota_psd.vtex_c", "materials/overviews/dota_tga_d8178876.vtex_c"):
                if n in pak and (img := _vtex_bgr(pak[n].read())) is not None:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    cv2.imwrite(str(out), cv2.resize(img, (1024, 1024), interpolation=cv2.INTER_CUBIC)); break
        except Exception as e: _warn(f"overview texture unreadable ({e})")
    return out if out.exists() else None


def _portraits(block=False):
    """{portrait name (hero or variant, e.g. monkey_king_alt1): BGR image}. Source: the installed VPK (re-extracted when the
    game updates), else 256x144 CDN portraits of base heroes. Loads in a background thread unless block=True. None until ready."""
    with _LOCK:
        if "thread" not in _P:
            def load():
                names, d = hero_names(), CACHE / "portraits_vpk"
                d.mkdir(parents=True, exist_ok=True)
                files = list(d.glob("*.png"))
                if VPK.exists() and (len(files) < 100 or min(f.stat().st_mtime for f in files) < VPK.stat().st_mtime):
                    try: _extract_portraits(d); files = list(d.glob("*.png"))
                    except Exception as e: _warn(f"VPK portraits unreadable ({e}); using CDN portraits")
                imgs = {f.stem: cv2.imread(str(f)) for f in files if _base(f.stem) in names}
                imgs = {k: v for k, v in imgs.items() if v is not None}
                miss = [n for n in names if n not in imgs]
                with ThreadPoolExecutor(16) as ex:  # CDN fallback for any hero the VPK didn't give us
                    for n, i in zip(miss, ex.map(lambda n: _fetch(f"{CDN}{n}.png", CACHE / "portraits" / f"{n}.png"), miss)):
                        if i is not None: imgs[n] = i
                _P["imgs"] = imgs
                if len({_base(k) for k in imgs}) < len(names): _warn(f"portraits: {len({_base(k) for k in imgs})}/{len(names)} heroes available")
            _P["thread"] = threading.Thread(target=load, name="portraits", daemon=True); _P["thread"].start()
    if block: _P["thread"].join()
    return _P.get("imgs")


# ---------- top bar ----------

def _band(img, sx, sy): return img[round(4 * sy):round(22 * sy), round(8 * sx):round(58 * sx)]  # skip slant, facet, colour strip


def _tpl_band(p, sx, sy):
    return _band(cv2.cvtColor(cv2.resize(p, (round(66 * sx), round(36 * sy)), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY), sx, sy)


def _lineup_tpls(sx, sy, P):
    key = (round(sx, 4), round(sy, 4), len(P))
    if _S.get("ltpl_key") != key:
        _S["ltpl"] = {n: _tpl_band(p, sx, sy) for n, p in P.items()}
        _S["ltpl_key"] = key
    return _S["ltpl"]


def _slot_scores(gray, reg, tpls, slots=range(10), pad=3):
    """[len(slots) x len(tpls)] best TM_CCOEFF_NORMED of each portrait band within ±pad px of each slot's band."""
    sx, sy, names = reg["s"], reg["sy"], list(tpls)
    S = np.full((len(slots), len(names)), -1.0, np.float32)
    for i, k in enumerate(slots):
        x, y, _, _ = reg["slots"][k]
        x0, y0 = x + round(8 * sx) - pad, y + round(4 * sy) - pad
        roi = gray[max(0, y0):y0 + round(18 * sy) + 2 * pad + 1, max(0, x0):x0 + round(50 * sx) + 2 * pad + 1]
        for j, n in enumerate(names):
            t = tpls[n]
            if roi.shape[0] >= t.shape[0] and roi.shape[1] >= t.shape[1]:
                S[i, j] = cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))[1]
    return S, names


def identify_lineup(frame, reg, P):
    """-> (heroes[10] short names or None, scores[10]); greedy best-first, each hero once."""
    S, names = _slot_scores(cv2.cvtColor(frame[:reg["slots"][0][1] + reg["slots"][0][3] + 8], cv2.COLOR_BGR2GRAY), reg, _lineup_tpls(reg["s"], reg["sy"], P))
    heroes, scores, used = [None] * 10, [float(r.max()) for r in S], set()
    for k in np.argsort(-S, axis=None):
        i, j = divmod(int(k), len(names))
        if S[i, j] < 0.5: break
        h = _base(names[j])  # template may be a variant portrait (monkey_king_alt1)
        if heroes[i] is None and h not in used: heroes[i], scores[i] = h, float(S[i, j]); used.add(h)
    return heroes, scores


def draft_picks(frame):
    """Hero-selection screen -> {"allies": [5 short names or None], "enemies": [5]} from the portrait slots along the top (allies
    left; enemies right, when the mode shows them). GSI sends no picks here. Geometry measured on 2560x1440 (game 9087545827:
    the 5 ally picks scored 0.77-0.90, empty slots <= 0.50), scaled by frame height.
    ponytail: assumes 16:9 with the UI anchored to both edges; ultrawide needs its own offsets."""
    P = _portraits()
    if not P: return None
    h, w = frame.shape[:2]; s = h / 1440
    tw, th = round(166 * s), round(93 * s)
    if _S.get("dtpl_key") != (tw, th, len(P)):
        _S["dtpl"] = {n: cv2.cvtColor(cv2.resize(p, (tw, th), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)[th // 6:th - th // 6, tw // 6:tw - tw // 6]
                      for n, p in P.items()}
        _S["dtpl_key"] = (tw, th, len(P))
    g, pad = cv2.cvtColor(frame[:round(200 * s)], cv2.COLOR_BGR2GRAY), round(10 * s)
    sat = cv2.cvtColor(frame[:round(200 * s)], cv2.COLOR_BGR2HSV)[..., 1]

    def match(x):
        x0, y0 = x + tw // 6 - pad, round(48 * s) + th // 6 - pad
        # a hover / not-yet-locked pick is drawn grey: 0% of its pixels have S > 90, locked picks >= 21% (pale Crystal Maiden;
        # 14 draft frames, games 9087545827 + 9090603898). Grey templates would still match in gray, so gate on colour first.
        if (sat[y0 + pad:y0 + th - 2 * (th // 6) + pad, x0 + pad:x0 + tw - 2 * (tw // 6) + pad] > 90).mean() < 0.1:
            return None
        roi = g[max(0, y0):y0 + th - 2 * (th // 6) + 2 * pad, max(0, x0):x0 + tw - 2 * (tw // 6) + 2 * pad]
        best = {}
        for n, t in _S["dtpl"].items():
            if roi.shape[0] >= t.shape[0] and roi.shape[1] >= t.shape[1]:
                b = _base(n); best[b] = max(best.get(b, -1), cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))[1])
        top = sorted(best.items(), key=lambda kv: -kv[1])[:2]
        return top[0][0] if top and top[0][1] >= 0.65 and (len(top) < 2 or top[0][1] - top[1][1] >= 0.1) else None
    return {"allies": [match(round((97 + 190 * i) * s)) for i in range(5)],
            "enemies": [match(w - round((97 + 190 * (4 - i)) * s) - tw) for i in range(5)]}


def _slot_sat(frame, reg):
    out = []
    for x, y, w, h in reg["slots"]:
        b = _band(frame[y:y + h, x:x + w], reg["s"], reg["sy"])
        out.append(float(cv2.cvtColor(b, cv2.COLOR_BGR2HSV)[..., 1].mean()) if b.size else 0.0)
    return out


# ---------- minimap ----------

def _mm_tpls(names, icon, bg):
    """{hero: [(half_core, bgr_u8, mask_u8, bgr_f32, mask_f32)] per variant} at icon px.
    half_core: half-res central ~60 % square (nearly all opaque) for the cheap unmasked coarse pass."""
    key = (tuple(sorted(names)), icon, tuple(int(c) // 16 for c in bg))
    if _S.get("mtpl_key") != key:
        out = {}
        for h in names:
            for v in variants(h):
                spr = sprite(v)
                if spr is None: continue
                t = cv2.resize(spr, (icon, icon), interpolation=cv2.INTER_AREA)
                m = t[..., 3] > 128
                if m.sum() < 10: continue
                rgb = t[..., :3].copy(); fill = rgb.copy(); fill[~m] = bg
                hs = max(4, icon // 2); c0 = _core(icon)  # coarse uses the opaque core only: halos/rings/arrows sit outside it
                half = cv2.resize(fill, (hs, hs), interpolation=cv2.INTER_AREA)[c0:hs - c0, c0:hs - c0].copy()
                out.setdefault(h, []).append((half, rgb, m.astype(np.uint8) * 255, rgb.astype(np.float32), m[..., None].astype(np.float32)))
        _S["mtpl"], _S["mtpl_key"] = out, key
    return _S["mtpl"]


def _core(icon): return max(4, icon // 2) // 5  # half-res px trimmed from each side of the coarse template


def _mmatch(win, t):
    """Masked TM_CCOEFF_NORMED (flat windows give NaN/inf -> -1)."""
    r = cv2.matchTemplate(win, t[1], cv2.TM_CCOEFF_NORMED, mask=t[2])
    return np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)


def _premask(crop):
    """Camera box (near-white, unsaturated lines) -> local median, so it does not break icon matches."""
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    m = cv2.dilate(((hsv[..., 1] < 30) & (hsv[..., 2] > 230)).astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    if not m.any(): return crop
    out = crop.copy(); out[m] = cv2.medianBlur(crop, 5)[m]
    return out


def _subpx(R, x, y):
    """Parabolic sub-pixel peak offset in a correlation map."""
    def f(a, b, c): d = a - 2 * b + c; return 0.0 if d >= 0 else float(np.clip(0.5 * (a - c) / d, -0.5, 0.5))
    dx = f(R[y, x - 1], R[y, x], R[y, x + 1]) if 0 < x < R.shape[1] - 1 else 0.0
    dy = f(R[y - 1, x], R[y, x], R[y + 1, x]) if 0 < y < R.shape[0] - 1 else 0.0
    return dx, dy


def _vbank(tpls):
    """Flattened masked zero-mean templates for _verify (rebuilt with the template set)."""
    if _S.get("vbank_key") != _S.get("mtpl_key"):
        ts = [(h, t) for h, v in tpls.items() for t in v]
        M = np.stack([t[4].reshape(-1) for _, t in ts]); cnt = M.sum(1, keepdims=True)
        T = np.stack([t[3] for _, t in ts]); m = np.stack([t[4] for _, t in ts])
        Tz = ((T - (T * m).sum((1, 2), keepdims=True) / cnt[:, :, None, None]) * m).reshape(len(ts), -1)
        _S["vbank"] = ([h for h, _ in ts], M, cnt, Tz, (Tz ** 2).sum(1, keepdims=True))
        _S["vbank_key"] = _S.get("mtpl_key")
    return _S["vbank"]


def _verify(crop, d, tpls, icon):
    """Masked zero-mean colour correlation of the icon crop (±1 px) against every roster template (BLAS).
    Accept if d's hero is best and second-best distance / best distance >= MARGIN (distance = 1 - corr)."""
    x, y = int(round(d["u"] - icon / 2)), int(round(d["v"] - icon / 2))
    if x < 1 or y < 1 or x + icon + 1 > crop.shape[1] or y + icon + 1 > crop.shape[0]: return True  # edge: trust the detector
    heroes, M, cnt, Tz, Tn = _vbank(tpls)
    P = np.stack([crop[y + j:y + j + icon, x + i:x + i + icon] for j in (-1, 0, 1) for i in (-1, 0, 1)]).astype(np.float32).reshape(9, -1, 3)
    var = sum(M @ (P[:, :, c] ** 2).T - (M @ P[:, :, c].T) ** 2 / cnt for c in range(3))
    sc = (Tz @ P.reshape(9, -1).T) / np.sqrt(np.maximum(var, 1e-6) * Tn)
    best = {}
    for h, v in zip(heroes, sc.max(1)): best[h] = max(best.get(h, -1.0), float(v))
    order = sorted(best, key=best.get, reverse=True)
    if order[0] != d["hero"]: return False
    return len(order) < 2 or (1 - best[order[1]]) >= MARGIN * max(1 - best[order[0]], 0.02)


def detect_minimap(crop, tpls, gates, icon):
    """Raw detections [{hero,u,v,score}] in one minimap crop.
    Coarse: unmasked colour CCOEFF at half res (peaks, plus the best peak inside each hero's gate).
    Refine: masked CCOEFF at full res in ±3 px windows. gates {hero: (u, v, radius_px)} = track / GSI prior."""
    crop = _premask(crop)
    H, W = crop.shape[:2]
    half = cv2.resize(crop, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    k = max(3, icon // 4 | 1); ker = np.ones((k, k), np.uint8); c0 = _core(icon)
    cands = []  # (hero, coarse top-left x, y in half px)
    for h, ts in tpls.items():
        R = None
        for hf, *_ in ts:
            if hf.shape[0] < half.shape[0] and hf.shape[1] < half.shape[1]:
                r = cv2.matchTemplate(half, hf, cv2.TM_CCOEFF_NORMED); R = r if R is None else np.maximum(R, r)
        if R is None: continue
        pk = np.argwhere((R >= cv2.dilate(R, ker)) & (R > T_COARSE))
        cands += [(h, px - c0, py - c0) for py, px in sorted(pk, key=lambda p: -R[p[0], p[1]])[:3 if h in ILLUSION else 2]]
        if h in gates:
            gu, gv, r = gates[h]; c = ((gu - icon / 2) / 2 + c0, (gv - icon / 2) / 2 + c0); r /= 2
            x0, y0 = max(0, int(c[0] - r)), max(0, int(c[1] - r))
            sub = R[y0:int(c[1] + r) + 1, x0:int(c[0] + r) + 1]
            if sub.size and sub.max() > 0.2:
                py, px = np.unravel_index(int(sub.argmax()), sub.shape); cands.append((h, x0 + px - c0, y0 + py - c0))
    dets, done = [], set()
    for h, px, py in cands:
        if (h, px, py) in done: continue
        done.add((h, px, py))
        x0, y0 = max(0, 2 * px - 3), max(0, 2 * py - 3); x1, y1 = min(W - icon, 2 * px + 3), min(H - icon, 2 * py + 3)
        if x1 < x0 or y1 < y0: continue
        win = crop[y0:y1 + icon, x0:x1 + icon]
        sc, loc, R = max(((lambda r: (*cv2.minMaxLoc(r)[1::2], r))(_mmatch(win, t)) for t in tpls[h]), key=lambda z: z[0])
        dx, dy = _subpx(R, *loc)
        u, v = x0 + loc[0] + dx + icon / 2, y0 + loc[1] + dy + icon / 2
        g = gates.get(h); gated = g is not None and math.hypot(u - g[0], v - g[1]) <= g[2]
        if sc >= (T_GATE if gated else T_DET): dets.append({"hero": h, "u": u, "v": v, "score": float(sc), "gated": gated})
    out = []  # greedy assign: best score first, one hero per blob, one blob per hero, verified
    for d in sorted(dets, key=lambda d: -d["score"]):
        if any(o["hero"] == d["hero"] for o in out): continue
        near = min((math.hypot(o["u"] - d["u"], o["v"] - d["v"]) for o in out), default=1e9)
        if near < 0.25 * icon or (not d["gated"] and (near < 0.4 * icon or (near < 0.75 * icon and d["score"] < T_OVERLAP))): continue
        d["ovl"] = near < icon  # partly covered: _track holds it longer
        if _verify(crop, d, tpls, icon): out.append(d)
    return out


def _track(raw, t, ppu, icon):
    """2 consistent frames to publish (3 for a >1500 u jump), EMA 0.5; hold a lost hero 1 frame, or up to 2 s if it was
    last seen overlapping another icon (stacked icons in lane are not fog)."""
    T, seen = _S["tracks"], set()
    for d in raw:
        h = d["hero"]; seen.add(h)
        tr = T.setdefault(h, {"cu": d["u"], "cv": d["v"], "n": 0, "t": t, "miss": 0, "pos": None, "score": 0.0})
        gate = 600 * min(max(t - tr["t"], 0.25), 2.0) * ppu + icon
        tr["n"] = tr["n"] + 1 if math.hypot(d["u"] - tr["cu"], d["v"] - tr["cv"]) <= gate else 1
        tr.update(cu=d["u"], cv=d["v"], t=t, miss=0, ovl=d.get("ovl", False))
        jump = tr["pos"] is not None and math.hypot(d["u"] - tr["pos"][0], d["v"] - tr["pos"][1]) > 1500 * ppu
        if tr["n"] >= (3 if jump else 2):
            p = tr["pos"]
            tr["pos"] = (d["u"], d["v"]) if p is None or jump else ((p[0] + d["u"]) / 2, (p[1] + d["v"]) / 2)
            tr["score"] = d["score"]
    for h, tr in T.items():
        if h not in seen:
            tr["miss"] += 1
            if tr["miss"] > 1 and not (tr["ovl"] and t - tr["t"] <= 2.0): tr["pos"], tr["n"] = None, 0
    return [(h, tr) for h, tr in T.items() if tr["pos"] is not None]


# ---------- calibration ----------

def _save_calib():
    """Merge our keys into data/calib.json (other modules may keep their own keys there, e.g. Roshan pits)."""
    try:
        c = json.loads(CALIB.read_text()) if CALIB.exists() else {}
        c = c if isinstance(c, dict) else {}
    except (OSError, ValueError): c = {}
    c.update(frame=_S["size"], geom=list(_S["g"]), ds=list(_S["ds"]), icon_px=_S["icon"], icon_ok=_S["icon_ok"],
             A=None if not _S["affine_ok"] else np.asarray(_S["A"]).tolist(), mm_side=_S["mm"][0], xl=_S["mm"][1])
    try:
        CALIB.parent.mkdir(parents=True, exist_ok=True)
        tmp = CALIB.with_suffix(".tmp"); tmp.write_text(json.dumps(c, indent=1)); os.replace(tmp, CALIB)
    except OSError as e: _warn(f"calib.json not saved: {e}")


def _reset():
    _S.clear()
    _S.update(lineup=None, lineup_t=-1e9, base_sat=[0.0] * 10, low=[0] * 10, dead=[False] * 10, tracks={}, pairs=[],
              n_pairs=0, n_fit=0, A=None, affine_ok=False, icon=None, icon_ok=False, icon_t=-1e9, ds=(1.0, 1.0), size=None,
              g=None, g_t=-1e9, matchid=None, raw=[], gsi_last={}, last=None, mm=minimap_settings(),
              aspect=render_aspect() if ASPECT is None else ASPECT or None)


def _geom(ds=None):
    """Content geometry with the calibrated scale correction ds = (x, y) applied."""
    dx, dy = ds or _S["ds"]; g = _S["g"]
    return (g[0] * dx, g[1] * dy, *g[2:])


def _setup(frame, check=False):
    """(Re)initialise geometry on a new frame size or (check=True) a changed content rect (black bands, scale);
    load data/calib.json if it was made for this size and geometry."""
    H, W = frame.shape[:2]
    if not _S: _reset()
    new = _S.get("size") != [W, H]
    if new or check:
        g = content_geom(frame, _S["aspect"])
        try: c = json.loads(CALIB.read_text()) if CALIB.exists() else {}
        except (OSError, ValueError) as e: c = {}; _warn(f"calib.json ignored: {e}")
        c = c if isinstance(c, dict) else {}
        if g is None and new:  # too dark to tell (loading screen): last calibrated geometry, else HUD fills the frame
            g = tuple(c["geom"]) if c.get("frame") == [W, H] and isinstance(c.get("geom"), list) else (H / 1080, H / 1080, 0, 0, W, H)
        if new or (g is not None and not _same_geom(g, _S["g"])):
            if not new: log.info("HUD geometry changed %s -> %s", _S["g"], g)
            _S.update(size=[W, H], g=g, A=None, affine_ok=False, icon=None, icon_ok=False, ds=(1.0, 1.0), pairs=[], n_pairs=0, n_fit=0, tracks={})
            try:
                if c.get("frame") == [W, H] and _same_geom(tuple(c.get("geom") or ()), g):
                    ds = c.get("ds"); _S.update(ds=tuple(ds) if isinstance(ds, list) and len(ds) == 2 else (1.0, 1.0),
                                                icon=c.get("icon_px"), icon_ok=bool(c.get("icon_ok")))
                    if c.get("A"): _S.update(A=np.array(c["A"], float), affine_ok=True)
            except (ValueError, TypeError) as e: _warn(f"calib.json ignored: {e}")
    reg = regions(W, H, _S["mm"][0], _S["mm"][1], g=_geom())
    if _S["A"] is None: _S["A"] = default_affine(reg["minimap"][2], reg["xl"])
    if not _S["icon"]: _S["icon"] = round(20 * reg["s"] * _S["mm"][2] / 600)  # ponytail: guess (linear in hero_size), auto-calibrated
    return reg


def _calib_icon(crop, hero, x, y, s):
    """Wide search of your own sprite (masked) at 12..40*s px around the predicted icon.
    -> size or None. Accepted only if >= 0.7 and clearly better (+0.05) than the current size."""
    u, v = world_to_px(_S["A"], x, y); r = round((30 if _S["affine_ok"] else 60) * s)
    win = _premask(crop[max(0, int(v) - r):int(v) + r, max(0, int(u) - r):int(u) + r])
    names = [n for n in variants(hero) if sprite(n) is not None]
    sc = {sz: max(float(_mmatch(win, _tpl_at(n, sz)).max()) for n in names) for sz in range(12, min(round(40 * s), min(win.shape[:2]) - 1) + 1)}
    if not sc or not names: return None, 0.0
    best = max(sc, key=sc.get)
    return (best if sc[best] >= 0.7 and sc[best] >= sc.get(_S["icon"], 0) + 0.05 else None), sc[best]


@lru_cache(maxsize=1024)
def _tpl_at(name, sz):
    t = cv2.resize(sprite(name), (sz, sz), interpolation=cv2.INTER_AREA); m = t[..., 3] > 128
    return None, t[..., :3].copy(), m.astype(np.uint8) * 255


def _refine_icon(crop, raw, icon):
    """While uncalibrated: score confident, isolated detections at icon-3..icon+3 px. Every 15 samples move to
    the best mean size; calibrated once the best size is the current one."""
    crop = _premask(crop)
    for d in raw:
        if d["score"] < 0.75 or any(o is not d and math.hypot(o["u"] - d["u"], o["v"] - d["v"]) < icon for o in raw): continue
        row = []
        for sz in range(icon - 3, icon + 4):
            x0, y0 = int(d["u"] - sz / 2) - 3, int(d["v"] - sz / 2) - 3
            win = crop[max(0, y0):y0 + sz + 6, max(0, x0):x0 + sz + 6]
            if win.shape[0] <= sz or win.shape[1] <= sz: break
            row.append(max(float(_mmatch(win, _tpl_at(v, sz)).max()) for v in variants(d["hero"]) if sprite(v) is not None))
        if len(row) == 7: _S["icon_acc"] = _S.get("icon_acc", 0) + np.array(row); _S["icon_n"] = _S.get("icon_n", 0) + 1
    if _S.get("icon_n", 0) >= 15:
        best = icon - 3 + int(np.argmax(_S["icon_acc"])); _S.update(icon_acc=0, icon_n=0)
        if best == icon: _S["icon_ok"] = True
        else: _S.update(icon=best, tracks={})
        _save_calib()


def _d(x): return x if isinstance(x, dict) else {}
def _num(x, default=0.0): return float(x) if isinstance(x, (int, float)) and math.isfinite(x) else default


def _gsi_seen(W):
    """{hero: (x, y)} for heroes whose position GSI's minimap feed gives right now (world merge: src 'gsi' + visible)."""
    out = {}
    for k, e in _d(W.get("heroes")).items():
        e = _d(e); x, y = _num(e.get("x"), None), _num(e.get("y"), None)
        if isinstance(k, str) and e.get("src") == "gsi" and e.get("visible") and not e.get("dead") and x is not None and y is not None:
            out[k.removeprefix("npc_dota_hero_")] = (x, y)
    return out


def _me(W):
    """(short hero name, team, (x, y)) from W["me"]; None for anything missing or malformed."""
    me = _d(W.get("me"))
    h = me.get("hero"); h = h.removeprefix("npc_dota_hero_") if isinstance(h, str) and h else None
    x, y = _num(me.get("x"), None), _num(me.get("y"), None)
    return h, me.get("team") if me.get("team") in ("radiant", "dire") else None, (x, y) if x is not None and y is not None else None


def calibrate(frame_bgr, world_snapshot=None):
    """Top-bar scale check + icon-size search from your own hero; persists data/calib.json. -> summary dict."""
    W = world_snapshot or {}
    with _LOCK:
        if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3: return {"ok": False, "error": "no frame"}
        reg = _setup(frame_bgr, check=True); hero, team, pos = _me(W); out = {"ok": True}
        P = _portraits(block=True) or {}
        if hero in P:  # 1. top-bar: your portrait in your team's slots must score >= 0.8, else search an x/y scale correction
            gray = cv2.cvtColor(frame_bgr[:round(frame_bgr.shape[0] * 0.12)], cv2.COLOR_BGR2GRAY)
            slots = range(5) if team == "radiant" else range(5, 10) if team == "dire" else range(10)
            def score(ds):
                r = regions(*_S["size"], _S["mm"][0], _S["mm"][1], g=_geom(ds))
                return float(_slot_scores(gray, r, {hero: _tpl_band(P[hero], r["s"], r["sy"])}, slots)[0].max())
            sc = score(_S["ds"])
            if sc < 0.8:
                ks = np.linspace(0.8, 1.25, 10)
                sc, ds = max((score((a, b)), (float(a), float(b))) for a in ks for b in ks)
                if sc >= 0.8: _S["ds"] = ds; _S["ltpl_key"] = None; reg = _setup(frame_bgr)
            out.update(slot_score=round(sc, 3), ds=list(_S["ds"]))
        else: out["slot_score"] = None
        if hero and pos:  # 2. icon size from your own icon at its GSI position
            sz, sc = _calib_icon(mm_crop(frame_bgr, reg), hero, *pos, reg["s"])
            if sz: _S.update(icon=sz, icon_ok=False, tracks={})  # _refine_icon confirms it over the next frames
            out.update(icon_score=round(float(sc), 3))
        _S["icon_t"] = time.monotonic()
        _save_calib()
        return out | {"s": reg["s"], "sy": reg["sy"], "geom": list(_S["g"]), "icon_px": _S["icon"], "icon_ok": _S["icon_ok"], "minimap_rect": list(reg["minimap"]),
                      "minimap_side": _S["mm"][0], "xl": _S["mm"][1], "affine_ok": _S["affine_ok"], "pairs": len(_S["pairs"])}


# ---------- per frame ----------

def process(frame_bgr, t, world_snapshot=None):
    t0 = time.perf_counter()
    W = world_snapshot if isinstance(world_snapshot, dict) else {}
    with _LOCK:
        try: out = _process(frame_bgr, time.monotonic() if t is None else float(t), W)
        except Exception:
            _warn("vision.process crashed (logged once)"); log.exception("vision.process")
            out = {"frame_ok": False, "lineup": None, "dead": None, "minimap": [], "minimap_rect": None, "icon_px": None, "affine_ok": False}
    if out["lineup"]: out["lineup"] = {**out["lineup"], "heroes": list(out["lineup"]["heroes"]), "scores": list(out["lineup"]["scores"])}
    out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _process(frame, t, W):
    if not (isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.shape[2] == 3 and frame.shape[0] >= 360 and frame.shape[1] >= 480):
        return {"frame_ok": False, "lineup": _S.get("lineup"), "dead": None, "minimap": [], "minimap_rect": None,
                "icon_px": _S.get("icon"), "affine_ok": bool(_S.get("affine_ok"))}
    if not _S: _reset()
    if (check := t - _S["g_t"] >= 10): _S["g_t"] = t  # re-read the black bands every 10 s
    reg = _setup(frame, check)
    mid = _d(W.get("game")).get("matchid")
    if mid and mid != _S["matchid"]:  # new match: forget lineup, dead baselines, tracks (keep calibration)
        _S.update(matchid=mid, lineup=None, lineup_t=-1e9, base_sat=[0.0] * 10, low=[0] * 10, dead=[False] * 10, tracks={})
    hero, team, pos = _me(W); icon, A = _S["icon"], _S["A"]
    crop = mm_crop(frame, reg); m = crop.shape[0]
    ok = float(crop.std()) > 4
    res = {"frame_ok": ok, "lineup": _S["lineup"], "dead": None, "minimap": [], "minimap_rect": list(reg["minimap"]),
           "icon_px": icon, "affine_ok": _S["affine_ok"], "A": None if A is None else np.asarray(A).round(6).tolist(),
           "size": [frame.shape[1], frame.shape[0]]}  # overlay: world -> minimap crop px -> frame px
    _S["last"] = (frame, reg, [], icon)
    if not ok: return res
    # lineup: per slot; a slot scoring >= T_SLOT is kept, the rest retry every 3 s until all 10 are in (= locked)
    lu = _S["lineup"]
    if not (lu and lu["locked"]) and t - _S["lineup_t"] >= 3 and (P := _portraits()):
        _S["lineup_t"] = t
        hs, sc = identify_lineup(frame, reg, P)
        for i in range(10) if lu else ():  # keep earlier confident slots (e.g. that hero is dead now, or wears an alt portrait)
            if lu["scores"][i] >= T_SLOT and sc[i] < T_SLOT and lu["heroes"][i] not in hs: hs[i], sc[i] = lu["heroes"][i], lu["scores"][i]
        mine = hs[:5] if team == "radiant" else hs[5:] if team == "dire" else hs
        locked = min(sc) >= T_SLOT and None not in hs and (hero is None or hero in mine)
        _S["lineup"] = res["lineup"] = lu = {"heroes": hs, "scores": [round(x, 3) for x in sc], "locked": locked}
    # dead state: band saturation < 0.6 x this slot's alive baseline for 2 frames (real dead slots measured 0.36-0.85)
    # ponytail: misses the odd dead slot that keeps colour; add the white respawn-digit count below the slot once checked on a real snap
    for i, sat in enumerate(_slot_sat(frame, reg)):
        base = _S["base_sat"][i]
        _S["low"][i] = _S["low"][i] + 1 if sat < 0.6 * base else 0
        _S["dead"][i] = _S["low"][i] >= 2
        if not _S["dead"][i]: _S["base_sat"][i] = max(base * 0.999, sat)  # ponytail: decaying max baseline
    res["dead"] = list(_S["dead"])
    # roster
    roster = {h for h, sc, d in zip(lu["heroes"], lu["scores"], _S["dead"]) if h and sc >= T_SLOT and not d} if lu else set()
    if not (lu and lu["locked"]): roster |= {k.removeprefix("npc_dota_hero_") for k in _d(W.get("heroes")) if isinstance(k, str)}
    if hero: roster.add(hero)
    if not roster: return res
    if hero and pos and not _S["icon_ok"] and time.monotonic() - _S["icon_t"] >= 5:  # nothing confident for 5 s: wide own-icon size search
        _S["icon_t"] = time.monotonic()
        sz, _ = _calib_icon(crop, hero, *pos, reg["s"])
        if sz: _S.update(icon=sz, tracks={}); icon = sz
    tpls = _mm_tpls(roster, icon, np.median(crop[::4, ::4].reshape(-1, 3), 0)); ppu = _ppu(A)
    gates = {h: (tr["cu"], tr["cv"], 600 * min(max(t - tr["t"], 0.25), 2.0) * ppu + icon)
             for h, tr in _S["tracks"].items() if h in tpls and tr["miss"] <= 2}
    if hero and pos:
        age = _num(_d(W.get("src_age")).get("gsi_s"))
        gates[hero] = (*world_to_px(A, *pos), icon * (1 if _S["affine_ok"] else 2) + 600 * min(age, 2) * ppu)
    raw = detect_minimap(crop, tpls, gates, icon); _S["raw"] = raw
    if any(d["score"] >= 0.75 for d in raw): _S["icon_t"] = time.monotonic()
    if not _S["icon_ok"]: _refine_icon(crop, raw, icon)
    # affine pairs from every hero GSI's minimap feed shows right now (x,y fresh, icon clean, alone, no TP/blink jump)
    gsi = _gsi_seen(W) | ({hero: pos} if hero and pos else {})
    xy_t = _num(_d(W.get("me")).get("xy_t"), None)  # wall time of the GSI payload; frames are wall-timed too
    fresh = (t - xy_t if xy_t is not None else _num(_d(W.get("src_age")).get("gsi_s"))) <= 0.15  # ponytail: no interpolation, just drop lagged pairs
    for d in raw if fresh else ():
        p = gsi.get(d["hero"]); last = _S["gsi_last"].get(d["hero"])
        if p is None or d["score"] < 0.72 or (last is not None and math.dist(last, p) >= 1000): continue  # glow keeps masked scores ~0.78
        if any(o is not d and math.hypot(o["u"] - d["u"], o["v"] - d["v"]) <= icon for o in raw): continue
        if any(h != d["hero"] and math.dist(q, p) * _ppu(A) <= icon for h, q in gsi.items()): continue  # an undetected icon may sit on it
        _S["pairs"] = (_S["pairs"] + [(*p, d["u"], d["v"])])[-400:]; _S["n_pairs"] += 1
    _S["gsi_last"] = gsi
    if _S["pairs"]:
        P_ = np.array(_S["pairs"]) if _S["pairs"] else np.zeros((0, 4))
        due = len(P_) >= 20 and _S["n_pairs"] - _S["n_fit"] >= (50 if _S["affine_ok"] else 5)  # monotonic: the list is capped
        # spread: >= 3000 u overall and >= 800 u std across the thinnest direction (a straight walk is degenerate)
        if due and np.ptp(P_[:, :2], 0).max() >= 3000 and np.linalg.eigvalsh(np.cov(P_[:, :2].T))[0] >= 800 ** 2:
            _S["n_fit"] = _S["n_pairs"]; A2 = fit_affine(P_)
            if A2 is not None:
                err = np.median(np.hypot(*(P_[:, 2:] - (P_[:, :2] @ A2[:, :2].T + A2[:, 2])).T))
                a, k = A2[:, :2], _ppu(A2)  # sanity: in mm_crop the minimap is ~a similarity (live fit: axis scales within 0.1 %, no rotation)
                sim = abs(abs(a[0, 0]) - abs(a[1, 1])) < 0.1 * k and abs(a[0, 1]) + abs(a[1, 0]) < 0.03 * k
                if err < 3 and sim and 0.7 < k / _ppu(default_affine(m, reg["xl"])) < 1.4:
                    _S.update(A=A2, affine_ok=True); A = A2; res["affine_ok"] = True; _save_calib()
    pub = []
    for h, tr in _track([d for d in raw if d["hero"] != hero], t, _ppu(A), icon):
        x, y = px_to_world(A, *tr["pos"])
        pub.append({"hero": h, "u": round(tr["pos"][0], 1), "v": round(tr["pos"][1], 1), "x": round(x), "y": round(y), "score": round(tr["score"], 3)})
    res["minimap"] = pub
    _S["last"] = (frame, reg, raw, icon)
    return res


# ---------- debug images ----------

def _draw(frame, reg, dets=(), icon=0):
    img = frame.copy()
    for i, (x, y, w, h) in enumerate(reg["slots"]): cv2.rectangle(img, (x, y), (x + w, y + h), SLOT_BGR[i], 1)
    mx, my, mw, mh = reg["minimap"]; cv2.rectangle(img, (mx, my), (mx + mw - 1, my + mh - 1), (0, 255, 255), 1)
    for d in dets:
        c = (int(mx + d["u"]), int(my + d["v"] * mh / mw))
        cv2.circle(img, c, max(3, icon // 2), (0, 255, 0), 1)
        cv2.putText(img, d["hero"][:8], (c[0] - icon // 2, c[1] - icon // 2 - 2), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 0), 1)
    return img


def last_minimap_png():
    last = _S.get("last")
    if not last: return None
    return cv2.imencode(".png", mm_crop(last[0], last[1]))[1].tobytes()


def last_calib_png():
    last = _S.get("last")
    return None if not last else cv2.imencode(".png", _draw(*last))[1].tobytes()


def snap():
    """Grab one real frame: data/snap.png + data/snap_boxes.png (predicted HUD boxes). First thing to run with Dota open."""
    import capture
    f = capture.snap()
    with _LOCK: reg = _setup(f, check=True)
    DATA.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(DATA / "snap.png"), f); cv2.imwrite(str(DATA / "snap_boxes.png"), _draw(f, reg))
    return {"size": [f.shape[1], f.shape[0]], "minimap_rect": list(reg["minimap"]), "slot0": reg["slots"][0], "side": _S["mm"][0], "geom": list(_S["g"])}


# ---------- synthetic frames (spec §8) ----------

def _paste(img, spr, u, v, icon):
    """Alpha-blend a BGRA sprite scaled to `icon` px centred at float (u, v) (bilinear, sub-pixel, like the GPU)."""
    k = icon / spr.shape[1]; ox, oy = math.floor(u - icon / 2) - 1, math.floor(v - icon / 2) - 1
    M = np.float32([[k, 0, 0.5 * k + (u - icon / 2 - ox) - 0.5], [0, k, 0.5 * k + (v - icon / 2 - oy) - 0.5]])
    sz = icon + 3; w = cv2.warpAffine(spr, M, (sz, sz), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))
    x0, y0, x1, y1 = max(ox, 0), max(oy, 0), min(ox + sz, img.shape[1]), min(oy + sz, img.shape[0])
    if x1 <= x0 or y1 <= y0: return
    sub = w[y0 - oy:y1 - oy, x0 - ox:x1 - ox].astype(np.float32); a = sub[..., 3:] / 255
    img[y0:y1, x0:x1] = (sub[..., :3] * a + img[y0:y1, x0:x1] * (1 - a)).astype(np.uint8)


def _synth_bg(m, s, rng):
    p = DATA / "minimap_bg.png"
    if p.exists() and (bg := cv2.imread(str(p))) is not None: return cv2.resize(bg, (m, m), interpolation=cv2.INTER_AREA)
    bg = np.full((m, m, 3), (0x2b, 0x3a, 0x2b), np.float32) + rng.normal(0, 5, (m, m, 1))
    bg = np.clip(cv2.GaussianBlur(bg, (0, 0), 1.5), 0, 255).astype(np.uint8)
    q = lambda a, b: (round(a * m), round(b * m))
    for pts in ([(0.1, 0.9), (0.1, 0.1), (0.9, 0.1)], [(0.1, 0.9), (0.9, 0.9), (0.9, 0.1)], [(0.1, 0.9), (0.9, 0.1)]):
        cv2.polylines(bg, [np.int32([q(*p) for p in pts])], False, (70, 95, 85), max(1, round(2 * s)))
    cv2.line(bg, q(0.05, 0.3), q(0.7, 0.95), (110, 80, 45), max(2, round(5 * s)))  # river
    for (a, b), c in [((0.1, 0.9), (60, 160, 60)), ((0.9, 0.1), (50, 50, 170))]:   # ancients
        cv2.rectangle(bg, q(a - 0.03, b - 0.03), q(a + 0.03, b + 0.03), c, -1)
    for _ in range(18):  # towers
        a, b = rng.uniform(0.1, 0.9, 2); c = (60, 160, 60) if a + (1 - b) < 1 else (50, 50, 170)
        cv2.rectangle(bg, q(a, b), (q(a, b)[0] + round(4 * s), q(a, b)[1] + round(4 * s)), c, -1)
    return bg


def _synth_frames(n, size=(2560, 1440), side="right", seed=0, icon=None, degrade=True, arrows=False, lineup=None, real=False, warp=None):
    """Yield (frame_bgr, ground_truth) for a moving 10-hero sequence (4 fps).
    degrade: blur 0.6, JPEG 70, fog half-plane, camera box, forced 50 % overlap. arrows: team ring + facing arrow
    per icon (a guess at dota_minimap_primary_option 0; the real look is UNVERIFIED).
    lineup: 10 fixed heroes, me = slot 0, enemies fogged (not on the minimap), nobody dies; pairs with tools.py sim.
    real: harder and closer to a game: true map bounds = entity-lump prior (not the default guess), heroes clustered
    in 3 lanes, slow own hero (75 u/frame), team-colour glow behind every icon.
    warp=(W, H, oy): render at `size`, stretch to W x H, shift down oy px (black band on top, bottom clipped), like the live
    macOS window; ground truth (u, v, A, icon) is then in mm_crop() coordinates of that frame."""
    rng = np.random.default_rng(seed)
    Wf, Hf = size; reg = regions(Wf, Hf, side); s = reg["s"]; mx, my, m, _ = reg["minimap"]
    icon = icon or round(22 * s); A = default_affine(m)
    if real: k = m / 244; ax, ay = 260 * k / 16896, 260 * k / 17920; A = np.array([[ax, 0, -8 * k + ax * 8448], [0, -ay, -8 * k + ay * 8448]])
    P = _portraits(block=True) or {}
    pool = [h for h in hero_names() if h in P and sprite(h) is not None]
    if len(pool) < 10: raise RuntimeError(f"synth needs 10 heroes with portrait+sprite, have {len(pool)} (offline?)")
    heroes = list(lineup) if lineup else [str(h) for h in rng.choice(pool, 10, replace=False)]
    me = 0 if lineup else int(rng.integers(10)); others = [i for i in range(1, 5)] if lineup else [i for i in range(10) if i != me]
    spr = [sprite(v) for v in (str(rng.choice(variants(h))) if rng.random() < 0.3 else h for h in heroes)]
    pos = rng.uniform(-6500, 6500, (10, 2)) if not real else np.array([[-5500, 5000], [600, 600], [5200, -5800]])[np.arange(10) % 3] + rng.normal(0, 700, (10, 2))
    ang = rng.uniform(0, 2 * np.pi, 10)
    vel = np.c_[np.cos(ang), np.sin(ang)] * np.where(np.arange(10) == me, *((75, 25) if real else (250, 120)))[:, None]  # u per frame
    occ_a, occ_b = (int(i) for i in rng.choice(others, 2, replace=False))
    dead_rng = {} if lineup else {int(i): (lo, lo + 12) for i, lo in zip(rng.choice([i for i in others if i not in (occ_a, occ_b)], 2, replace=False), (6, 14))}
    bg = _synth_bg(m, s, rng); band_h = round(44 * s)
    for i in range(n):
        ang += rng.normal(0, 0.3, 10); sp = np.hypot(*vel.T)[:, None]; vel = np.c_[np.cos(ang), np.sin(ang)] * sp
        pos += vel; out = np.abs(pos) > 7000; ang[out.any(1)] += np.pi; pos = np.clip(pos, -7000, 7000)
        dead = [lo <= i < hi for lo, hi in (dead_rng.get(k, (0, 0)) for k in range(10))]
        fog = [bool(lineup) and k >= 5 for k in range(10)]
        uv = [world_to_px(A, *p) for p in pos]
        uv[occ_b] = (uv[occ_a][0] + icon / 2, uv[occ_a][1]); pos[occ_b] = px_to_world(A, *uv[occ_b])  # forced 50 % overlap
        mm = bg.copy()
        for _ in range(25):  # creeps
            c = rng.integers(2, m - 4, 2); cv2.rectangle(mm, tuple(map(int, c)), tuple(map(int, c + max(2, round(2 * s)))), (60, 170, 60) if rng.random() < 0.5 else (60, 60, 190), -1)
        order = [k for k in range(10) if k not in (occ_a, occ_b) and not dead[k] and not fog[k]] + [occ_a, occ_b]
        for k in order:
            if arrows:
                c = (90, 200, 90) if (k < 5) == (me < 5) else (60, 60, 220); u, v = uv[k]; a = ang[k]
                tip = (u + 0.75 * icon * np.cos(a), v - 0.75 * icon * np.sin(a)); l, r = a + 2.6, a - 2.6
                cv2.fillPoly(mm, [np.int32([tip, (u + icon / 2 * np.cos(l), v - icon / 2 * np.sin(l)), (u + icon / 2 * np.cos(r), v - icon / 2 * np.sin(r))])], c)
                cv2.circle(mm, (int(u), int(v)), icon // 2, c, max(1, icon // 14))
            if real:  # soft team-colour halo, 50 % at the centre
                gl = np.zeros_like(mm, np.float32); cv2.circle(gl, (int(uv[k][0]), int(uv[k][1])), int(icon * 0.75), (60, 200, 60) if (k < 5) == (me < 5) else (40, 40, 220), -1)
                gl = cv2.GaussianBlur(gl, (0, 0), icon / 5); a = gl.max(2, keepdims=True) / 440
                mm[:] = np.clip(mm * (1 - a) + gl * (a > 0), 0, 255).astype(np.uint8)
            _paste(mm, spr[k], *uv[k], icon)
        cover = {k: max([0.0] + [max(0, icon - abs(uv[k][0] - uv[j][0])) * max(0, icon - abs(uv[k][1] - uv[j][1])) / icon ** 2
                                  for j in order[order.index(k) + 1:]]) for k in order}
        if degrade:
            th = rng.uniform(0, 2 * np.pi); yy, xx = np.mgrid[:m, :m]   # fog: darken a random half-plane 40 %
            half = (xx - m / 2) * np.cos(th) + (yy - m / 2) * np.sin(th) > rng.uniform(-m / 4, m / 4)
            mm[half] = (mm[half] * 0.6).astype(np.uint8)
            cx, cy = rng.uniform(0.15 * m, 0.85 * m, 2); w2, h2 = 0.13 * m, 0.09 * m   # camera trapezoid
            cv2.polylines(mm, [np.int32([(cx - w2 * 1.2, cy - h2), (cx + w2 * 1.2, cy - h2), (cx + w2, cy + h2), (cx - w2, cy + h2)])], True, (245, 245, 245), max(1, round(s)))
        f = np.zeros((Hf, Wf, 3), np.uint8); f[:band_h] = (28, 32, 36)
        for k, (x, y, w, h) in enumerate(reg["slots"]):
            p = cv2.resize(P[heroes[k]], (w, h), interpolation=cv2.INTER_LINEAR)
            if dead[k]: p = cv2.cvtColor(cv2.addWeighted(cv2.cvtColor(p, cv2.COLOR_BGR2GRAY), 0.6, np.full((h, w), 0x89, np.uint8), 0.4, 0), cv2.COLOR_GRAY2BGR)
            f[y:y + h, x:x + w] = p; f[:y, x:x + w] = SLOT_BGR[k]
            cv2.rectangle(f, (x + 2, y + h - round(12 * s)), (x + round(12 * s), y + h - 2), tuple(int(c) for c in rng.integers(0, 255, 3)), -1)  # facet badge
        f[my:my + m, mx:mx + m] = mm
        if degrade:
            f = cv2.GaussianBlur(f, (0, 0), 0.6)
            f = cv2.imdecode(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 70])[1], cv2.IMREAD_COLOR)
        gt = {"size": [Wf, Hf], "side": side, "icon_px": icon, "A": A.tolist(), "minimap_rect": [mx, my, m, m], "slots": heroes, "dead": dead,
              "me": {"hero": heroes[me], "team": "radiant" if me < 5 else "dire", "x": float(pos[me][0]), "y": float(pos[me][1])},
              "heroes": {heroes[k]: {"u": uv[k][0], "v": uv[k][1], "x": float(pos[k][0]), "y": float(pos[k][1]), "slot": k,
                                     "drawn": not dead[k] and not fog[k], "occluded": cover.get(k, 0) > 0.25} for k in range(10)}}
        if warp:
            W2, H2, oy = warp; kx, ky = W2 / Wf, H2 / Hf
            f2 = np.zeros((H2, W2, 3), np.uint8); f2[oy:] = cv2.resize(f, (W2, H2), interpolation=cv2.INTER_LINEAR)[:H2 - oy]; f = f2
            sy = H2 / 1080; r2 = regions(W2, H2, side, g=(sy * (W2 / H2) / (Wf / Hf), sy, 0, oy, W2, H2)); rx, ry, rw, rh = r2["minimap"]
            A2 = np.vstack([kx * A[0] + [0, 0, kx * mx - rx], ky * rw / rh * A[1] + [0, 0, (my * ky + oy - ry) * rw / rh]])
            for g in gt["heroes"].values():
                Y = (my + g["v"]) * ky + oy
                g.update(u=(mx + g["u"]) * kx - rx, v=(Y - ry) * rw / rh, drawn=g["drawn"] and Y < H2, occluded=g["occluded"] or Y + icon * ky / 2 > H2)
            gt.update(size=[W2, H2], icon_px=round(icon * rw / m), A=A2.tolist(), minimap_rect=[rx, ry, rw, rh])
        yield f, gt


def synth(out_dir, n=200, size=(2560, 1440), side="right", seed=0, lineup=None):
    """Write synthetic frames NNN.png + NNN.json ground truth (spec §8)."""
    d = Path(out_dir); d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("[0-9][0-9][0-9].*"): f.unlink()  # a smaller --n must not leave old frames in the loop
    for i, (f, gt) in enumerate(_synth_frames(n, size, side, seed, lineup=lineup)):
        cv2.imwrite(str(d / f"{i:03}.png"), f); (d / f"{i:03}.json").write_text(json.dumps(gt))
    return n


# ---------- self-check (spec §8 items 1, 2, 3, 6) ----------

def _eval_synth(n, size, side, seed, degrade=True, arrows=False, real=False, out=None):
    global CALIB
    old = CALIB; CALIB = Path(tempfile.mkdtemp()) / "calib.json"  # never touch the real calibration
    try:
        with _LOCK: _reset(); _S["mm"] = (side, False, 600); _S["aspect"] = size[0] / size[1]
        hit = tot = occ_hit = occ_tot = wrong = dead_err = dead_n = 0; errs, ms, lineup = [], [], None
        prev_dead = []
        for i, (f, gt) in enumerate(_synth_frames(n, size, side, seed, degrade=degrade, arrows=arrows, real=real, warp=out)):
            team = gt["me"]["team"] == "radiant"  # GSI's minimap feed shows your allies (+ visible enemies)
            Wd = {"me": gt["me"], "game": {"matchid": "synth"}, "heroes": {h: {"x": g["x"], "y": g["y"], "src": "gsi", "visible": True}
                  for h, g in gt["heroes"].items() if g["drawn"] and (g["slot"] < 5) == team}}
            if i == 0: cal = calibrate(f, Wd)
            r = process(f, i / 4, Wd); ms.append(r["ms"]); lineup = r["lineup"]
            icon = gt["icon_px"]
            for d in _S["raw"]:
                g = gt["heroes"].get(d["hero"])
                if not g or not g["drawn"] or math.hypot(d["u"] - g["u"], d["v"] - g["v"]) > icon / 2: wrong += 1
            for h, g in gt["heroes"].items():
                if not g["drawn"]: continue
                d = next((d for d in _S["raw"] if d["hero"] == h and math.hypot(d["u"] - g["u"], d["v"] - g["v"]) <= icon / 2), None)
                if g["occluded"]: occ_tot += 1; occ_hit += d is not None
                else:
                    tot += 1; hit += d is not None
                    if d: errs.append(math.hypot(d["u"] - g["u"], d["v"] - g["v"]))
            prev_dead.append(gt["dead"])
            if i >= 2 and r["dead"] is not None:  # skip the 2-frame confirmation latency after a change
                for k in range(10):
                    if prev_dead[-1][k] == prev_dead[-2][k] == prev_dead[-3][k]: dead_n += 1; dead_err += r["dead"][k] != gt["dead"][k]
        lu_ok = sum(a == b for a, b in zip(lineup["heroes"], gt["slots"])) if lineup else 0
        A_err = max(math.dist(world_to_px(_S["A"], x, y), world_to_px(np.array(gt["A"]), x, y)) for x in (-7000, 7000) for y in (-7000, 7000))
        return {"lineup": lu_ok, "locked": bool(lineup and lineup["locked"]), "recall": hit / max(tot, 1), "occ_recall": occ_hit / max(occ_tot, 1),
                "med_err": float(np.median(errs)) if errs else 99.0, "wrong": wrong, "dets": tot, "dead_acc": 1 - dead_err / max(dead_n, 1),
                "ms_p50": float(np.median(ms)), "ms_p95": float(np.percentile(ms, 95)), "ms_max": float(np.max(ms)), "icon": _S["icon"], "icon_true": gt["icon_px"],
                "affine_ok": _S["affine_ok"], "affine_err_px": A_err, "pairs": len(_S["pairs"]), "calib": cal, "reg": regions(*_S["size"], side, g=_geom())}
    finally:
        CALIB = old
        with _LOCK: _reset()


def selfcheck():
    """-> [(name, ok, detail)]"""
    res = []
    r1, r2 = regions(1920, 1080, "left"), regions(2560, 1440, "right")
    r3 = regions(1728, 1117, "left")  # hud report table
    ok = (r1["slots"][0] == (544, 4, 66, 36) and r1["minimap"] == (0, 836, 244, 244) and r2["minimap"] == (2235, 1115, 325, 325)
          and r3["slots"][0] == (434, 4, 68, 37) and r3["slots"][5][0] == 969 and r3["minimap"] == (0, 865, 252, 252))
    res.append(("vision.regions", ok, f"1080p slot0={r1['slots'][0]} mm={r1['minimap']}; 1440p right mm={r2['minimap']}"))
    f = np.full((1440, 2560, 3), 90, np.uint8); f[:43] = 0  # live macOS: 1728x1117 render stretched to 2560x1440, 43 px band, bottom cut
    g = content_geom(f, 1728 / 1117); r4 = regions(2560, 1440, "right", g=g)
    ok = g is not None and r4["slots"][0][:2] == (643, 48) and r4["slots"][5][0] == 1436 and r4["minimap"] == (2186, 1158, 374, 325) \
        and content_geom(np.zeros((1440, 2560, 3), np.uint8)) is None and content_geom(np.full((1080, 1920, 3), 90, np.uint8)) == (1.0, 1.0, 0, 0, 1920, 1080)
    res.append(("vision.content_geom", ok, f"live 2560x1440 + 43 px band: geom={g} slot0={r4['slots'][0]} slot5={r4['slots'][5]} mm={r4['minimap']} "
                                           "(measured live: slot0 x 644 y 49, slot5 x 1435)"))
    try:
        spr = load_sprites(); p = spr.get("pudge"); n = int((p[..., 3] > 128).sum()) if p is not None else 0
        res.append(("vision.sprites", 400 <= n <= 800 and len(hero_names()) >= 120,
                    f"{len(spr)} sprites, {len(hero_names())} heroes, pudge opaque px={n} (src={'VPK' if VPK.exists() else 'cache/CDN'})"))
    except Exception as e: res.append(("vision.sprites", False, repr(e)))
    rng = np.random.default_rng(1)  # 6. affine recovery from noisy pairs (+10 % outliers)
    At = default_affine(325) @ np.array([[np.cos(0.02), -np.sin(0.02), 0], [np.sin(0.02), np.cos(0.02), 0], [0, 0, 1]]) + [[0, 0, 4], [0, 0, -3]]
    w = rng.uniform(-7500, 7500, (60, 2)); px = w @ At[:, :2].T + At[:, 2] + rng.normal(0, 0.7, (60, 2)); px[:6] += 25
    Af = fit_affine(np.c_[w, px]); g = [(x, y) for x in (-8000, 0, 8000) for y in (-8000, 0, 8000)]
    e = max(math.dist(world_to_px(Af, *q), world_to_px(At, *q)) for q in g) if Af is not None else 99
    res.append(("vision.fit_affine", e <= 1.0, f"max grid error {e:.2f} px (60 pairs, sigma 0.7 px, 6 outliers)"))
    runs = (("synth 2560x1440 right", 100, (2560, 1440), "right", False, False), ("synth 1728x1117 left", 40, (1728, 1117), "left", False, False),
            ("stress 2560 rings+arrows", 40, (2560, 1440), "right", True, False), ("realistic 2560 lanes+glow+true bounds", 80, (2560, 1440), "right", False, True),
            ("1728x1117 stretched to 2560x1440, 43px band, bottom cut", 60, (1728, 1117), "right", False, False, (2560, 1440, 43)))
    for name, n, size, side, arrows, real, *out in runs:
        try:
            m = _eval_synth(n, size, side, seed=7, arrows=arrows, real=real, out=out[0] if out else None)
            ok = (m["wrong"] <= 0.01 * m["dets"] and m["recall"] >= 0.7) if real else (m["wrong"] == 0 and m["recall"] >= 0.85) if arrows else \
                (m["lineup"] == 10 and m["recall"] >= 0.9 and m["med_err"] <= 2 and m["wrong"] == 0)
            res.append((f"vision.{name}", ok, (f"{n} degraded frames: lineup {m['lineup']}/10 locked={m['locked']}, minimap recall {m['recall']:.3f} "
                        f"(n={m['dets']} visible icons), occluded(>25% covered) recall {m['occ_recall']:.2f}, median err {m['med_err']:.2f}px, "
                        f"wrong IDs {m['wrong']}, dead acc {m['dead_acc']:.3f}, icon {m['icon']}px (true {m['icon_true']}), " +
                        (f"affine fitted, corner err {m['affine_err_px']:.2f}px" if m["affine_ok"] else "affine not fitted yet (default mapping)") + f" from {m["pairs"]} GSI pairs"
                        + (" [pass = 0 wrong IDs + recall; ring/arrow look is a guess]" if arrows else " [pass = recall >= 0.7, wrong IDs <= 1 %; stacked lane icons stay unseen]" if real else ""))))
            if out: res[-1] = (res[-1][0], res[-1][1], res[-1][2] + f"; geom {m['calib'].get('geom')} mm rect {m['reg']['minimap']}")
            if not arrows and not real and not out: res.append((f"vision.speed {size[0]}x{size[1]}", m["ms_p50"] < 120, f"process() p50 {m['ms_p50']:.0f} ms, p95 {m['ms_p95']:.0f} ms, max {m['ms_max']:.0f} ms (max = first frame: templates + lineup)"))
        except Exception as e: res.append((f"vision.{name}", False, repr(e)))
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    rs = selfcheck()
    for name, ok, detail in rs: print("PASS" if ok else "FAIL", name, "-", detail)
    raise SystemExit(0 if all(ok for _, ok, _ in rs) else 1)
