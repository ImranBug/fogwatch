# Free data sources and rule tables for a non-AI Dota 2 coaching engine (patch 7.41f, Nov 2026)

## 0. Patch baseline (verified)
- **Latest patch: 7.41f, released 2026-09-15.** Source: the official Valve feed `https://www.dota2.com/datafeed/patchnoteslist?language=english`. The 7.41 line runs 7.41 (2026-03-24), then a 03-27, b 04-07, c 05-06, d 06-04, e 07-30, f 09-15. Before that: 7.40 (2025-12-16) and 7.40c (2026-01-21).
- **The local client is newer than the last patch.** `game/dota/steam.inf` shows `VersionDate=Nov 05 2026`, ClientVersion 6972.
- **Full patch notes are free JSON.** `https://www.dota2.com/datafeed/patchnotes?version=7.41&language=english` returns `general_notes`, `items`, `neutral_items`, `heroes`, `neutral_creeps`. Watch this list to auto-detect a new patch and invalidate cached rules.
- **Structural changes to know:**
  - 7.41 removed Facets. Innates no longer scale with abilities.
  - 7.40 gave talents their own talent points (levels 10/15/20/25/27-30) and added Largo (hero id 155).
- **Major map history:**
  - 7.38 "Wandering Waters": river streams, two Roshan pits in the river, Tormentors and Twin Gates in the map corners, Wisdom Shrines replace wisdom runes, corner outposts removed.
  - 7.40: streams extended into the bases with Defender's Gates, Wisdom Shrines moved to low ground in water, several Watchers removed or moved, Watcher night vision cut to 450.
  - 7.41: Tormentor areas moved to low ground near the Lotus Pools. The ancient camps at the stream ends became medium camps. The T1 safe-lane towers moved. Until 7:30, lane creeps meet closer to the offlane.

## 1. Best source: the installed game files (free, offline, always the right patch)
`~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota/pak01_dir.vpk` can be read with `uv run --with vpk --with vdf`. I checked each of these:

| File in VPK | Use |
|---|---|
| `scripts/creep_pull_timings.txt` | **Valve's own stack and pull windows for every camp** (sample below) |
| `scripts/npc/neutral_items.txt` | Neutral tier unlock times, craft costs, item and enchantment pools |
| `scripts/npc/items.txt` | Costs, stock and restock times, durations (Aegis, wards, smoke) |
| `scripts/npc/npc_units.txt`, `npc_abilities.txt`, `npc_heroes.txt` | Roshan and Tormentor stats, lotus/shrine radii, hero base stats |
| `game/dota/itembuilds/default_<hero>.txt` (129 files, plain KV) | Valve's recommended build per hero: Starting / Early / Mid / Late sections |

```python
import vpk, vdf
p = vpk.open(".../pak01_dir.vpk")
pulls = vdf.loads(p["scripts/creep_pull_timings.txt"].read().decode())["CREEP_PULL_TIMINGS"]
```

Sample from `creep_pull_timings.txt` (seconds past the minute):
- Most camps stack at **:53–:57**. Examples: good_2 52–54, good_8 52–54, evil_3 53–55, good_9 56–57.
- Pull windows:
  - good_1 (Radiant pull): 19–21 / 49–51.
  - good_2: Radiant 27–29 / 57–59, Dire 17–19 / 47–49.
  - evil_1: Radiant 19–20 / 49–50, Dire 27–29 / 57–59.
  - evil_2 (Dire): 21–23 / 51–53.
- Camp names match the in-game ALT labels when the convar `dota_hud_show_creep_spawn_camp_names 1` is on.

## 2. Web APIs

**OpenDota (`https://api.opendota.com/api`)**
- Limits are defined in odota/core `config.ts`, and live headers agree (`x-rate-limit-remaining-minute`, `-day`):
  - No key: **60 requests/min, 3000/day**.
  - With a key: 300/min. Calls above 3000/day are billed.
- Verified endpoints and response shapes:
  - `/heroes`: 127 heroes, `{id, name:"npc_dota_hero_antimage", localized_name, primary_attr, attack_type, roles[], legs}`.
  - `/heroStats`: per hero `1_pick..8_pick / 1_win..8_win` (Herald..Immortal buckets; the 8 bucket returned 0 for AM), `pub_pick/pub_win`, `turbo_picks/wins`, `pro_pick/win/ban`, plus base stats (attack_range, move_speed, day_vision/night_vision…). Use these winrates to label "weak/strong enemy heroes this patch".
  - `/heroes/{id}/itemPopularity`: `{start_game_items:{item_id:count}, early_game_items, mid_game_items, late_game_items}`. Keys are numeric item ids; map them with dotaconstants `item_ids.json`. The counts are small (for example 198), which suggests a pro or high-skill sample (UNVERIFIED).
  - `/heroes/{id}/matchups`: `[{hero_id, games_played, wins}]`. Samples are tiny (about 80 games), probably pro-only, so don't treat these as reliable counters.
  - `/benchmarks?hero_id=1`: `result.{gold_per_min, xp_per_min, last_hits_per_min, denies_per_min, kills/deaths/assists_per_min, hero_damage_per_min, hero_healing_per_min, tower_damage}`, each a list of `{percentile:0.1..0.99, value}`. Example for AM: last_hits_per_min p10=5.4, p20=7.0, p30=8.0.
  - `/scenarios/itemTimings?hero_id=1&item=bfury`: `[{time:720, games, wins}]`. Gives a "your BF is late" benchmark.
  - `/scenarios/laneRoles?hero_id=1`: winrate by `lane_role` and time bucket.
  - `/constants/{patch|items|abilities|hero_abilities|item_ids…}`. `/constants/patch` lists 7.41 as its newest entry (id 60).
- **odota/dotaconstants** raw JSON, last commit 2026-10-12: `https://raw.githubusercontent.com/odota/dotaconstants/master/build/items.json` (501 items, with `id, dname, cost, components, attrib, abilities, cd, mc, tier` for neutrals), plus `item_ids.json`, `heroes.json`, `hero_abilities.json`, `abilities.json`, `patch.json`.

**Valve datafeed (free, no key)**: `herolist`, `herodata?hero_id=`, `itemlist` (includes `neutral_item_tier` and `is_pregame_suggested` flags), `patchnoteslist`, `patchnotes`.

**Stratz GraphQL (`https://api.stratz.com/graphql`)**: **a bearer token is required.** An anonymous POST returns 403: "A bearer token is required… https://stratz.com/api". The token is free with a Steam login. Its rate limits, and whether it filters item builds by position or matchup, are UNVERIFIED (I couldn't load the page). Use it only for position- and bracket-filtered builds and matchups. OpenDota plus the local files are enough for v1.

**Strategy:** fetch everything once per patch at app start, cache to disk, and make zero API calls during a game.

## 3. Map timings and objectives (7.41f)

Sources are marked: **[PN]** official patch notes, **[GF]** game files, **[LP]** Liquipedia wikitext (fetched before it rate-limited me), **UNVERIFIED** not confirmed.

| Event | Rule |
|---|---|
| Bounty runes | 0:00: first pair at the two river power-rune spots (40 g per hero) [LP]. Then every **4:00** at the jungle bounty spots [PN 7.38]. Uncollected runes stack beside the new ones. Gold is fixed when the rune spawns (7.40). Scaling is 40 + 6 per interval: LP says the interval is 300 s, 7.38 notes say 4 min (UNVERIFIED which). |
| Water runes | **2:00 and 4:00** at both river spots, then never again. Each gives +40 HP and +80 mana [LP]. |
| Power runes | First at **6:00**, then every **2:00**, at one of the two river spots. No repeats within a cycle [LP]. Haste is a flat 22 s (7.40). |
| Wisdom Shrines (replaced wisdom runes in 7.38) | Activate every **7:00** (7:00, 14:00, …) in each offlane jungle. Stand within 300 units for 3 s [GF `countdown_time 3`, radius 300]. XP is **200, then +300 per subsequent shrine** (200/500/800…) (7.41). XP goes to a random hero in range plus the lowest-XP teammate. Since 7.41, an enemy hero in the area *reverses* the countdown. |
| Lotus Pools | One lotus per **180 s**, max **6** [LP]. Auto-pickup within 350 units, 1.5 s for the first [GF]. Great Lotus from **35:00**, Greater from **60:00** (follows the neutral T4/T5 times). Contesting reverses the countdown (7.41). |
| Day/Night | Day 0–5, night 5–10, and so on: **5-minute phases** (derived from the 7.38 notes: Roshan's first move is at 15:00 at nightfall, and a Night Stalker facet delayed it to 20). GSI exposes `map.daytime` and `map.nightstalker_night`, so read those instead of computing. Since 7.38 nobody gets bonus movement speed at night. |
| Roshan location | Two river pits. **7.41 swapped the pit preference.** He is in the **top pit 0:00–15:00**, then follows the cycle: **night → bottom (SE) pit, day → top (NW) pit**. He walks between pits and throws heroes he runs into. He respawns in the pit for the current time of day. This is inferred from the 7.38 rule plus the 7.41 swap, and Hawk.live and LP agree ("appears in the upper pit"). Treat it as verify-on-screen: the minimap shows only the active pit. |
| Roshan respawn | **8–11 min** after death (7.38 notes). The HUD icon beside the minimap is **red** (definitely dead, first 8 min), **yellow** (maybe alive, next 3 min) or **green** (alive). That is ideal for colour-based CV. |
| Aegis | **300 s** (Turbo 240 s), 5 s reincarnation [GF `disappear_time 300`]. |
| Roshan drops | Aegis every kill. Banner from the 2nd kill. Cheese and Refresher Shard from the 3rd kill on [LP]. |
| Tormentor | One active at a time. First spawn **15:00** [PN 7.38]. It teleports at each day/night switch and is always on the opposite side to Roshan: **day → SE corner, night → NW corner** [LP, matches the 7.41 swap]. Respawn is **10 min after death**: UNVERIFIED (LP's value is a template I couldn't resolve). Reward is an Aghanim's Shard; if everyone already has one, 415 g each (7.41). There is a Tormentor timer beside the minimap (7.40). |
| Neutral items [GF] | **T1 0:00** (6 Madstone, 4 artifact + 4 enchantment options). **T2 15:00, T3 25:00, T4 35:00, T5 60:00** (10 Madstone each, 5 + 5 options, T5 recraft 10). The Madstone cap is removed at **70:00**. You get Madstone by fully clearing camps, and you pick an artifact plus an enchantment. Since 7.41, enchantment options depend on your primary attribute. |
| Lane creeps | Upgrade every 7:30. Extra siege creep from **30:00** and again from **60:00** (7.41). Flagbearer gives 60 XP (7.41). |
| Wards [GF] | Observer: free, 2 in stock at start, max 4, restock 135 s, lasts 360 s. Sentry: 50 g, 3 at start, max 8, restock 70 s, lasts 420 s. Since 7.40 you can't plant a ward within 300 units of a same-type ward planted in the last second. |
| Smoke [GF] | 50 g, 2 at start, max 3, restock **420 s**, lasts **45 s**, broken by an enemy within **1025** units. |
| Glyph | Fortifies creeps for 3 s (7.38). Cooldown 300 s is UNVERIFIED. |
| Buyback | Cooldown 480 s. Cost about 200 + NetWorth/13 (UNVERIFIED; sources say /12 or /13). Read GSI's buyback fields rather than computing it. |
| Courier respawn | 45 s + 5 s per hero level (7.40). |

## 4. Rulebook to encode (deterministic, runs every tick)

Inputs:
- **GSI:** clock, daytime, gold, last hits/denies, level, items, alive state.
- **Minimap CV:** enemy positions and last-seen time, Roshan and Tormentor icons and colour.
- **Static tables:** the files and timings above.

```python
TIMERS = [  # (name, first_s, period_s or None, lead_s)
 ("Water rune",120,120,20), ("Bounty runes",240,240,20), ("Power rune",360,120,20),
 ("Wisdom shrine",420,420,30), ("Lotus +1",180,180,0), ("Stack window",53,60,4),
 ("Neutral tier",None,None,0),  # use [0,900,1500,2100,3600]
 ("Day/Night flip",300,300,15), ("Tormentor",900,None,30),
]
```

**Economy and laning (compare GSI against benchmarks)**
- **R1 Last hits.** Each minute, compare `last_hits / minute` with OpenDota `/benchmarks` for the hero: warn below p30, praise above p70.
  - Ceiling: a lane wave every 30 s, roughly 4 creeps each, so about 80 or more creeps reach the lane by 10:00 (UNVERIFIED composition).
  - Rough targets (heuristic, not data): core 45–60 LH at 10:00 in pubs, 70+ is excellent. Denies: about 10 at 10:00 is good mid, about 5 in side lanes.
- **R2 Item timing.** Use `/scenarios/itemTimings`. If the core item isn't done by the bucket where winrate drops, say "farm faster, skip fights".
- **R3 Stacking and pulling.** Supports with a free lane get a prompt at `stack_start - 4 s` for the nearest camp (from `creep_pull_timings.txt`). Pull prompts at :15/:45 (or the camp's exact window) only when the lane is pushed toward the enemy and before about 7:30.
- **R4 Neutral tier unlock.** At 0/15/25/35/60 min, and when there's enough Madstone, say "craft neutral".
- **R5 Ward and smoke stock.** Restock rules: observer 135 s, sentry 70 s, smoke 420 s. Remind supports when stock is full ("observer available, ward X"). Ward spots should be a hand-curated list of minimap coordinates keyed by game phase. No free machine-readable source exists, so this is the one manual dataset.

**Map danger (from minimap CV)**
- **R6 Missing enemies.** A hero is "missing" if unseen for 8 s or more.
  - If **3 or more enemies are missing**, you are in a side lane or jungle beyond your T1 or the river, and no ally is within about 1500 units: **"Back off / farm closer to tower"**.
  - If 4–5 are missing and Roshan is alive: **"Possible Roshan"**.
- **R7 Smoke detection.** Fire a 45 s smoke-gank alert when **2 or more enemies disappear from the minimap within about 3 s** of each other while grouped, especially at night or around 10/15/20-minute objective timers. Escalate if you're the closest isolated ally to their last-seen centroid.
- **R8 Night.** Most heroes see 800 at night against 1800 in daytime (OpenDota `day_vision/night_vision`). When a night phase starts, raise the danger weight (ganks are more likely). Night Stalker ult or `nightstalker_night` gets a separate, stronger warning.
- **R9 Roshan.**
  - Watch the HUD Rosh icon colour.
  - **Green and the enemy core has an item spike or 2+ enemies dead on our team →** "enemy likely taking Rosh".
  - **Our team is up 2+ kills, Rosh green, it's in the near pit** (day: top, night: bottom) → suggest Roshan.
  - **Red → yellow:** start the 3-minute "maybe alive" countdown.
  - **Aegis seen:** 5:00 countdown, and "don't fight the Aegis carrier until it expires" when we're behind.
- **R10 Tormentor and Roshan sides.** At each day/night flip, announce the new Rosh pit and Tormentor corner.
- **R11 Buyback.** If the enemy carry is dead with buyback likely, or our own `gold < buyback_cost` after 30 min, warn "save buyback" for cores.
- **R12 Runes.** Mid heroes: "go rune" 15–20 s before 6:00 and every even minute after; also before the 2:00/4:00 water runes. Everyone: bounty runes at 0:00 and every 4:00.
- **R13 Shrines and lotus.** At 7-minute multiples, the offlane or support goes to their shrine if it's safe (no enemy within about 2000 units on the minimap). Pools with lotuses waiting are contestable because contesting reverses the countdown.

**Draft and enemy analysis (once per game, pre-computed)**
- **R14 Weak enemy heroes.** Rank the enemy heroes by `heroStats` bracket winrate this patch and by low base armour / HP from `npc_heroes.txt`. Pick the lowest-durability, most-isolated enemy as the "kill target". Use the LLM only to phrase or summarise this.

## 5. Gaps and things to check in-game
- **Tormentor respawn (10 min?), Glyph cooldown (300 s?), buyback formula, bounty gold scaling interval:** UNVERIFIED. Confirm in-game or with `scripts/vscripts` once Liquipedia stops rate-limiting (its API allows about 1 parse every 30 s and requires gzip).
- **Roshan and Tormentor sides after the 7.41 swap** come from an inference that sources agree on. Let CV of the minimap icon be the source of truth and use the timing rule as the fallback.
- **The local client (Nov 05 2026 build) is newer than 7.41f.** Re-parse the VPK files at every launch. They are the only source that can never be out of date.

Sources: [Valve patch list](https://www.dota2.com/datafeed/patchnoteslist?language=english), [Valve 7.41 notes JSON](https://www.dota2.com/datafeed/patchnotes?version=7.41&language=english), [Liquipedia Version](https://liquipedia.net/dota2/Version), [Liquipedia Runes](https://liquipedia.net/dota2/Runes), [Liquipedia Roshan](https://liquipedia.net/dota2/Roshan), [Liquipedia Tormentor](https://liquipedia.net/dota2/Tormentor), [Hawk.live 7.41 Tormentor](https://hawk.live/posts/valve-reworked-the-tormentor-in-patch-741), [odota/core config.ts](https://github.com/odota/core/blob/master/config.ts), [odota/dotaconstants](https://github.com/odota/dotaconstants), [OpenDota API](https://api.opendota.com/api/heroStats), [teamsmurf buyback math](https://teamsmurf.com/buyback-math-when-2-500-gold-saves-the-game/), [win.gg buyback](https://win.gg/news/how-does-buyback-work-dota-2-lets-you-cheat-death-for-a-price/), [PlayNews 7.41e objectives guide (search result only, 403 when fetched)](https://www.playnews.gg/en/guides/dota-2-the-map-objectives-guide-for-patch-7-41e-roshan-the-gravekeeper-shrines-and-portals). Local files: `~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota/pak01_dir.vpk`, `.../game/dota/itembuilds/`. Working copies are in `<scratch>/vpkout/` (creep_pull_timings.txt, neutral_items.txt, items.txt, npc_units.txt).