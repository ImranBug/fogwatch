# Live Dota 2 Coach v1: Architecture and Build Spec (client build 6972, Nov 05 2026; game-data baseline patch 7.41f)

## 0. Decisions, and where the reports disagreed

The design has three layers:

- **Perception:** Valve GSI is the main source. Classical computer vision on screen captures fills the gaps. There is no AI in this layer.
- **Rules:** run every 250 ms.
- **LLM:** called rarely, and only to phrase strategy.

| Topic | Conflict | Resolution |
|---|---|---|
| Enemy positions | The gsi report says the player gets a GSI `minimap` block with visible enemies (`minimap_enemyicon`, per Wardly recordings). The minimap report says this is spectator-only (UNVERIFIED). | **Build both and merge them.** A GSI minimap entry overrides CV. CV covers whatever GSI lacks. Every disagreement is logged as a CV accuracy metric. The first real game settles which source wins, and CV stays in either case for the lineup, dead state and experiments. |
| Minimap side | The project context says left. The minimap report says `dota_minimap_position_option "1"`, which is **right**. | Verified locally: the user's vcfg has `position_option "1"`, `primary_option "0"`, `background_option "1"`, `hero_size "600"`. **The minimap is on the right.** The app reads the vcfg at startup and never hardcodes the side. |
| Minimap convars | The context and the hud report use `dota_minimap_show_hero_icon`, `simple_colors`, `hud_flip`. | Those convars are "No longer used" per the binary help strings. Use `dota_minimap_primary_option`, `background_option` and `position_option`. |
| Window size | The context and `video.txt` say 1728×1117 on the built-in display. The capture report says 2560×1440 pt on the Studio Display. | **Never hardcode it.** Compute everything from each captured frame: `s = H/1080`, `C = W/2`. Today that means Dota is on the Studio Display and the dashboard goes on the MacBook screen. |
| Capture scale | Downscale a 2× capture, or capture at 1×? | Capture at 1.0 (points). Dota is not hi-dpi, so a 2× capture adds no detail. |
| Map bounds | ±8288 (datdota, 7.39); x [−8448, 8448], y [−9472, 8448] (entity lump); the old ±8288 figure dates from before 7.33. | Start from the hud report's formula using ±8288. **Always refit an affine transform** from your own hero's GSI position paired with its icon on the minimap. |
| Sprite source | CDN 32px icons, or the VPK sheet (the antimage icon differed between them). | Use the VPK sheet `materials/vgui/hud/minimap_hero_sheet_psd_*.vtex_c` together with `scripts/mod_textures.txt`. It matches the installed patch and includes the variant icons. CDN images are used only for the 256×144 top-bar portraits and for dashboard images. |
| GSI timings | heartbeat 30 or 2; buffer 0 or 0.1. | `buffer 0.1`, `throttle 0.1`, `heartbeat 2.0`, `timeout 5.0`, all providers on. Plan for about 1 Hz real updates. |
| Transport | WebSocket or SSE | SSE through `fastapi.sse.EventSourceResponse`. This needs no extra dependency; plain uvicorn has no WebSocket support. |
| LLM client | `openai` SDK | **Stdlib `urllib`.** It is one POST, so no dependency is needed. |
| Assignment | scipy `linear_sum_assignment` | Greedy assignment. With 10 heroes the difference is negligible, and it saves a dependency. |

## 1. Stack and run command

- Python 3.13 managed by uv. System Python 3.9 cannot run numpy 2.5 or pyobjc 13.
- I checked `vpk` 1.4.0 and `vdf` 3.4 on PyPI today.

```toml
# pyproject.toml
[project]
name = "dota-coach"
version = "0.1.0"
requires-python = ">=3.13,<3.15"
dependencies = [
  "numpy==2.5.4", "opencv-python-headless==5.0.0.93",
  "pyobjc-core==13.0", "pyobjc-framework-ScreenCaptureKit==13.0", "pyobjc-framework-libdispatch==13.0",
  "fastapi==0.162.2", "uvicorn==0.54.0", "vpk==1.4.0", "vdf==3.4",
]
[tool.uv]
package = false
```

Commands:

- **Run:** `uv run app.py`. This serves `http://127.0.0.1:3000`. The browser on the second monitor opens `/`.
- **Dev:** `uv run app.py --frames fixtures/synth --replay fixtures/sim.jsonl --speed 4`. This needs no game.
- **One-time setup:** `uv run tools.py install-gsi`, then add the Steam launch option `-gamestateintegration`.
- **Self-check:** `uv run tools.py selfcheck`.

## 2. File layout (8 source files, plus a data folder)

| File | Responsibility and key signatures |
|---|---|
| `app.py` | FastAPI app and lifespan. Routes: `POST /gsi` (check the token, reply 200 at once, then `world.merge_gsi`), `GET /events` (SSE, full world at ≤4 Hz, coalesced), `GET /` (static index), `GET /calib.png` (latest frame with predicted boxes drawn), `GET /minimap.png` (latest raw crop), `POST /api/calibrate`, `POST /api/mark?name=rosh_top\|rosh_bot`, `POST /api/settings` (role, bracket). An asyncio `tick()` loop every 0.25 s runs `world.tick` → `rules.evaluate` → `llm.maybe_call` → publish. CLI flags: `--frames DIR --replay FILE --speed N --record`. |
| `world.py` | Owns `W`, a single dict guarded by `threading.Lock`. Functions: `merge_gsi(body)`, `merge_cv(cv: dict, t)`, `tick(now)` (MIA timers, ghost radii, vanish detection), `snapshot() -> dict`, `to_text() -> str` (the LLM snapshot), `zone(x, y, team) -> str`. |
| `capture.py` | The SCStream module from the capture report, verbatim: `find_dota_window()` and `start(on_frame, fps=4, scale=1.0)`. Adds `start_files(dir, on_frame, fps)` for replaying PNGs, and `snap() -> ndarray`. If the window is missing, retry every 5 s. |
| `vision.py` | `regions(W, H, mm_side, xl=False) -> {slots[10], minimap, s}`; `load_sprites() -> {hero: [rgba32...]}`; `identify_lineup(frame, my_hero, my_team) -> (heroes[10], scores[10])`; `slot_dead(frame, slot, base_sat) -> bool`; `detect_minimap(crop, roster, tracks) -> [{hero, u, v, score}]`; `fit_affine(pairs) -> A`; `px_to_world(A, u, v)`; `process(frame, t) -> cv dict`. |
| `data.py` | Fetched once per patch into `data/cache/<patch>/`. `load_static()` covers OpenDota `/heroes`, `/heroStats` and `/constants/patch`, plus dotaconstants `items.json`, `item_ids.json` and `heroes.json`. `hero_data(hero_id)` covers `/heroes/{id}/itemPopularity` and `/heroes/{id}/matchups`. `valve_build(hero)` reads `game/dota/itembuilds/default_<hero>.txt`, the offline fallback. `vpk_file(path)` reads from the VPK. Nothing is fetched during a game except one burst of ≤10 per-hero calls when the lineup locks (within 60 req/min). |
| `rules.py` | `evaluate(W, now) -> None`. Mutates `W["alerts"]`, `W["timers"]`, `W["advice"]` and `W["enemy_report"]`. `TIMERS` and `COUNTERS` are tables. Functions: `danger(W)`, `rosh_suspicion(W)`, `items(W)`, `weak_heroes(W)`. |
| `llm.py` | `maybe_call(W, now)` handles triggers, guards and the background thread. `ask(system, user) -> dict\|None` makes the call. Also holds `SCHEMA` and `SYSTEM`. |
| `static/index.html` | The entire dashboard in one file: HTML, CSS and roughly 300 lines of vanilla JS. |
| `tools.py` | Subcommands: `install-gsi`, `sim`, `replay`, `synth`, `snap`, `selfcheck`. |

Runtime data:

- `data/calib.json`: affine `A`, icon px size, dx/dy/ds offsets, Roshan pit coordinates.
- `data/token`
- `data/cache/`
- `recordings/<ts>/gsi.jsonl` and `recordings/<ts>/frames/*.png`, written only with `--record`. Saved crops are the minimap plus the top-bar band at 1 fps, about 100 KB per frame.

## 3. GSI

**`tools.py install-gsi`:**

- creates `~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/`;
- generates `secrets.token_hex(16)` and saves it to `data/token`;
- writes `gamestate_integration_coach.cfg`;
- prints: "Steam → Dota 2 → Properties → Launch Options: `-gamestateintegration`, then restart Dota".

The cfg it writes:

```
"coach"
{
    "uri"       "http://127.0.0.1:3000/gsi"
    "timeout"   "5.0"
    "buffer"    "0.1"
    "throttle"  "0.1"
    "heartbeat" "2.0"
    "auth"      { "token" "<hex>" }
    "data"
    {
        "provider" "1" "map" "1" "player" "1" "hero" "1" "abilities" "1" "items" "1"
        "events" "1" "buildings" "1" "minimap" "1" "draft" "1" "roshan" "1"
        "couriers" "1" "neutralitems" "1" "league" "1" "wearables" "0"
    }
}
```

Providers the client can't supply are skipped, and the console logs "cannot be provided".

**Ingest rules:**

- Drop the payload if `auth.token` mismatches, if `player.activity != "playing"` or `team_name == "spectator"`, or if `customgamename` is non-empty (unless `--demo`).
- Null-check every field.
- Use `clock_time` for all timers.
- Ignore minimap entries at (0,0) and `minimap_plaincircle`.
- Keys `oN` are not stable IDs, so key units by `unitname`.
- Deduplicate events on `(event_type, game_time, json of ids)`.
- Take team from `team_name`, never from `player_slot`.

**World state `W`** (one dict, sent whole over SSE):

```
t, src_age: {gsi_s, cv_s}
game:  {state, clock, daytime, ns_night, paused, matchid, map, kind: normal|demo|custom, score:[rad,dire], win_team}
me:    {hero, hero_id, team, role(1-5, from UI), level, alive, respawn_s, hp_pct, mp_pct, gold, buyback_cost,
        buyback_cd, k,d,a, lh, dn, gpm, xpm, x, y, items[6], backpack[3], stash[6], neutral, tp_cd,
        abilities:[{name,level,cd,ult}], smoked, scepter, shard}
heroes: { "<short>": {team, slot, ally, visible, x, y, src: gsi|cv, last_seen, last_xy, mia_s, reach_u,
         dead, respawn_s, conf, vanished_at} }          # 10 entries once lineup locks
lineup: {locked, scores[10]}
obj:   {rosh: {state: alive|dead|maybe, killed_at, killer_team, aegis_until, pit: top|bot, last_seen_at},
        my_buildings: {name: hp_pct}}
events: [last 30, normalized]
alerts: [{id, level: info|warn|high, text, rule, at, ttl}]
timers: [{name, at_clock, in_s}]
advice: {headline, do_now[], next_items[{name, cost, affordable}], where, risk, why, src: rules|llm, at}
enemy_report: [{hero, weak_score, winrate, reasons[], counter_items[]}]
cv:    {fps, ms, minimap_rect, icon_px, affine_ok, gsi_vs_cv_err_u (median), frame_ok}
llm:   {enabled, calls, in_flight, last_at, usd_est}
```

## 4. CV pipeline

**Capture:**

- An SCStream window filter on the largest layer-0 window owned by `"Dota 2"`: full window, scale 1.0, 4 fps, BGRA → BGR.
- Call `Q.CGMainDisplayID()` first, and completion blocks must return None (see the capture report).
- Frames are processed on the dispatch thread. If the previous frame is still being processed, drop the new one.
- Crops are numpy slices.

**Geometry** (`regions`, from the hud report's CSS constants):

- `s = H/1080`, `C = W/2`.
- Radiant slot k: `x = C + (−416 + 62k)s`.
- Dire slot k: `x = C + (102 + 62k)s`.
- Slot size: `y = 4s, w = 66s, h = 36s`.
- Minimap `m = 244s` (`280s` if extra-large): `x = W−m` if `position_option == 1`, else 0; `y = H−m`.

| Frame | Slot R0 | Slot D0 | Minimap |
|---|---|---|---|
| 1920×1080 | (544, 4, 66, 36) | 1062 | (0, 836, 244, 244), left |
| **2560×1440 (your setup, right side)** | (725, 5, 88, 48) | 1416 | **(2235, 1115, 325, 325)** |

**Calibration** (`POST /api/calibrate` and automatic):

1. **Top-bar check.** Your own hero's portrait in your predicted slot must score ≥0.8. If it doesn't, search the top 6% of the frame at `s × [0.8 … 1.25]`, 15 steps, and save the best `s'`.
2. **Icon size.** GSI gives your world position. Predict its pixel with the default mapping and search a 60×60 window with your hero's sprite at sizes 12–40 px in steps of 1. Keep the best as `icon_px`.
3. **Affine.** On every frame where your icon's score is ≥0.8 and you are not overlapped by another icon (and not TPing), store the pair (GSI `x,y` at the nearest timestamp, detected `u,v`). Once there are ≥20 pairs spread over at least 3000 world units, run `cv2.estimateAffine2D(world, px, ransacReprojThreshold=2.0)`, save `A`, and refit every 50 pairs. Until that point, use `u = −8s + 260s·(x+8288)/16576`, `v = −8s + 260s·(8288−y)/16576` (UNVERIFIED bounds).
4. `/calib.png` draws the slot boxes, the minimap rectangle and the detected icons. dx/dy/ds offsets go in `calib.json`.

**Lineup** (from `STRATEGY_TIME`/`PRE_GAME`, retried every 3 s until locked):

- Template: the CDN portrait `dota_react/heroes/<short>.png` (256×144), resized to `66s×36s` with `INTER_AREA`, greyscale, cropped to the band `x 8s–58s, y 4s–22s`.
- Matching: `TM_CCOEFF_NORMED` against all 127 heroes plus arcana alternates, with a ±3 px margin. Assign greedily, best score first, each hero used once.
- Lock when all 10 scores are ≥0.75 and your own slot matches your GSI hero.
- This uses only what is rendered in the top bar. **There is no player-profile lookup.**

**Dead state:**

- Record each slot's mean HSV saturation while the hero is alive, as `base_sat`.
- The hero is dead when saturation is below `0.35 × base_sat` for 2 consecutive frames. A dead hero is excluded from MIA.
- v1 has no respawn timer OCR. Respawn shows only as dead or alive.

**Minimap detection** (roster = the 9 non-self heroes; your own position always comes from GSI):

1. **Prepare.** Templates are the VPK sprites for each roster hero and its variants (`<name>`, `_persona1`, `_alt*`), resized to `icon_px` with `INTER_AREA`. Fill transparent pixels with the template mean.
2. **Pre-mask.** Remove camera-box pixels (HSV S<30 and V>230, then dilate 1 px) and replace them with the local median.
3. **Detect.** Per hero, colour `TM_CCOEFF_NORMED` over the full ROI. A peak needs ≥ **0.70**, or ≥ **0.55** inside the gate around the hero's existing track. The gate radius is `(600 u/s · dt + 1 icon) → px`. NMS radius = `icon_px/2`.
4. **Verify.** Run a masked SSD (alpha mask) on an `(icon_px+6)²` crop against all roster templates. Accept only if this hero is best and `second_best/best ≥ 1.15`.
5. **Assign.** Greedily by score; one hero per blob and at most one blob per hero. For PL, Naga, TB, CK, Meepo and Arc Warden, mark extra blobs as `possible_illusion` and do not track them.
6. **Track.** A position is published after 2 consistent frames, and a jump of more than 1500 u needs 3. Smoothing is EMA with α=0.5 in world space. Hold a lost hero for 1 frame before marking it unseen, which absorbs ping and occlusion flicker.
7. **Merge.** For each hero, GSI minimap ≻ CV. Log the median CV-vs-GSI distance to `cv.gsi_vs_cv_err_u`.

Expected cost is about 60–100 ms per frame on the 325 px ROI, roughly 30% of one core at 4 fps. If that is too much, drop CV to 2 fps with `--cv-fps`.

**Last seen and MIA** (in `world.tick`, every 0.25 s):

```python
for h in enemies:
    if h["visible"]: h.update(last_seen=clock, last_xy=(h["x"], h["y"]), mia_s=0)
    elif not h["dead"] and h["last_seen"] is not None:
        h["mia_s"] = clock - h["last_seen"]
        h["reach_u"] = min(330 * h["mia_s"], 6000)   # ponytail: flat 330 u/s, no pathing/TP
```

- `vanished_at` is set when a hero goes from visible to unseen. The smoke rule uses it.
- Everything lives in memory. With `--record`, the session can be replayed exactly.

**User settings to change** (console or Settings → Minimap; exact menu labels UNVERIFIED):

```
dota_minimap_primary_option 3       // hero icon only (0 also works; arrows add edge clutter)
dota_minimap_background_option 1    // Simple (already set)
dota_minimap_show_cast_range 0      // removes 1200u circles over icons
dota_minimap_hero_scalar 0          // constant icon size
dota_minimap_hero_size 800          // bigger icons classify better; recalibrate after changing
```

Do not change the extra-large minimap unless you also set `xl=True`, because `regions` reads it.

## 5. Rule engine (`rules.py`; thresholds are starting values to tune)

**Timers** (patch 7.41f, `clock_time`). Format is (name, first, period, lead seconds, roles):

```python
TIMERS = [("Bounty runes",0,240,20,"all"), ("Water rune",120,120,20,"mid"),   # water: only 2:00 & 4:00
          ("Power rune",360,120,20,"mid"), ("Wisdom shrine",420,420,30,"3,4,5"),
          ("Lotus",180,180,0,"all"), ("Stack",53,60,4,"4,5"),                 # :53 per creep_pull_timings
          ("Day/Night",300,300,15,"all"), ("Tormentor",900,None,30,"all")]
NEUTRAL_TIERS = [0, 900, 1500, 2100, 3600]                                      # "craft neutral" alert
```

The stack timer reminds only once `role ∈ {4, 5}` and before 25:00.

**Roshan:**

- `roshan_killed` → `dead` until killed_at + 480, then `maybe` until +660, then `alive`.
- `aegis_picked_up` → `aegis_until = t + 300`.
- Rosh is assumed alive at game start (UNVERIFIED whether he exists before 0:00).
- Pit: 0–15:00 top, then night → bot, day → top (inferred per the 7.41 swap; **UNVERIFIED**). Override this whenever GSI minimap shows `npc_dota_roshan`.
- **Pit coordinates are not in v1 defaults.** In hero demo, walk to each pit and press "Mark rosh_top" or "Mark rosh_bot". This stores your GSI x,y.

**R1 Danger / likely gank** (for me, while alive). Per alive enemy:

- `threat = 1.0` if visible and `dist ≤ 2000`;
- `threat = 0.6` if `4 ≤ mia_s ≤ 40` and `dist(last_xy, me) ≤ reach_u + 1000`;
- otherwise 0.

Total: `D = Σthreat × (1.5 if me in enemy half (x+y>0 for radiant) else 1) × (1.25 if night) / (1 + allies_within_1500)`.

- `D ≥ 1.5` → warn: "Likely gank: back toward tower".
- `D ≥ 2.5` → high: "Danger: leave now / hold TP".
- Debounced 20 s. It re-fires only if the level rises.

**R2 Missing enemies:** at least 3 alive enemies with `mia_s ≥ 8` while I'm outside my own T1 or in the river or jungle → warn: "3 missing: farm closer to tower".

**R3 Smoke suspicion:** at least 2 enemies vanish within 3 s of each other while their `last_xy` points are within 1500 of each other → high: "Possible smoke: A+B vanished at <zone>".

- The alert lives 45 s.
- While it is active, those heroes' threat is forced to 1.0 within `reach_u + 1025`.

**R4 Roshan suspicion:** Rosh is alive or maybe; at least 3 enemies alive with `mia_s ≥ 10`; at least 2 of them were last seen within 4000 of the active pit; and no ally is within 1500 of the pit.

- warn: "Possible Roshan".
- If 4 or more enemies meet the condition: high, "Enemies likely at Roshan: smoke/ward or push the opposite lane".
- The `roshan_killed` event clears it and posts a kill/aegis line.

**R5 Night start:** info: "Night: vision 800 — ward/avoid river". A `ns_night` start raises this to high.

**R6 Buyback:** after 30:00, `role ≤ 3` and `gold < buyback_cost` → warn: "Save buyback (need X)".

**R7 Last hits:** at each whole minute from 3 to 15, compare `lh/min` with OpenDota `/benchmarks` p30 and p70 (fetched for my hero). Below p30 → info.

**R8 Items:**

- Phase: `<0 start | <600 early | <1500 mid | late`.
- Take `itemPopularity[phase]` for my hero and map ids via `item_ids` and `items` to `{dname, cost}`.
- Drop items already owned anywhere in the inventory, backpack or stash. Take the top 3 by count.
- `affordable = gold ≥ cost`.
- Append `COUNTERS` from the enemy lineup:

```python
COUNTERS = {"evasion": (["phantom_assassin","windranger","troll_warlord"], "monkey_king_bar"),
  "invis": (["riki","clinkz","bounty_hunter","weaver","nyx_assassin","invoker","treant"], "ward_sentry / dust"),
  "illusions": (["phantom_lancer","naga_siren","terrorblade","chaos_knight"], "mjollnir / battle_fury"),
  "heal": (["necrolyte","huskar","alchemist","morphling"], "spirit_vessel"),
  "magic": (["lion","lina","zuus","skywrath_mage","leshrac"], "black_king_bar")}  # heuristic, hand-curated
```

If OpenDota is unreachable, use the Valve `default_<hero>.txt` sections.

**R9 Weak enemy heroes** (computed once when the lineup locks):

```
weak_score = 0.4·z(−winrate in bracket b, i.e. b_win/b_pick, default pub_win/pub_pick)
           + 0.3·z(−(base_armor + agi·0.167))
           + 0.2·z(−base_health)
           + 0.1·(my matchup winrate vs them − 0.5)·10   # only if games_played ≥ 30
```

- Matchup samples are small and probably pro-only.
- "Kill target" = the highest `weak_score` among enemies that are visible and have no other enemy within 1500.

**Rules-only advice (no LLM):**

- `headline` = the top active alert, else a phase template such as "Laning: last-hit, next Bounty 3:40".
- `next_items` from R8.
- `where = zone(me)`.

## 6. LLM layer (`llm.py`)

- **Off unless `LLM_BASE_URL` is set.** Without it the dashboard shows rules-only advice with `src: rules`.
- **Environment variables:** `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (example from the llm report: `gpt-6-luna` on `https://api.openai.com/v1`); `LLM_FALLBACK_BASE_URL/_API_KEY/_MODEL` (example: `claude-haiku-5-5` on `https://api.anthropic.com/v1/`); `LLM_REASONING_EFFORT=low`; `LLM_TIMEOUT_S=10`; `LLM_MIN_INTERVAL_S=20`; `LLM_MAX_CALLS_PER_GAME=60`; `LLM_MAX_USD_PER_GAME=0.10`; `LLM_PRICE_IN=0.10`; `LLM_PRICE_OUT=0.50` (USD per 1M tokens, used for the estimate).
- **Triggers:**
  - lineup locked: the pre-game plan, the one larger call;
  - every 150 s of clock while alive;
  - my death;
  - reaching level 6, 12 or 18;
  - a high alert that persists ≥10 s.
- **Guards:** never two calls in flight; ≥ min interval; ≤ max calls; `usd_est < max`. Discard a result if the clock has moved more than 60 s since the request. Reset at a new `matchid`.
- **Request:** a POST to `{base}/chat/completions` through stdlib `urllib` in a thread.
  - `messages = [system (static, byte-identical all game, so it can be cached), user = world.to_text()]`;
  - `response_format = {"type":"json_schema","json_schema":{"name":"coach","strict":true,"schema":SCHEMA}}`;
  - `max_completion_tokens = 1500`;
  - `reasoning_effort` is sent **only** when the host contains `openai.com` or `googleapis`. `temperature` is never sent.
  - On any error, try the fallback, then return None.
- **Snapshot** (`to_text`, about 400 tokens):
  `t=18:42 night me=juggernaut L14 hp62 mp40 g2840 bb1210 kda7/3/5 lh142 | items:... | pos:own_jungle_bot | enemies: lion(vis,mid_river) pa(MIA31s,last bot_river) ... | alerts: R4 rosh_suspect | rosh:alive pit=bot | score 12-17 | weak: lion,cm`
- **Schema.** Lengths are enforced in the prompt and truncated on the server, because strict mode does not support `maxLength`:

```json
{"type":"object","additionalProperties":false,
 "required":["headline","do_now","next_items","where","risk","why"],
 "properties":{"headline":{"type":"string"},"do_now":{"type":"array","items":{"type":"string"}},
  "next_items":{"type":"array","items":{"type":"string"}},
  "where":{"type":"string","enum":["safe_lane","mid","off_lane","own_jungle","enemy_jungle","river","roshan","base","with_team"]},
  "risk":{"type":"string","enum":["low","med","high"]},"why":{"type":"string"}}}
```

- **System prompt contents:**
  - the coach role;
  - "advise only; never instruct about chat or inputs";
  - output limits (headline ≤90 chars, do_now ≤3 items of ≤60 chars, why ≤200);
  - "rule alerts are authoritative, don't contradict them";
  - my hero's abilities and popular build, plus the enemy weak/counter table (the per-game static part).
- **Expected cost:** about $0.02 per game at luna or haiku prices (llm report). Model names and prices are taken from that report and not re-verified here.

## 7. Dashboard (`static/index.html`, aimed at 1728×1117)

```
┌──────────────────────────────── header: 18:42 ☾ | GAME_IN_PROGRESS | Juggernaut L14 7/3/5 | 2840g | GSI ● CV ● LLM ● ┐
│  REPLICA MAP (SVG, square, ~min(100vh-60px, 60vw))  │  ALERTS feed (newest top, colour by level, ttl fade)       │
│  - lanes/river schematic background                 │  ADVICE: headline / do_now / where / risk (src+age badge)  │
│  - allies: blue ring + 32px CDN icon; me: gold ring │  ITEMS: next 3 (cost, affordable=green) + counter items    │
│  - enemies visible: red ring + icon                 │  TIMERS: next 5 (countdown, flashes inside lead)           │
│  - MIA ghosts: greyscale icon @40% at last_xy,      │  ENEMY PANEL: 5 rows: portrait, dead/MIA s, weak_score bar,│
│    dashed circle r=reach_u, "23s" label             │    reasons, counter item                                   │
│  - Rosh pits coloured alive/maybe/dead, aegis timer │                                                            │
│  - inset: /minimap.png raw crop (debug toggle)      │  footer: role select 1-5, Calibrate, Mark rosh_top/bot     │
│  LINEUP strip under map: 10 portraits, dead greyed  │                                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Transport:** `new EventSource('/events')`. The client re-renders on each message (≤4 Hz, about 10 KB each) and reconnects automatically.
- **Map display transform:** fixed `svg = ((x+8300)/16600·1000, (8300−y)/16600·1000)`. This is for display only; CV uses its own affine.

## 8. Testing without a live game

- **`tools.py sim [--speed 4]`.** POSTs a scripted GSI timeline to `/gsi` at 1 Hz using the real payload shape from gsi report §6. The script is about 60 lines covering:
  - Juggernaut, Radiant, from −90 to 25:00;
  - enemies appearing in the `minimap` block and moving along lanes;
  - lion and pa vanishing within 2 s at 11:00 (smoke);
  - 4 enemies going MIA near the top pit at 17:30;
  - `roshan_killed` at 18:00, then `aegis_picked_up`;
  - my death at 12:00.
  It also writes `fixtures/sim.jsonl`.
- **`tools.py replay FILE --speed N`.** Re-posts `recordings/*/gsi.jsonl` with its original timing.
- **`tools.py synth --n 200`.** Generates synthetic frames:
  - Background: `data/minimap_bg.png` if captured (the first real crop, with heroes painted out by median of 30 frames), else a flat `#2b3a2b` square with lane and river lines.
  - Paste 10 VPK sprites, alpha-blended at `icon_px`, at positions mapped through the default affine.
  - Augment: Gaussian blur σ=0.6, a JPEG q=70 round-trip, fog (40% darkening of random half-planes), one forced 50% overlap pair, and a white camera trapezoid.
  - Compose a full 2560×1440 frame: black, with the crop at the minimap rectangle and CDN portraits at the 10 slot boxes.
  - Output: `fixtures/synth/NNN.png` plus `NNN.json` with ground truth (hero → u, v, world x, y; slot → hero).
- **`tools.py snap`.** Saves one real full-window frame to `data/snap.png` and an overlay to `data/snap_boxes.png`. **This is the first thing to run with Dota open.**
- **`tools.py selfcheck`** prints PASS/FAIL per check:
  1. `regions(1920,1080,'left')` equals slot0 (544,4,66,36) and minimap (0,836,244,244); `regions(2560,1440,'right')` minimap equals (2235,1115,325,325).
  2. The VPK sheet decodes, and the pudge sprite has 400–800 opaque pixels.
  3. On 20 synth frames, lineup is 10/10, minimap recall is ≥0.9 on non-overlapped icons, median error ≤2 px, and there are 0 wrong-hero IDs.
  4. Replaying `sim.jsonl` through `merge_gsi` + `tick` + `evaluate` (no HTTP) fires R3 between 11:00 and 11:05, R4 at about 17:40, and the Rosh state is `dead` at 18:01.
  5. The rules-only advice validates against `SCHEMA`.
  6. `fit_affine` recovers a known affine from noisy synthetic pairs to within 1 px.

## 9. Policy, risks, and what to check on your machine

**Do:**
- GSI via the cfg plus the launch flag.
- OS-level ScreenCaptureKit capture of rendered pixels only.
- Dashboard in a browser on the other monitor.
- Public data cached per patch.
- Advice only; the human makes every decision.

**Don't:**
- Read or hook Dota memory (`task_for_pid`/`vm_read`), or inject anything.
- Modify VPKs or game files. Writing the GSI cfg is the sanctioned exception.
- Parse `console.log`/`server_log.txt`, or use `record`/demo tricks.
- Look up enemy player profiles or history, ever. The lineup comes only from rendered portraits.
- Send keystrokes, mouse input, chat or pings.
- Use it in ticketed or tournament games (GSI is delayed there, and event rules forbid outside help).

**Risks and limits:**

| Risk | Effect / handling |
|---|---|
| GSI minimap for players (UNVERIFIED) | If absent, CV carries positions. |
| SCStream liveness while Dota is covered during a match (UNVERIFIED) | Unknown until tested in a real match. |
| macOS monthly screen-recording re-approval (UNVERIFIED for 26) | You may need to re-approve periodically. |
| TCC permission is granted to the terminal app | Running the coach from launchd needs a separate grant. |
| No game-mode field (Turbo has a 240 s aegis) | Turbo timers will be wrong. |
| Roshan/Tormentor pit rule after 7.41 is inferred | Pit coordinates must be marked manually. |
| Unverified game data | Tormentor respawn, glyph cooldown, buyback formula. |
| OpenDota samples are small | `matchups` gets only 10% weight. |
| Overlapping icons in fights lose identity | Tracks are held 1 frame. Fallback: YOLO11n trained on synth frames. |
| Smoke shows up only as heroes vanishing | That is the R3 heuristic. |
| GSI updates at about 1 Hz | Alerts lag by up to about 1 s. |
| Danger thresholds are guesses | Tune them from `--record` sessions. |

**Check on your machine, in this order:**
1. Run `tools.py snap` with Dota in a match or hero demo. Confirm the boxes in `snap_boxes.png` sit on the portraits and on the right-side minimap.
2. Run `install-gsi`, add `-gamestateintegration`, restart Dota. Open the Dota console and look for "Loading Game State Integration" and any "cannot be provided" lines.
3. In hero demo, check whether GSI `minimap` contains your hero and spawned enemy units. Hero demo has `customgamename` set, so run with `--demo`.
4. Apply the convars above, then walk around for about 30 s so the affine fits (`cv.affine_ok`).
5. Mark both Roshan pits.
6. Play one game with `--record`. Then rerun `selfcheck` against those recordings and compare `cv.gsi_vs_cv_err_u`.

The research scratchpad referenced by the reports, with sprite coordinates, the decoded sheet, the capture prototype and the benchmark, is at `<scratch>/`. Most relevant there: `captest/capture.py`, `vpkout/mod_textures.txt`, `tex/sheet2.png` and `bench/bench.py`.