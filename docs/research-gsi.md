# Dota 2 Game State Integration (GSI) for a player: findings report (checked against client build 6972, VersionDate Nov 05 2026, patch 7.41-era)

## 0. What matters most

1. **When you are playing (not spectating), GSI sends a `minimap` block with world coordinates for every unit your minimap shows.** That covers allies, enemy heroes your team can currently see (`image: "minimap_enemyicon"`), lane and neutral creeps in vision, towers, and Roshan when someone has vision of him. Two GSI libraries describe this behaviour (sources: [makquella/wardly](https://github.com/makquella/wardly) `backend/app/enemy_heroes.py` and `minimap_units.py`, last pushed 2026-11-09; maintainer comment in [antonpup/Dota2GSI#28](https://github.com/antonpup/Dota2GSI/issues/28)). The replica map and "missing hero" alerts can therefore be built from GSI, with no computer vision on screenshots. Screenshot CV would only be needed for things GSI does not give (see §6).
2. **Several things are spectator-only:** `draft`, `roshan`, `couriers`, `neutralitems`, `league`, map-level `roshan_state`/`tormentor_*`/glyph/scan/`radiant_win_chance`, and data for players other than you. A player gets their own hero, items and abilities in full, their own team's buildings only, plus `events` and `minimap`.
3. **GSI is off unless Dota is launched with `-gamestateintegration`** (required since the [March 11, 2022 update](https://steamcommunity.com/games/dota2/announcements/detail/4491783379124370819)). Your Steam `localconfig.vdf` (userdata/<account-id>) has **no launch options for app 570 yet**, and the `cfg/gamestate_integration/` folder does not exist yet.

## 1. Setup on macOS (checked locally)

The client library `game/dota/bin/osx64/libclient.dylib` (x86_64) contains `cfg/gamestate_integration/gamestate_integration_*.cfg` and the log line `Loading Game State Integration: %s`. So GSI is compiled into the macOS build and loads config files by glob when the game starts.

- **Folder:** `~/Library/Application Support/Steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/` (you need to create it).
- **File name:** `gamestate_integration_<anything>.cfg`, for example `gamestate_integration_coach.cfg`.
- **Launch option:** Steam → Dota 2 → Properties → Launch Options → `-gamestateintegration`.
  - Overwolf's guide (updated Nov 2025) still requires it.
  - I could not find the flag as a plain string in the macOS binaries, so I couldn't confirm it from the binary. UNVERIFIED whether this build still needs it, but adding it is harmless.
- **Restart Dota** after creating or editing the cfg. Files are read only at startup.
- **Ticketed/tournament games:** GSI data arrives delayed ([Dota Coach FAQ](https://dotacoach.gg/en/app/faqs)).

Recommended config. Every provider is turned on, because a provider the client can't supply is simply skipped. The binary has the log string `%s: data for '%s' v%s cannot be provided.`, so the console tells you which ones were dropped.

```
"coach"
{
    "uri"        "http://127.0.0.1:3000/gsi"
    "timeout"    "5.0"
    "buffer"     "0.0"
    "throttle"   "0.1"
    "heartbeat"  "2.0"
    "auth"       { "token" "CHANGE_ME_random_hex" }
    "data"
    {
        "provider" "1"  "map" "1"  "player" "1"  "hero" "1"  "abilities" "1"
        "items" "1"  "events" "1"  "buildings" "1"  "minimap" "1"  "wearables" "1"
        "draft" "1"  "roshan" "1"  "couriers" "1"  "neutralitems" "1"  "league" "1"
    }
}
```

What the endpoint settings mean, from Valve's CS:GO GSI docs (mirrored in [anthonytournier/csgo-gsi-to-arduino](https://github.com/anthonytournier/csgo-gsi-to-arduino)). Dota uses the same engine code; the binary has the keys `timeout|buffer|throttle|heartbeat|event_timeout|auth`.

| Key | Meaning |
|---|---|
| `uri` | The address Dota POSTs JSON to. |
| `timeout` | How long Dota waits for a 2XX reply. Only one request is in flight at a time. After a timeout, the next request is full state with no delta. Default 1.1 s. |
| `buffer` | Seconds Dota collects changes before sending one bigger delta. Default 0.1 s; 0.0 turns it off. |
| `throttle` | Minimum seconds between requests after a 2XX reply. Default 1.0 s. |
| `heartbeat` | Dota sends anyway after this many seconds with no change. Keeps the app fresh during pauses; Wardly uses 2.0 and treats state older than 5 s as stale. |
| `event_timeout` | New key. Meaning UNVERIFIED. |

- **Data values:** `"1"` is the provider **version**. The binary maps `minimap` to `minimap_v1` via `%s_v%s`.
- **Auth:** the token is echoed back in every payload as `auth.token`. Check it on the server.
- **Reply 200 quickly.** If you never see `previously`/`added` blocks, Dota isn't getting 2XX replies.
- **Real update rate:** about 1–2 Hz. One user found throttle/buffer below about 1 s had no effect for Dota ([antonpup/Dota2GSI#38](https://github.com/antonpup/Dota2GSI/issues/38)); GankMeDaddy claims about 0.5 s. Plan for ~1 Hz.

Smallest working receiver, Python stdlib only:

```python
import json, http.server
TOKEN = "CHANGE_ME_random_hex"
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200); self.end_headers()          # ack first
        if body.get("auth", {}).get("token") != TOKEN: return
        on_state(body)                                       # your handler
http.server.ThreadingHTTPServer(("127.0.0.1", 3000), H).serve_forever()
```

## 2. Every field name in the current build

I pulled these strings from `libclient.dylib` (build 6972). This is the master list. Whether each one is sent to a player is covered in §3.

- **map:** `name matchid game_time clock_time daytime nightstalker_night radiant_score dire_score game_state paused win_team customgamename ward_purchase_cooldown radiant_ward_purchase_cooldown dire_ward_purchase_cooldown roshan_state(alive|respawn_base|respawn_variable) roshan_state_end_seconds tormentor_state(has_not_spawned|alive|respawning) tormentor_state_end_seconds tormentor_state_location radiant/dire_glyph_cooldown radiant/dire_scan_cooldown radiant/dire_scan_charges radiant/dire_wisdom_shrine radiant/dire_lotus_pool_count watchers{watcher%d{location_x,location_y,capture_state(not_captured…)}} radiant_win_chance`
- **player:** `steamid accountid name activity(menu|playing) kills deaths assists last_hits denies kill_streak commands_issued kill_list{victimid_%d} team_name player_slot team_slot gold gold_reliable gold_unreliable gold_from_hero_kills gold_from_creep_kills gold_from_summon_kills gold_from_income gold_from_shared gpm xpm onstage_seat net_worth hero_damage hero_healing tower_damage wards_purchased wards_placed wards_destroyed runes_activated water_runes_activated bounty_runes_activated camps_stacked support_gold_spent consumable_gold_spent item_gold_spent gold_lost_to_death gold_spent_on_buybacks wisdom_shrines_taken damage_{received|outgoing}_{pre|post}_reduction_{physical|magical|pure}`
- **hero:** `facet xpos ypos id name level xp alive respawn_seconds buyback_cost buyback_cooldown health max_health health_percent mana max_mana mana_percent silenced stunned disarmed magicimmune hexed muted break aghanims_scepter aghanims_shard smoked permanent_buffs has_debuff selected_unit talent_1..8 attributes_level`
- **abilities:** `ability%d{name level can_cast passive ability_active cooldown max_cooldown ultimate charges max_charges charge_cooldown}`
- **items:** `slot0-8` (0–5 inventory, 6–8 backpack), `stash0-5`, `teleport0`, `neutral0` (neutral item), `neutral1` (enhancement), `preserved_neutral%d`, `additional_inventory_%s`. Each item has `name purchaser item_level contains_rune can_cast cooldown max_cooldown passive charges item_charges ability_charges max_charges charge_cooldown`. Empty slots are `"name":"empty"`.
- **events:** `courier_killed roshan_killed aegis_picked_up aegis_denied tip bounty_rune_pickup chat_message generic_event` (details in §4).
- **minimap:** `o<N>{xpos ypos image team yaw unitname visionrange name remainingtime eventduration}`
- **roshan:** `health max_health alive spawn_phase phase_time_remaining xpos ypos yaw items_drop{item%d}`
- **couriers:** `courier%d{health max_health alive respawn_time_remaining xpos ypos yaw owner flying_upgrade shield boost items}`
- **neutralitems:** includes the newer crafting fields `madstone_required escalating_recraft_cost current_madstone total_madstone crafting_tier times_crafted trinket_choices enchantment_choices`.
- **draft:** `activeteam pick activeteam_time_remaining radiant_bonus_time dire_bonus_time team%d{home_team pick%d_id pick%d_class ban%d_id ban%d_class}`
- **buildings, wearables (`wearable%d`, `style%d`), league, provider, `previously`, `added`.**

## 3. What a player actually receives

| Block | Sent while playing? | Notes |
|---|---|---|
| provider | yes | `{"name":"Dota 2","appid":570,"version":47,"timestamp":<unix>}` |
| map | yes, reduced | Player gets **`ward_purchase_cooldown`** (own team) instead of radiant_/dire_. `roshan_state`, `tormentor_*`, glyph/scan, shrine/lotus, `watchers` and `radiant_win_chance` are modelled only in the spectator types ([MrBean355 PlayingMapImpl vs SpectatedMapImpl](https://github.com/MrBean355/dota2-gsi), captured fixtures; dotabod's player `MapData` matches). The key's presence is how libraries detect player mode. |
| player | yes, you only, flat object | Fixture fields stop at `gpm/xpm`. Dotabod marks `net_worth`, `hero_damage`, `wards_*`, `camps_stacked`, gold-spent etc. as "additional fields in spectating mode". The new damage/rune breakdowns are UNVERIFIED for players. `team_name` is `radiant|dire|spectator`. **Do not derive team from `player_slot`** (dotabod saw it reshuffled live). |
| hero | yes, you only | Includes `xpos/ypos`, `facet`, talents. `selected_unit` is spectator-only. `id:-1` before you pick. |
| abilities | yes, you only | Filled from PRE_GAME on. |
| items | yes, you only | Bottle has `contains_rune` (`regen`, `arcane`, `double_damage`, `haste`, `illusion`, `invis`, `water`, `bounty`, `shield`, `xp`). |
| buildings | **your team only** | Keys like `dota_goodguys_tower1_top`, `good_rax_melee_mid`, `dota_goodguys_fort` with `{health,max_health}`. Enemy towers have to come from minimap tower icons. |
| events | yes | See §4. |
| **minimap** | **yes** | See below. |
| draft | **no**, arrives as `{}` | Spectator-only ([tomasfarias/dota-gsi](https://github.com/tomasfarias/dota-gsi) fixtures; Wardly). |
| roshan | no | Maintainer of antonpup/Dota2GSI: "you cannot get other Roshan information when playing". Use the minimap entry `unitname:"npc_dota_roshan"` while you have vision, plus `roshan_killed` events. |
| couriers, neutralitems, league | likely no | UNVERIFIED. Check the console for "cannot be provided". |
| wearables | yes, you only | Cosmetic; ignore. |
| previously / added | yes | `previously` holds the old values of fields that changed; `added` marks new keys with `true`. Diffs come for free. |

**How the player minimap behaves** (Wardly, checked on real recordings):

- **Your heroes:** allies are `image:"minimap_herocircle"` and you are `minimap_herocircle_self`.
- **Enemy heroes:** `minimap_enemyicon` with `team` 2 or 3, **only while visible**. Wardly remembers each sighting to build the enemy lineup over the match.
- **Ignore `minimap_plaincircle` at (0,0).** From strategy time on, Dota sends those for every pick and even for heroes not in the match.
- **Ignore anything at (0,0)** in general; it isn't placed on the map yet.
- **Creeps:** your lane creeps always; enemy lane creeps (`minimap_creep`/`minimap_siege`) and neutrals (`team 4`, `npc_dota_neutral_*`) only when seen.
- **Towers:** `npc_dota_{goodguys|badguys}_tower{1-4}_{top|mid|bot}` with `minimap_tower90/45`.
- **Vision:** your own units carry `visionrange`.
- **Keys `o123` are not stable IDs.** The same key can point to a different unit a moment later, so track units by `unitname` and position.
- UNVERIFIED: whether enemy illusions and wards appear, and the exact `image` names for them.

**Coordinates:**
- World units, origin (0,0) at map centre.
- Radiant base is negative x/y (bottom-left), Dire base positive (top-right).
- Playable range is roughly **±8200**; Wardly rejects |v| > 12000 as broken.
- Replay/parser cells = world + 16384.
- Same system for `hero.xpos/ypos`, minimap, couriers and roshan.
- Fixture example: Oracle at (-1664, -1216). Roshan minimap sample: (-7808, 7680).

## 4. Events

`events` is an array of entries like `{game_time, event_type, ...}` with type-specific fields. Entries can repeat across payloads, so dedupe on `(event_type, game_time, ids)`.

| event_type | Fields | Notes |
|---|---|---|
| `roshan_killed` | `killed_by_team`, `killer_player_id` | Not `player_id`. |
| `aegis_picked_up` | `player_id`, `snatched` | |
| `aegis_denied` | `player_id` | Assumed shape. |
| `tip` | `sender_player_id`, `receiver_player_id`, `tip_amount` | |
| `bounty_rune_pickup` | `player_id`, `team`, `bounty_value`, `team_gold` | |
| `courier_killed` | `courier_team`, `killer_player_id`/`killed_by_team`, `owning_player_id` | Spectator-only per dotabod. |
| `chat_message` | `player_id`, `channel_type` (11/12 = ally/all), `message` | Dotabod has handled it since 2025-09-27. |
| `generic_event` | `data` = JSON string `{type:"CHAT_MESSAGE_*", value, playerid1..6, value2, value3, time}` | Dotabod since 2025-10-01. This is the in-game kill-feed/system-message stream. |

- `generic_event` types include `CHAT_MESSAGE_SMOKE_ACTIVATED` (**own team only**), `CHAT_MESSAGE_BUYBACK`, `CHAT_MESSAGE_TOWER_KILL`, `CHAT_MESSAGE_BARRACKS_KILL`, and more. The full enum is in [dotabod/backend `packages/dota/src/types.ts`](https://github.com/dotabod/backend).
- Player IDs 0–4 are Radiant and 5–9 Dire in events.
- Roshan respawn window after a kill: 8–11 minutes; Aegis lasts 5 minutes (Wardly `data/meta/map_timers.json`, patch 7.41f).

## 5. Map/state semantics

- **`game_state` values:** `DOTA_GAMERULES_STATE_` + `INIT`, `WAIT_FOR_PLAYERS_TO_LOAD`, `CUSTOM_GAME_SETUP`, `HERO_SELECTION`, `STRATEGY_TIME`, `TEAM_SHOWCASE`, `WAIT_FOR_MAP_TO_LOAD`, `PRE_GAME`, `GAME_IN_PROGRESS`, `POST_GAME`, `DISCONNECT`, `LAST`.
- **Two clocks:**
  - `clock_time` is the in-game clock. It is negative during pre-game (dotabod sample: -28) and is what the HUD shows.
  - `game_time` runs from loading. Fixture: game_time 891 vs clock 713, so it includes draft and strategy time.
  - Use `clock_time` for rune, Roshan and stacking timers.
- **`daytime`, `nightstalker_night`:** booleans.
- **`win_team`:** `none|radiant|dire`.
- **`paused`:** boolean.
- **`matchid`:** a string. It is `"0"` in hero demo and lobbies.
- **Game type:** there is **no `game_mode` field** in this build.
  - `map.name` is `"start"` on the normal map, or `hero_demo_main` / `last_hit_trainer`.
  - A non-empty `customgamename` means arcade or demo.
  - Telling Ranked, All Pick and Turbo apart needs an outside source: the OpenDota/Steam API by `matchid` after the game, or screen OCR. UNVERIFIED whether any live API exposes the mode.

## 6. Composite player payload, mid-game

Built from the captured fixtures (MrBean355, antonpup, Wardly). Field shapes are real; values are illustrative.

```json
{"provider":{"name":"Dota 2","appid":570,"version":47,"timestamp":1794243000},
 "map":{"name":"start","matchid":"8512345678","game_time":1071,"clock_time":893,"daytime":false,
  "nightstalker_night":false,"radiant_score":12,"dire_score":17,"game_state":"DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
  "paused":false,"win_team":"none","customgamename":"","ward_purchase_cooldown":37},
 "player":{"steamid":"7656119…","accountid":"<account-id>","name":"me","activity":"playing","kills":3,"deaths":2,
  "assists":5,"last_hits":121,"denies":9,"kill_streak":0,"commands_issued":4211,"kill_list":{"victimid_7":2,"victimid_9":1},
  "team_name":"radiant","player_slot":1,"team_slot":1,"gold":1350,"gold_reliable":420,"gold_unreliable":930,
  "gold_from_hero_kills":640,"gold_from_creep_kills":4520,"gold_from_income":1180,"gold_from_shared":210,"gpm":512,"xpm":601},
 "hero":{"facet":2,"xpos":-1664,"ypos":-1216,"id":8,"name":"npc_dota_hero_juggernaut","level":13,"xp":9120,"alive":true,
  "respawn_seconds":0,"buyback_cost":1210,"buyback_cooldown":0,"health":1310,"max_health":1720,"health_percent":76,
  "mana":210,"max_mana":540,"mana_percent":39,"silenced":false,"stunned":false,"disarmed":false,"magicimmune":false,
  "hexed":false,"muted":false,"break":false,"aghanims_scepter":false,"aghanims_shard":false,"smoked":false,
  "permanent_buffs":{},"has_debuff":false,"talent_1":true,"talent_2":false,"talent_3":false,"talent_4":false,
  "talent_5":false,"talent_6":false,"talent_7":false,"talent_8":false,"attributes_level":0},
 "abilities":{"ability0":{"name":"juggernaut_blade_fury","level":4,"can_cast":true,"passive":false,"ability_active":true,"cooldown":0,"ultimate":false}},
 "items":{"slot0":{"name":"item_power_treads","purchaser":1,"passive":false,"can_cast":true,"cooldown":0},
  "slot1":{"name":"empty"},"teleport0":{"name":"item_tpscroll","purchaser":1,"can_cast":true,"cooldown":0,"charges":1},
  "neutral0":{"name":"item_keen_optic","purchaser":-1,"passive":true},"neutral1":{"name":"empty"}},
 "buildings":{"radiant":{"dota_goodguys_tower1_top":{"health":1795,"max_health":1800}}},
 "minimap":{"o12":{"xpos":-1664,"ypos":-1216,"image":"minimap_herocircle_self","team":2,"yaw":40,"unitname":"npc_dota_hero_juggernaut","visionrange":1200},
  "o57":{"xpos":1200,"ypos":-300,"image":"minimap_enemyicon","team":3,"yaw":200,"unitname":"npc_dota_hero_lion","visionrange":0},
  "o109":{"xpos":-7808,"ypos":7680,"image":"minimap_creep","team":4,"yaw":347,"unitname":"npc_dota_roshan","visionrange":1400}},
 "events":[{"game_time":2333,"event_type":"roshan_killed","killed_by_team":"dire","killer_player_id":7}],
 "draft":{},
 "previously":{"hero":{"health":1402}},
 "auth":{"token":"CHANGE_ME_random_hex"}}
```

## 7. Gotchas and gaps

- **Gaps:**
  - No enemy items, levels, HP or gold.
  - No enemy draft.
  - No fog-of-war data: an enemy that isn't visible is simply absent, so the app must remember last-seen position and time itself.
  - No enemy smoke notice (`SMOKE_ACTIVATED` only reaches your own team).
  - No courier or ward state.
  Screen CV is only worth building for these: enemy lineup from the top-bar portraits (earlier than minimap sightings), enemy items via scoreboard/tab OCR, and the kill feed. Minimap pixel parsing for hero positions is **redundant with GSI** and can be skipped in v1. Your settings (`dota_minimap_show_hero_icon` etc.) don't affect GSI.
- **Sanity checks:** ignore payloads where `player.activity != "playing"`, where `customgamename` is non-empty (unless you are testing in demo mode), or where `team_name == "spectator"`.
- **Missing fields are normal.** Fields come and go between packets (`hero.id = -1`, empty objects before PRE_GAME). Null-check everything; libraries use -1 or empty defaults.
- **Older libraries are behind.** antonpup/Dota2GSI was last updated Feb 2024 and xzion/dota2-gsi Dec 2017; neither knows `chat_message`, `generic_event`, `neutral1`, the madstone fields or the tormentor fields. The best current reference is **Wardly** (MIT, Python FastAPI, GSI-only live coach, active 2026-11-09). Reuse its parsers and timer data rather than writing them from scratch. Dotabod's types are the best reference for events.

Sources: [antonpup/Dota2GSI](https://github.com/antonpup/Dota2GSI) · [xzion/dota2-gsi](https://github.com/xzion/dota2-gsi) · [MrBean355/dota2-gsi](https://github.com/MrBean355/dota2-gsi) (captured fixtures) · [tomasfarias/dota-gsi](https://github.com/tomasfarias/dota-gsi) · [dotabod/backend](https://github.com/dotabod/backend) · [makquella/wardly](https://github.com/makquella/wardly) · [psikoma/GankMeDaddy](https://github.com/psikoma/GankMeDaddy) · [Valve March 11, 2022 update](https://steamcommunity.com/games/dota2/announcements/detail/4491783379124370819) · [Liquipedia launch options](https://liquipedia.net/dota2/Launch_Options) · [Overwolf GSI guide](https://support.overwolf.com/support/solutions/articles/9000212745-how-to-enable-game-state-integration-for-dota-2) · [Dota Coach FAQ](https://dotacoach.gg/en/app/faqs) · local `libclient.dylib` strings (build 6972).

Cloned repos and the binary strings dump are in `<scratch>/src/` and `<scratch>/libclient_strings.txt` (GSI block is around lines 605130–605350).