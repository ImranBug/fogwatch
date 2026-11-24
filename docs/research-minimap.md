# Dota 2 minimap computer vision: research report (build Nov 05 2026, ClientVersion 6972, patch about 7.41f)

## 0. Facts checked on this Mac, including three corrections to the project context

The most reliable source is the installed game, so I checked its files directly. These come from `game/dota/bin/osx64/libclient.dylib` strings, `pak01_dir.vpk` and `~/Library/Application Support/Steam/userdata/<account-id>/570/local/cfg/user_convars_0_slot0.vcfg`.

1. **The minimap settings in the context are stale.** `dota_minimap_simple_colors`, `dota_minimap_show_hero_icon`, `dota_minimap_always_draw_hero_icons` and `dota_minimap_simple_background` all have help text in the binary saying "No longer used". The current settings are:
   - `dota_minimap_primary_option` and `dota_minimap_secondary_option` (the secondary one applies while Alt is held): "0 = Hero icon with arrow, 1 = Arrow with player colors, 2 = Arrow with team colors, 3 = Hero icon only"
   - `dota_minimap_background_option`: 0 = None, 1 = Simple, 2 = Realistic
   - `dota_minimap_position_option`: "Minimap on Left = 0, Minimap on Right = 1"
   - `dota_hud_flip` is also deprecated. Its help text says "No longer used. Use dota_minimap_position_option instead." This is inferred from where the string sits in the binary.
2. **Your current values:** `primary_option 0`, `secondary_option 1`, `background_option 1`, `hero_size 600`, `hero_scalar false`, `hero_spread false` (spread distance 500), `show_cast_range true` (min radius 1200), `extra_large_minimap 0`.
3. **`dota_minimap_position_option` is "1", which means the minimap is on the RIGHT, not the left.** Please confirm with one screenshot.
4. **Capture scaling.** `video.txt` has `setting.high_dpi "0"` and `fullscreen "0"`. On a Retina panel the capture will be 2x the game's real pixels, so downscale it by 2 with `cv2.INTER_AREA` before matching. Otherwise the templates won't line up with the upscaled pixels.
5. **GSI has a minimap provider.** The binary contains `CGameIntegrationDataRenderer_MiniMap_v1` and keys `minimap_v1`. In antonpup/Dota2GSI each element has Location, Team, UnitName, Image, Rotation and VisionRange. **Test this before building CV.** Add `"minimap" "1"` to the GSI cfg and check whether it carries data while you play. If it does for allied or visible units, much of the CV below becomes unnecessary. Community reports say it only fills in for spectators. That is UNVERIFIED and untested.

## 1. Existing projects

| Project | Technique | Result |
|---|---|---|
| [RicardoRCarvalho/dota2-vis](https://github.com/RicardoRCarvalho/dota2-vis) (SBGames 2026, [arXiv 2606.26970](https://arxiv.org/abs/2606.26970)) | YOLO11 n–x on 240×240 minimaps from 1080p. 2,477 labelled images, 21 classes: 10 player **slots**, 10 clones, plus "Other" for pings. | YOLO11l: P 0.974 / R 0.906 / F 0.939 / mAP50:95 0.729. Code and dataset are "coming soon"; the repo only has some YOLO scripts. |
| [Quinntana/LOL_Minimap_Tracker](https://github.com/Quinntana/LOL_Minimap_Tracker) (LoL, 2026) | **Classical.** HSV picks out the red enemy rim, then portraits are matched with SSIM on the icon **centre only** (so the rim and background don't dominate). Requires an `ssim_margin` over the second-best portrait, at most one observation per champion per frame, N consecutive spatially consistent frames before publishing, and extra frames to confirm large jumps. | The most transferable design. No published accuracy. |
| [may850608-glitch/lol-minimap-yolo](https://github.com/may850608-glitch/lol-minimap-yolo) | YOLO11 trained only on **synthetic** composites. | Synthetic mAP50 ≈ 0.99. Lessons: synthetic frames that are too clean fail on real ones (add rim colour, blur, compression, fog and noise), and **a 10-class model per match beats a 58-class global one**. |
| [farzaa/DeepLeague](https://github.com/farzaa/DeepLeague) (2017, 1.2k★) | YOLOv2 on about 100k LoL frames. | Shows a net works, but needs a big dataset. |
| [PandaScore 2018](https://www.pandascore.co/blog/league-of-legends-getting-champion-coordinates-from-the-minimap-using-deep-learning) | 12×12 grid of per-champion probabilities, with a GAN to generalise. | Grid output. Overkill here. |
| [gist 6e87f605](https://gist.github.com/6e87f605579425d4f3a24976fcb0115f) (Dota) | Unfinished YOLO attempt. | The hard part was making portraits look like minimap icons. That problem doesn't exist here because the game ships the exact sprites. |
| [usama-ghufran/dotaAI](https://github.com/usama-ghufran/dotaAI) (2018) | CNN on the Dota minimap. | Old and undocumented. |

**Conclusion.** No ready-made Dota classical tracker exists. The Quinntana pattern (cheap candidate stage, masked compare with a margin test, temporal confirmation) plus a restriction to the 10 heroes in the match is the right v1. If it falls short, a YOLO11n trained on **synthetic composites from the game's own sprite sheet** is the fallback.

## 2. How heroes render, and which settings to use

From `scripts/minimap_icons.txt` and `scripts/mod_textures.txt` in the installed VPK:
- Hero icons are sprites `minimap_heroicon_npc_dota_hero_<name>`, 32×32 cells in `materials/vgui/hud/minimap_hero_sheet.vmat` (512×512 RGBA8888, uncompressed).
- There are **183 entries**, including variants such as `pudge_persona1`, `invoker_persona1`, `*_alt1/2/3`, `*_carnival`, `arc_warden_tempest_double` and `brewmaster_fire/storm/earth/void`. Templates must include these variants.
- Other icons: `minimap_herocircle`, `minimap_herocircle_self`, `minimap_herocircle_self_highlight`, `minimap_heroinvis`, `minimap_glowenemy`, `minimap_creep`, `minimap_ward_obs`, `minimap_ping_teleporting`, `minimap_danger`, `minimap_death`, `minimap_roshancamp`, `minimap_tormentor`, `minimap_watcher`, and about 60 ping icons.
- Panorama arrow textures, which I decoded and viewed:
  - `minimap_hero_arrow` (128px): a glowing chevron, tinted at runtime.
  - `minimap_hero_enemy` (64): a white chevron arrow.
  - `minimap_hero_team` (64): a white circle with a pointer, tinted by colour.
  - `minimap_hero_self` (64): a **blue-filled** circle with a pointer.
- **Exactly how these combine in each mode is UNVERIFIED.** One real screenshot will settle it.

**Recommended settings for the classical pipeline:**

```
dota_minimap_primary_option 3        // Hero icon only: identity from icon, no arrow clutter (use 0 if you want facing)
dota_minimap_background_option 1     // Simple (already set): flat, high-contrast background
dota_minimap_hero_scalar 0           // keep icon pixel size constant (single template scale)
dota_minimap_show_cast_range 0       // removes 1200-radius circles drawn over icons
dota_hud_extra_large_minimap 1       // more pixels per icon (optional; recalibrate)
dota_minimap_hero_size 800-1000      // bigger icons classify better; more overlap. Tune empirically
dota_minimap_hero_spread 0 (default) // try 1: separates stacked icons at ≤500u position error
```

What each question needs:
- **(a) Ally vs enemy:** don't read it from minimap colour. Use identity plus the roster. The team for each hero comes from GSI for your own hero, and for the others from one-time template matching of the 10 top-bar portraits at game start (Radiant on the left, Dire on the right). If you choose an arrow-only mode, option 2 ("team colors") makes ally/enemy a trivial HSV threshold, but you lose identity.
- **(b) Which hero:** an icon mode (0 or 3) is required.
- **(c) Position:** the icon centre, after calibration (section 4).

## 3. Templates: source, size, speed (measured on this M3 Pro, OpenCV 5.0.0, Python 3.12)

**Sources:**
- CDN: `https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/icons/<name>.png`. **Verified**: HTTP 200, 32×32 RGBA. About 62% of pixels are fully opaque; the shape is the head silhouette, not a circle, so use the alpha channel as the mask.
- The game sheet: `materials/vgui/hud/minimap_hero_sheet_psd_3529892a.vtex_c`. Decode it as the last 512·512·4 bytes, RGBA. Combined with the x/y coordinates in `mod_textures.txt`, it matched the CDN icons pixel for pixel for pudge and crystal_maiden (mean abs diff 0.4–0.5 on opaque pixels). antimage differed (diff 45), which suggests the CDN and the game art drift apart.
- The other sheet, `minimap_hero_sheet.vtex_c`, is stale: its pudge cell holds a different hero.
- **Use the sheet from the installed VPK as the authoritative template source.** It always matches the installed patch and includes all variants. Python `vpk` reads it directly.

**Benchmark.** Synthetic 260×260 minimap, 10 icons resized to 20px with `INTER_AREA`. Script: `<scratch>/bench/bench.py`

| Method | ms/frame | Correct |
|---|---|---|
| Masked `TM_CCORR_NORMED` over the full map, 10 templates | 154 | 10/10 |
| Masked `TM_SQDIFF` over the full map, 10 templates | 118 | 10/10 |
| Masked over the full map, **all 183** templates | 2,085 | – |
| Colour, unmasked `TM_CCOEFF_NORMED` (transparent pixels filled with the template mean) | **60** | 10/10 |
| Grayscale unmasked | 26 | **8/10**, so colour matters |
| **Masked SSD on 26×26 crops around 10 candidates × 10 templates** | **7.5** | 10/10 |
| 50% occluded icon (another icon pasted over it) | – | **Failed**: the peak jumped elsewhere with score 0.67 |

Takeaways:
- Masked matching over the full frame is slow in OpenCV. Use unmasked colour CCOEFF for detection and masked SSD only on crops for verification.
- Scale: about 20px per icon at a 1080p minimap of about 240px is an estimate (UNVERIFIED for 1728×1117). Calibrate it once by matching your own hero's template at scales 0.5–1.0 in steps of 0.02 and keeping the best.

## 4. Minimap pixels to world coordinates

These come from the installed map entity lump `maps/dota.vpk → maps/dota/entities/default_ents.vents_c` (KV3 v5, zstd; I decompressed it and read the string table):
- `_dotatilegrid_fogbounds_min/max`: (-9472, -9472) to (9472, 9472)
- `world_bounds` min (-8448, -9472, 0), max (8448, 8448, 1024). The values are verified strings; their link to the world_bounds entity is inferred from string order.
- Two `dota_minimap_boundary` entities exist (hammer IDs 41297 and 41298). Their origins are typed vectors I couldn't decode. My attempt to run Source2Viewer-CLI was blocked by the permission classifier, so **the exact minimap bounds are UNVERIFIED**. The old ±8288 figure dates from before 7.33 and should not be used.
- **Prior:** x ∈ [-8448, 8448], y ∈ [-9472, 8448], with y flipped on screen.

**Calibration (do this anyway):** collect pairs of (GSI `hero.xpos/ypos`, detected centre of your own hero's icon), then fit with RANSAC:

```python
A, inl = cv2.estimateAffine2D(np.float32(world_pts), np.float32(px_pts), ransacReprojThreshold=2.0)
# need ≥20 pairs spread over the map; refit while walking out of fountain; store per HUD config
px = A @ [x, y, 1];  world = cv2.invertAffineTransform(A) @ [u, v, 1]
```

- Discard samples when your icon is overlapped, or during TP or blink. GSI positions arrive at roughly the GSI throttle rate (about 0.1 s), so match them to the frame by timestamp.
- Error budget: one minimap pixel ≈ 17,000 / 260 ≈ **65 world units**. Hero move speed is about 300–550 u/s, so 5 fps is plenty.

## 5. Other minimap signals

| Signal | Classical method | Value |
|---|---|---|
| **Fog of war / your team's vision** | Fog is drawn darker. Compare the V channel against a fully lit reference minimap you captured once (for example at game end or in the demo) to get a binary vision mask. | Tells the coach where enemies *could* be hiding. |
| **Enemy TP** (`minimap_ping_teleporting`) | Template or colour-blob match at the TP destination. Transient. | Strong gank / defend warning. |
| **Camera box** | A thin, near-white trapezoid (S<30, V>230 as line segments, `HoughLinesP`). Remove those pixels from matching masks. | Where you're looking: "watch the map" nudges. |
| **Towers and buildings** | Fixed positions. After calibration, sample a small patch at each known pixel and flag it destroyed when it stops matching its reference. GSI `buildings` may be spectator-only (UNVERIFIED). | Map state. |
| **Creeps** | Small blobs: HSV threshold plus `connectedComponentsWithStats` with area 2–12 px. Cluster along the lane polylines. | Lane equilibrium, pushes. |
| **Wards** | Only your own team's wards render (`minimap_ward_obs`). | Vision bookkeeping. |
| **Pings** | Transient and often over icons. Ignore any detection that disappears within the ping duration (`dota_minimap_ping_duration`). Template-match `minimap_danger` etc. to log teammate pings. | Noise source, and teammate intent. |
| **Roshan** | `minimap_roshancamp` icon. Since 7.38 there are two river pits, top/NW ("Dire pit") and bottom/SW ("Radiant pit"). A 7.41e guide says Roshan **swaps pits with day/night** and first spawns at 20:00 (UNVERIFIED; check the current pit icon each frame instead). | "Enemies doing Rosh" = 3+ enemies missing, last seen near the active pit, and you have no vision there. Confirm with the GSI `events` `roshan_killed` / `aegis_picked_up` (these keys exist in the binary). |

## 6. Recommended v1 pipeline

```
capture (ScreenCaptureKit/mss) 5 Hz → crop minimap ROI (fixed per HUD layout; right side!) → INTER_AREA /2
roster: own hero via GSI; other 9 via one-time top-bar portrait match → 10 templates (+ their alt/persona variants)
1. pre-mask: zero out camera-box pixels, known building/shop/rune icon pixels
2. detect: per roster hero, colour TM_CCOEFF_NORMED full ROI (~60 ms/10 heroes);
   peaks ≥ T_det (start 0.70) with NMS radius = icon/2; also search a window around last track at a lower T (0.55)
3. verify: masked SSD on padded crop; accept if best < T_ssd and (2nd-best / best) ≥ margin 1.15
4. assign: one hero per blob (scipy linear_sum_assignment on −score); allow extra blobs for
   illusion/clone heroes (PL, Naga, Terrorblade, Chaos Knight, Meepo, Arc Warden double) → "possible illusion"
5. track: alpha-beta filter per hero; gate on 600 u/s × dt plus TP/blink allowance; publish after 2 consistent
   frames; jumps > 1500 u need 3 frames
6. state: visible enemies get position+timestamp; missing enemies get last_seen + MIA seconds + reachable radius
   (speed × t) clipped to the fog mask → this is what gets sent to the LLM as compact JSON
```

**Failure modes and how to handle them:**
- **Overlapping icons** (lanes, fights): occlusion breaks the global match, as shown in the benchmark. Use a track-local search at a lower threshold, keep the hero's last position if a blob is still present nearby, and try `hero_spread 1`.
- **Camera box crossing icons:** handled by the mask.
- **Pings and danger glows over icons:** temporal persistence filter.
- **Own-hero highlight glow** (`herocircle_self_highlight`): take your own position from GSI, never from CV.
- **Smoke:** enemies vanish together, which is itself a signal.
- **Persona/arcana icon swaps:** load all variant templates for each roster hero.
- **HUD scale or resolution change:** re-run the scale and affine calibration.
- **Death:** the icon disappears. Separate dead from fogged via the top-bar respawn timer or GSI kill events.
- **Fog brightness drifting** from day/night and HUD effects: refresh the reference regularly.

**Upgrade path if precision or recall is too low:** generate synthetic minimaps by pasting the VPK sprites over real background captures, with blur, JPEG compression, fog dimming, pings and overlaps. Then train a YOLO11n with 10 per-match classes, or a small CNN that classifies the 26×26 candidate crops, and run it under CoreML.

## Files (scratchpad)
- Decoded sheet: `<scratch>/tex/sheet2.png`
- Ring and arrow textures: `<scratch>/tex/rings.png`
- Sprite coordinates: `<scratch>/vpkout/mod_textures.txt` and `<scratch>/vpkout/minimap_icons.txt`
- Decompressed entity lump: `<scratch>/mapents/dec.bin`
- Benchmark script: `<scratch>/bench/bench.py`

Sources: [dota2-vis](https://github.com/RicardoRCarvalho/dota2-vis), [Quinntana LoL tracker](https://github.com/Quinntana/LOL_Minimap_Tracker), [lol-minimap-yolo](https://github.com/may850608-glitch/lol-minimap-yolo), [DeepLeague](https://github.com/farzaa/DeepLeague), [PandaScore](https://www.pandascore.co/blog/league-of-legends-getting-champion-coordinates-from-the-minimap-using-deep-learning), [Dota YOLO gist](https://gist.github.com/6e87f605579425d4f3a24976fcb0115f), [antonpup/Dota2GSI](https://github.com/antonpup/Dota2GSI), [dota2-minimap-hero-sprites](https://unpkg.com/dota2-minimap-hero-sprites@2.4.0/README.md), [ValveResourceFormat](https://github.com/ValveResourceFormat/ValveResourceFormat), [7.38 Roshan pits](https://www.gosugamers.net/dota2/news/74352-dota-2-introduces-more-water-and-new-roshan-pit-locations-in-patch-7-38), [7.41e objectives guide](https://www.playnews.gg/en/guides/dota-2-the-map-objectives-guide-for-patch-7-41e-roshan-the-gravekeeper-shrines-and-portals), [Liquipedia 7.41f](https://liquipedia.net/dota2/Version_7.41f).