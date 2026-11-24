# Dota 2 HUD geometry and hero identification from screenshots (research report)

Bottom line: the HUD layout depends only on the captured frame's height (`s = H/1080`) and centre (`C = W/2`). I took the constants below from Valve's own current Panorama CSS rather than measuring them. They agree with two independent open-source projects to within about 1–2 px. A few cheap self-checks keep them honest when the HUD scale differs. Nothing in this pipeline needs AI; it uses OpenCV and numpy only.

I did not take a screenshot of your Dota session, even though the game was running. That should be an explicit step: grab one frame and confirm the rectangles below.

## 1. Where the numbers come from

- **Valve's HUD stylesheets.** Decompiled copies are at `spirit-bear-productions/dota_vpk_updates`, last commit 2026-11-05 ("Client 6972", decompiled with Source 2 Viewer 12.0). The files are `panorama/styles/hud/hud_reborn.css`, `dota_hud_top_bar.css`, `panorama/layout/hud/dota_hud_top_bar_player.xml` and `dotastyles.css`. The compiled `.vcss_c` files are also in your local `pak01_dir.vpk`, but in binary form, so I used the decompiled copies.
- **How Panorama scales.** Its pixel units are relative to a screen 1080 units high. The `dota2-mod-manager` source says so directly ("the screen is 1080 high to it"). So `s = H/1080` and `C = W/2`, where H is the height in pixels of the frame you capture.
- **Independent checks:**
  - `mdiller/twitchclipmatchfinder/finder.py`, a working top-bar hero matcher. Its slot positions are relative to `herobar_width = 0.8*H`, centred: `[30.44,156.44,282.44,408.44,534.44,1078.77,1205.11,1331.11,1456.44,1582.77]` at H=2160, with templates of `round(128*H/2160)` px and a crop of `8/8/8/32` px per 128. Converted, these match the CSS within about 1 px at 1080p.
  - `kimyong95/dota-autokey`: the minimap's top edge is at y=1670 on a 4K screen. The formula gives 1672.
  - I drew the CSS rectangles onto real 1080p frames from mdiller's test set and they line up with the portraits.

## 2. Top bar geometry (in 1080-height units; multiply by `s`)

| Element | Rule (x relative to C) |
|---|---|
| Whole top bar | 1640 wide, centred, starts at y=0. Each team half is 820 wide. Your window is 1728/1.0343 = 1671 units wide, so it fits. |
| Clock (`#TimeOfDay`) | 80×45, centred. Digits are about y 24–35 at 1080p. Dota2-Vis reads them from crop x 900–1020, y 0–40. |
| Team scores | 58 wide, 4 margin. Radiant score spans C−102…C−44, Dire score C+44…C+102. |
| **Radiant portrait k** (k=0..4, left to right) | x = **C + (−416 + 62k)**, width **66**, y **4…40** (36 tall) |
| **Dire portrait k** | x = **C + (102 + 62k)**, width **66**, y **4…40** |
| Player-colour strip | y **0…4**, full slot width |
| Ult indicator | 38×20 box centred on the slot, y 28…48. The 18 px icon sits at its bottom centre. |
| Respawn timer (when dead) | full slot width, y **40…66**, white 16 px bold digits |
| Health/mana bars | 60×8 at y 28–36 and 36–44. Hidden by default; whether they show for allies in a normal match is UNVERIFIED. |

These are the same in 16:9, 16:10 and 4:3. The `.AspectRatio4x3` rule only sets each team half to 50%, which equals 820.

Computed rectangles (x, y, w, h in pixels):

| Frame | s | R0 | R4 | D0 | D4 | Minimap |
|---|---|---|---|---|---|---|
| 1920×1080 | 1.000 | 544,4,66,36 | 792,… | 1062,… | 1310,… | 0,836,244,244 |
| **1728×1117** (your window, 1:1 capture) | 1.0343 | **434,4,68,37** | 690 | 969 | 1226 | **0,865,252,252** |
| 3456×2234 (2× Retina capture) | 2.0685 | 867,8,137,74 | 1380 | 1939 | 2452 | 0,1729,505,505 |
| 2560×1600 (16:10) | 1.4815 | 664,6,98,53 | 1031 | 1431 | 1799 | 0,1239,361,361 |

A note on capturing on the Mac: Dota is not rendering in high-DPI, so it draws at 1728×1117 and macOS upscales that 2×. Capture at 1728×1117 (for example, set `SCStreamConfiguration.width/height` in ScreenCaptureKit) to get real pixels and a quarter of the work. Whatever you capture, always compute `s` from the frame's own height.

## 3. Minimap geometry (from `hud_reborn.css`)

- `#minimap_block` is 244×244, bottom-left, with no margin (`vertical-align: bottom`). Extra-large minimap makes it 280. There is also a `.MinimapExtraExtraLarge` rule at 420; whether the settings menu offers that size is UNVERIFIED.
- The actual map panel inside it is 260×260 (296 extra-large, 444 extra-extra-large), centred and clipped. So the map texture extends 8 units past each edge of the visible box.
- With the HUD flipped, the minimap's x becomes `W − 244s`. The top bar does not move.
- The glyph/scan buttons sit immediately to the right (x 244…318, 280 tall). Keep them out of the crop.
- Mapping world coordinates to minimap pixels: the visible box spans [0, 244s]. The full map panel is offset by −8s and is 260s wide. A starting guess for the world range is −8288…8288 on both axes, taken from `Noxville/datdota-frontend` LiveMinimap.tsx ("7.39 minimap image"). That range is UNVERIFIED, so fit it instead (section 6).
  - `u = −8s + 260s·(x+8288)/16576`
  - `v = (H−244s) − 8s + 260s·(8288−y)/16576`
- Each slot's player colour is defined in `dotastyles.css` (verified):

| Slot | Radiant colour | Slot | Dire colour |
|---|---|---|---|
| 0 | `#3375FF` | 5 | `#FE86C2` |
| 1 | `#66FFBF` | 6 | `#A1B447` |
| 2 | `#BF00BF` | 7 | `#65D9F7` |
| 3 | `#F3F00B` | 8 | `#008321` |
| 4 | `#FF6B00` | 9 | `#A46900` |

  The top bar's 4 px colour strip shows the same colour, so you can tie colour → slot → hero without any lookup.

Settings to recommend:
- **`dota_minimap_show_hero_icon 1`** shows hero faces on the minimap. Match them against the CDN 32×32 icons (`heroimagestyle="icon"`). That the minimap uses exactly that art is UNVERIFIED but likely.
- Keep `dota_minimap_simple_background 1`. A flat background makes it easy to subtract a median "empty" frame.
- Keep `dota_minimap_simple_colors 0`. Simple colours turn everything plain green or red and lose the per-player colour.
- `dota_minimap_hero_size` changes icon size. The pixel size at 600 is UNVERIFIED; measure it once.
- `dota_minimap_hide_background 1` is the most reliable option for detection, but it is optional because it hurts you as a player.

## 4. Identifying the 10 heroes

**Hero list:**
- OpenDota `GET https://api.opendota.com/api/heroes`: 127 heroes, highest id 155 (Largo).
- `odota/dotaconstants` `build/heroes.json` has the same heroes plus `img` and `icon` paths.

**Images** (verified, served from `https://cdn.cloudflare.steamstatic.com`):

| Path | Size | Use |
|---|---|---|
| `/apps/dota2/images/dota_react/heroes/<short>.png` | 256×144 | top-bar art (same image `DOTAHeroImage` uses by default) |
| `/apps/dota2/images/dota_react/heroes/icons/<short>.png` | 32×32 | minimap icons |
| `/apps/dota2/images/dota_react/heroes/crops/<short>.png` | 400×250 | not needed |

`<short>` is the hero's `name` with `npc_dota_hero_` removed.

**Things drawn over each portrait** (from `dota_hud_top_bar_player.xml`):
- a slanted opacity mask (`topbar_heroslant_left/right`),
- the facet icon at the bottom-left,
- a rank badge, the player-colour strip, and the dead layer.

So match only the central band. mdiller drops 8/128 of the width on each side and the top, plus the bottom 32/72 rows. The equivalent here is portrait x 8–58 and y 4–22 units.

**Method:**
- For each slot, run `cv2.matchTemplate` with `TM_CCOEFF_NORMED` on greyscale, against all 127 templates plus alternate portraits. Allow a ±3 px search margin around the predicted rectangle.
- Greyscale means a dead (desaturated) portrait still matches.
- Assign heroes greedily, best score first, with each hero used only once.
- That is about 1,300 small template matches per pass, a few milliseconds.
- mdiller accepts a result when score ≥ 0.75 for most slots.
- Perceptual hashing (`imagehash.phash`) works as a quick first filter, but it is weaker than template matching once the overlays above are present.

**Alternate portraits:** some arcanas and personas replace the portrait. mdiller's `data/arcana_links.json` lists the `*_alt1/_alt2/_persona1_png` paths for Lina, Legion Commander, Terrorblade, Techies, Zeus, Shadow Fiend, Phantom Assassin, Crystal Maiden, Pudge, Monkey King, Juggernaut, Io, Spectre and others. Add those as extra templates. They are served by `dotabase.dillerm.io/dota-vpk/panorama/images/heroes/...png`, or can be pulled from the local VPK.

**When to run it:** at strategy time or pre-game, as reported by the game's live data feed (GSI) game state. Retry every few seconds until all 10 scores pass the threshold, because hidden picks show as blanks early on. The lineup is then fixed; recheck only if a score drops.

**Free check:** GSI gives your own hero and team. The portrait in your predicted slot must match your GSI hero with a score above about 0.8. If it doesn't, run the scale search below.

## 5. Per-frame state that's realistic without AI

| Signal | How | Feasibility |
|---|---|---|
| Dead | `.Dead DOTAHeroImage {saturation:0; wash-color:#898989}`. Measure mean HSV saturation of the portrait and compare it with that slot's own alive baseline (some portraits are naturally grey). Confirm with white digit pixels in the y 40–66 band. | Very reliable |
| Respawn seconds | Digit templates in the y 40–66 band (Dota2-Vis does the same for the clock with 9×11 crops). Or Apple Vision `VNRecognizeTextRequest` through pyobjc: free and local, though how fast it is was not measured here. Or compute it from the respawn formula and the death time. | Feasible |
| Ultimate state | Colour of the icon at the slot centre, y 30–48: green = `ult_ready`, blue = `ult_no_mana`, dark = `ult_cooldown`, transparent = not learned (`opacity:0` until `.UltLearned`). Whether enemy ultimates are shown in a normal match is UNVERIFIED (I believe allies only). | Feasible for allies |
| Disconnected | Portrait opacity 0.5 plus a disconnect icon | Feasible |
| Hero level | Not shown in the top bar | Not feasible; estimate from XP curves |
| Enemy missing from map | Not in the top bar. Use the minimap: no icon of that colour for N seconds. | Feasible (minimap) |
| Clock and kill score | **No OCR needed.** GSI `map.clock_time`, `game_time`, `daytime`, `radiant_score`, `dire_score` | Use GSI |

## 6. Calibration that survives HUD-scale differences

1. **Default:** compute `s`, `C` and the minimap rectangle from the frame size. Flip the minimap's x if `dota_hud_flip` is 1; read it from `cfg` once.
2. **Check:** match your own GSI hero's template in your slot. If the score is ≥ 0.8, keep the default.
3. **If it fails:** search the top 6% of the frame at scales `s·[0.8…1.25]` in 15 steps with your hero's template. The best match gives `s'`, and `C'` comes from the slot formula. Save both per window size. This also covers HUD mods that use `ui-scale`. As far as I know (UNVERIFIED) the game itself has no HUD-scale slider.
4. **Minimap:** fit the world-to-pixel mapping from your own hero. Record GSI `hero.xpos/ypos` alongside the detected icon position. Your hero is the one blob that moves when GSI says you moved; on the first frame, you are in the fountain. Fit with least squares, assuming equal scale on both axes and y flipped: `u = a·x + b`, `v = −a·y + c`. The constants in section 3 are only the starting guess.
5. **One-click option:** a "calibrate" button in the dashboard saves a frame and shows the predicted boxes so you can nudge offsets (`dx`, `dy`, `ds`) and save them to JSON. The minimap block has a fixed frame image (`bg_minimap_psd`), so if needed, a single template match of the bottom-left 40×40 corner also confirms the rectangle. Custom HUD skins change the frame (`HUDSkinMinimap` 265×311) but not the map box.

```python
import cv2, numpy as np
def regions(W, H, s=None, C=None, mm=244, flip=False):
    s = s or H / 1080; C = C or W / 2
    xs = [C + (-416 + 62*k)*s for k in range(5)] + [C + (102 + 62*k)*s for k in range(5)]
    slots = [(round(x), round(4*s), round(66*s), round(36*s)) for x in xs]
    m = round(mm*s); minimap = (W - m if flip else 0, H - m, m, m)
    return s, slots, minimap

def prep_template(img_rgb_256x144, s):          # CDN portrait -> matchable band
    t = cv2.resize(img_rgb_256x144, (round(66*s), round(36*s)), interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(t, cv2.COLOR_RGB2GRAY)
    return g[round(4*s):round(22*s), round(8*s):round(58*s)]   # skip slant edges / facet / colour strip

def best_hero(frame_gray, slot, templates, pad=3):   # templates: {hero_id: [gray bands]}
    x, y, w, h = slot
    roi = frame_gray[max(0, y-pad):y+h+pad, max(0, x-pad):x+w+pad]
    return max((cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))[1], hid)
               for hid, ts in templates.items() for t in ts)

def is_dead(frame_bgr, slot, alive_sat):
    x, y, w, h = slot
    sat = cv2.cvtColor(frame_bgr[y:y+h, x:x+w], cv2.COLOR_BGR2HSV)[..., 1].mean()
    return sat < 0.35 * alive_sat                 # ponytail: fixed ratio; tune on real frames

if __name__ == "__main__":
    s, sl, mm = regions(1728, 1117)
    assert sl[0] == (434, 4, 68, 37) and sl[5][0] == 969 and mm == (0, 865, 252, 252)
```

Left out: alternate-portrait download and minimap icon matching. Add them once the top-bar pass works on a real frame from your setup.

## Sources
- Valve HUD CSS (decompiled): https://github.com/spirit-bear-productions/dota_vpk_updates (`panorama/styles/hud/hud_reborn.css`, `dota_hud_top_bar.css`, `panorama/layout/hud/dota_hud_top_bar_player.xml`, `panorama/styles/dotastyles.css`)
- mdiller hero finder: https://github.com/mdiller/twitchclipmatchfinder (`finder.py`, `data/arcana_links.json`)
- Dota2-Vis (YOLO minimap detector, clock digit templates; minimap crop 240×240 and clock crop at 1080p): https://github.com/RicardoRCarvalho/dota2-vis
- "1080-high" scaling statement: https://github.com/dota2modmanager/dota2-mod-manager/blob/HEAD/src/hud-scale.ts
- 4K minimap edge: https://github.com/kimyong95/dota-autokey/blob/HEAD/utils/camera.py
- World range guess: https://github.com/Noxville/datdota-frontend/blob/HEAD/src/components/LiveMinimap.tsx
- Hero data: https://api.opendota.com/api/heroes and https://raw.githubusercontent.com/odota/dotaconstants/master/build/heroes.json
- Portrait CDN (verified 256×144 / 32×32): https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/antimage.png

Working files are in `<scratch>/`:
- `css/` (downloaded CSS and XML)
- `scripts/geom.py` (the geometry calculation)
- `dl/a_top.png` and `dl/b_top.png` (rectangle overlays on real 1080p frames)