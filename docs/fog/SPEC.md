# Fogwatch fog layer: implementation spec (lead engineer, 2026-11-16; amended after the skeptic pass, see section 8)

Goal (the user's words): the on-screen overlay should be "very helpful in predicting when and where enemy heroes might be".
Everything here was chosen on harness numbers. All paths below are under
`<scratch>/ws/` (`<ws>`) unless they
start with a project file name (project = `<project>/`).

Reference code and measurements written for this spec (all in `<ws>/lead/`):

| File | What |
|---|---|
| `fog_ref.py` | First reference implementation of the backend. **Superseded by `<ws>/skeptic/fog_ref2.py`** (amendments 1-2); kept for the before / after numbers only. |
| `world_death.diff`, `w2/world.py` | Validated patch of `world.py` for the death rules (section 2.4): a diff against today's file, and the patched copy. |
| `display_ref.py` -> `display_tracks.txt`, `display_tracks2.txt` | Acceptance harness, first version: replays `build()` per second, scores what the player would see, next to today's rules. `tracks` = today's world.py, `tracks2` = patched world.py. Its outputs describe the superseded reference. |
| `fog_pred.py` -> `eval_fog_both.txt`, `eval_baseline_both.txt` | Adapter for `predict_eval.py eval`, and the baseline on both track sets. |
| `deaths.py`, `deaths2.py`, `miss.py` -> `deaths.txt`, `deaths2.txt`, `deaths2_patched.txt`, `miss_patched.txt` | world.py's enemy `dead` flag against GSI kill events. |
| `dump.py`, `ana1.py`..`ana4.py` -> `ana*_*.txt` | Why no predicted (moved) marks: precision of the grid model's best point; ghost confidence models. |
| `tiers.py`, `tiers2.py`, `mk_table.py` -> `tiers.txt`, `tiers2.txt`, `odds_table.json` | Cue thresholds on TEST; the shipped odds table. |
| `pidmap.py` -> `pidmap.txt` | Player id -> hero cannot be learned online safely (rejected). |
| `tracks2/` | All 19 games re-extracted through the patched world.py. |

Skeptic pass (all in `<ws>/skeptic/`; these are the files to port and the numbers to hit):

| File | What |
|---|---|
| `fog_ref2.py` | **The reference implementation A ports to `fog.py`** (section 2.2-2.3). Stdlib only, `demo()` passes, 0.10 ms per call. |
| `display_ref2.py` -> `display_amended_tracks2.txt`, `display_amended_tracks.txt` | **The acceptance harness A ports** (`display_ref.py` + the "skeptic lines"), and its output on the patched / today's tracks. |
| `sk1.py`..`sk7.py` -> `sk*.txt` | The measurements behind the amendments: cue flicker and who really arrives (`sk1`, `sk2_test`, `sk5`), 4 Hz icon dropouts (`sk3`), brief sightings and lit / dark on test (`sk4`), summon hints and strip churn (`sk6`), CV-only sightings in live recordings (`sk7`). |
| `SPEC_before_skeptic.md` | This document before the amendments. |

---

## 0. Decisions and the evidence

| Question | Decision | Evidence |
|---|---|---|
| Grid belief (recursive filter or stateless kernel x prior x wave x mates)? | **Not in this build.** Tables only. | The grid's gain is real at "best 10 % of the map" (hit 88 % vs 75 %), but that is a region of about 2700 u radius, which cannot be drawn as a mark. Its best single point, when it claims 50-70 % of the mass, is within 1000 u of where the hero shows up only **36-40 %** of the time (train, leave one game out; `ana1_train.txt`, `ana2_train.txt`). As ghost confidence, its mass is worse than a two-column age table (test Brier 0.173 vs 0.152, `ana3.txt`). For WHEN it ties the odds table (0.0813 vs 0.0814). |
| Move a ghost to a predicted spot (wave / tower / mates) for long-unseen or respawned heroes? | **No.** A ghost never moves, except to a fresh summon. | Moved peaks at 0.5-0.7 claimed mass: 38 % within 1000 u, 50 % within 1500 u. Respawn: 30-43 % within 1500 u at best. A mark that is wrong 6 times in 10 is the "random" complaint. |
| Hide or fade a ghost on ground my team lights? (belief said yes, glance said no) | **Keep it, and trust it more.** | A ghost whose spot is inside my vision is right more often at every age: 3-5 s 81 % vs 47 % dark; 5-8 s 63 % vs 32 %; 8-12 s 43 % vs 25 % (within 1000 u, train). The hero is in trees or uphill next to my units. The harness's "markers in plain sight" metric does not apply to last-seen ghosts and is dropped as a target. |
| Ghost confidence | Table of unseen time x (spot lit / dark), never rising within one unseen stretch. | Test Brier 0.152 vs 0.162 for age alone. Drawn at conf >= 0.30. Lit / dark holds on test too: 61 % vs 19 % at 5-9 s, 41 % vs 15 % at 9-20 s (`sk4.txt`). |
| Which sightings earn a ghost? | **Every real sighting** (amended: the first version wanted the hero visible for 2 s first). Only a respawn has no position. | 36 % of the reappearances a ghost could be scored on follow a sighting shorter than 2 s (world's clock is whole seconds, so "2 s" was 2-3 s), and those ghosts are as right as the others: 74 % vs 73 % within 1000 u (`sk2_test.txt`, `sk4.txt`). With the old gate 38 % of cue seconds pointed at a hero with no mark on the minimap. Flicker is handled by time unseen, not by time seen. |
| Anything on screen in the first 2 s unseen? | **No.** Ghost and cue both start at 2 s. | Enemy icons drop out of GSI for under 2 s and come back 5.9-6.8 times per game minute, 10-22 times per 10 min within 1200 u of me (4 Hz payloads, 2 games, `sk3.txt`). The 1 Hz harness cannot see the sub-second ones. In the first version 79 % of close cues started on a hero unseen for under 2 s and 49 % of all cues lasted 2 s or less (`sk1.txt`). |
| Traces (conf 0.12-0.30), reach rings, zone discs, "?" | **Cut.** | A trace is right 12-18 %. The ring at 10-20 s is 7-16 % of the map. |
| Arrival odds | Table `[fight][distance][unseen time]` (glance's stand-in), made non-increasing in unseen time after 10 s. | Test Brier 0.0814 (always-base-rate 0.0891, today's rule 0.1321). Adding lit, belief mass or the logistic extras moved it by <= 0.0003. Calibrated on test: 0.3-0.5 -> 30 %, 0.5-0.7 -> 55 %. |
| ETA number | **Cut** (field and label). | Earliest arrival is 0 in 97 % of cue seconds (glance E5). The cue itself is the "when". Honest odds after the 2 s gate (test): while a close cue is up somebody shows up within 1200 u in 10 s 43 % of the time and the cued hero itself 30 %; 54 % of close cues see their hero arrive within 15 s of the cue's start (faint: 31 % / 20 % / 35 %; no cue: 16 %). The first version's "1 in 2" counted any hero and the first 2 s after a hero stepped out of sight. |
| Cue tiers | Faint at p >= 0.20, close at p >= 0.40, never before 2 s unseen, 15 s life, at most two. | Test, patched tracks: cue time 21 % of alive time (today 46 %), 23.5 cues per 10 min counted per hero (first version 32.7), somebody arrives 43 % / 31 % (today's arrow 28 %). The gate costs 2 points of cold recall (46 -> 44 %) and 2 of 52 pre-cued deaths. |
| Enemy death state | **Kill events decide deaths; a timer decides respawns.** In scope, because it decides who is on the fog layer at all. | Today a living, unseen enemy is flagged dead for 505 hero-seconds per game and a dead one is shown as missing for 316 (about 13 % and 8 % of the layer's subject time). Patched: 7 and 59 (`deaths2*.txt`). |
| Player id -> hero mapping | **Rejected.** Kill events are used anonymously. | Learned online it is ready at a median clock of 9:51 and was wrong for 4 heroes in one of 19 games (`pidmap.txt`). |

Not measured on a non-Turbo game: there is none in the recordings.

---

## 1. Outcome: what the player sees

Three elements, all grey or amber. Red, the pill, the glow and the voice stay with enemies in sight (existing R1), unchanged.

| Situation | On the minimap | Strip above the minimap | Screen edge |
|---|---|---|---|
| **Calm** (all enemies in sight or dead) | Nothing | Empty | Nothing |
| **An enemy just left sight** (however briefly it was seen) | Nothing for 2 s (icons flicker). Then its real hero icon appears where it left sight, greyed, at half strength from 2 s and full from 3 s. It fades out as the spot loses value: gone after about 9 s on dark ground, up to about 25 s when the spot is inside my team's vision (hiding in trees / uphill). | From 8 s unseen: a chip with the hero icon and the unseen time, in steps (`8s` `9s` `10s` `15s` ... `30s` `45s` `1m` `2m`). | Nothing |
| **Enemies missing for long, or respawned** | Nothing. No position is credible, and none is invented. | Chip per hero with the time since it was last located (or since its respawn). Fixed slot per hero. | Nothing |
| **One could reach me soon** (never in its first 2 s unseen) | That hero's ghost gets an amber ring, and stays drawn for as long as the cue is up even when it would have faded: every arrow has its mark on the map. | Its chip turns amber. | Amber arrow toward its last spot. Faint (small, translucent, icon only): somebody shows up within 10 s about 3 times in 10, that hero 2 in 10. Close (full size, icon, a bar that steps down over the cue's 15 s life): somebody 4 in 10, that hero 3 in 10; in half of the close cues the hero is on me within 15 s. At most two cues. When the hero does arrive it comes from within 45 degrees of the arrow 7-8 times in 10 (`sk1.txt`). |
| **Contradicted ghost** | Seen anywhere: ghost, chip and cue vanish in that frame (Dota draws the real icon). Killed: the same. Its summon appears elsewhere: the old ghost fades out, a dashed-ring ghost fades in at the summon. My team walks over the spot and nobody is there: nothing special, the ghost keeps fading on its curve (measured: such ghosts are right more often, not less). A ghost that has faded does not come back until the hero is seen again, with one exception: if a cue starts for that hero later (I walked toward its last spot), its mark returns with the amber ring for as long as the cue is up. | | The cue ends when the hero is seen, dies, or its bar runs out; it does not return for the same unseen stretch. |
| **All seen** | Nothing from the fog layer. | Empty | Red arrows as today. |
| **I am dead** | Ghosts stay (today the overlay goes blank). | Chips stay. | No cues. |

Removed from the overlay: the dashed "?" reach circles, the `? ~Ns` / `? NEAR` arrows, the red ping for a "missing" guess.

---

## 2. Backend

### 2.1 Files

| File | Change |
|---|---|
| `fog.py` (new, about 110 lines, stdlib only) | Port of `<ws>/skeptic/fog_ref2.py`: constants, `odds`, `lit`, `build`, `demo`. |
| `world.py` | (a) collect my team's vision sources; (b) death rules of 2.4 = apply `<ws>/lead/world_death.diff`. |
| `rules.py` | One hook (2.5). `danger()` is not touched. |
| `predict_eval.py` | Track column `resp`, hint-flag fix, built-in predictor `fog`, sub-command `display` (2.6). |
| `tools.py` | Selfcheck additions (5.3). |

No model file, no numpy in the app path, no new dependency.

### 2.2 Evidence sources

| Evidence | Read from | Used for |
|---|---|---|
| Last sighting | `W.heroes[h].last_xy`, `mia_s` (world.py, as today). `vanished_at` is **not** read by the fog layer | Ghost position, age |
| Summon hint | `hint_at` present (world.py:296-308, now cleared on a sighting) | Ghost relocation, `src: "hint"` |
| Respawn | `resp_at` present (new, 2.4) | `src: "respawn"`: no map mark, no cue |
| Death | `dead` (now driven by `CHAT_MESSAGE_HERO_KILL`, 2.4) | No entry |
| My team's vision | every own-team minimap object with `visionrange > 0` -> `W["_"]["sight"] = [(x, y, r), ...]`, rebuilt per payload | "Lit" column of the confidence table |
| Fight context | an enemy hero visible within 2000 u of me (`W.heroes`) | Fight half of the odds table |
| Me | `W.me.x / y / alive` | Distance, cue gating |

`W["_"]["sight"]`: in the `_merge_gsi` minimap loop, for each object with a position (not None, not (0,0), `max(abs(x), abs(y)) <= 12000`), `TEAM.get(num(o.get("team"))) == team`
and `num(o.get("visionrange"), 0) > 0`, append `(x, y, visionrange)`. 41-74 rows per payload. Do not reuse the name `W["_"]["vis"]` (taken).
The append goes **before** the loop's existing `continue` (the one that skips `GHOST` images and pings): wards, buildings and my team's
illusions (`minimap_heroimage`, visionrange 400 / 800) are vision sources, and the harness's `obs.vision()` counts them the same way.

### 2.3 Algorithm: `fog.build(heroes, me, c, sight, st) -> list`

Exact code: `<ws>/skeptic/fog_ref2.py`. Per enemy hero with a known team:

1. Skip (and drop its state) when `visible`, `dead`, no `last_xy`, or `mia_s is None`.
2. `src` = `"respawn"` if `resp_at` is set, else `"hint"` if `hint_at` is set, else `"seen"`. `age = mia_s`.
3. State per hero in `st[hero]`, reset when `src` changes or the evidence time `c - age` moves by more than 1.5 s (a new unseen stretch).
4. **conf** = 0 for `src == "respawn"`. For `seen` and `hint` (any real sighting, however short, or a fresh summon) piecewise-linear in `age`:

   | unseen s | 2 | 3.5 | 5 | 7 | 8 | 9 | 11 | 14 | 20 | 30 | 45 |
   |---|---|---|---|---|---|---|---|---|---|---|---|
   | dark | .85 | .55 | .36 | | .30 | | .22 | .16 | .12 | | .08 |
   | lit | .92 | .80 | .68 | .62 | | .50 | .42 | | .36 | .27 | .10 |

   Lit = `last_xy` more than 300 u inside any disc of `sight`, evaluated now. Hint: capped at 0.45.
   `conf = min(previous conf of this stretch, value)`: it never rises, so a faded ghost cannot return and a spot that becomes lit later only slows the fade.
5. **p10** = `ODDS[fight][distance(last_xy, me) bin][age bin]`, 0 while I am dead or have no position.
   Edges: distance 0, 800, 1200, 1600, 2000, 2500, 3000, 4000, 5000, 7000 u; age 0, 2, 4, 7, 10, 15, 20, 30, 45 s. Table: `<ws>/lead/odds_table.json`
   (literal in `fog_ref2.py`; 12 train games, shrunk with 8 pseudo-counts, non-increasing in age from 10 s for distance < 5000 u).
6. **tier** (game-clock seconds, so it behaves the same at 4 Hz live and 1 Hz in the harness):
   - **while `age < CUE_MIN_S` (2.0 s) the tier logic sees `p10` as 0**: no cue can start in the flicker window. The published `p10` is not gated.
   - faint on after `p10 >= 0.20` for 1.0 s; off after `p10 < 0.12` for 2.0 s.
   - close on at once at `p10 >= 0.40` (so at 2.0 s unseen at the earliest); off after `p10 < 0.25` for 2.0 s.
   - the first cue second of a stretch starts a 15 s life; after it, tier is 0 for the rest of the stretch. `ttl = 1 - elapsed / 15`.
   - tier 0 for `src == "respawn"`, and while I am dead (the life clock keeps running).
   - at most 2 entries keep a tier, ordered by (tier, p10).
7. **slot** = the hero's index among the enemies in first-seen order, modulo 5; once CV has a top-bar slot for all five enemies, `e["slot"] % 5`.
   A hero's slot can therefore change once, when the top bar is read (normally before the horn). The overlay does not animate that.

Constants (all in `fog_ref2.py`): `LIT_DEPTH 300`, `HINT_CAP .45`, `GHOST .30` (applied by the overlay), `FIGHT_R 2000`,
`SOFT_ON .20`, `SOFT_OFF .12`, `HARD_ON .40`, `HARD_OFF .25`, `CONFIRM_S 1.0`, `RELEASE_S 2.0`, `LIFE_S 15`, `MAX_CUES 2`, `CUE_MIN_S 2.0`.
`SOFT_ON` is the knob to turn first if the player finds the faint cue too chatty (0.25 removes about half of the faint time, `tiers.txt` V3).

### 2.4 Enemy death state (world.py; apply `world_death.diff`, measured in 5.2)

Today's faults (19 games, `deaths.txt`, `deaths2.txt`): 602 false-death intervals (405 right after a kill-score rise: the corpse marker and
the kill-score guess both fire for one kill and the guess lands on another hero; 197 with no kill at all: illusion corpses and old markers);
"marker gone" flips a hero alive a median 15-29 s after its death while real respawns take 34-47 s in mid game.

Rules:

1. **Kill events.** At the top of the minimap handling, scan `body["events"]` for `generic_event` whose `data` contains
   `CHAT_MESSAGE_HERO_KILL`; keep `[time, playerid1, used]` in `W["_"]["ek"]` (last 40) when the victim is an enemy:
   `(playerid1 >= 5) == (my team is radiant)`. `time` is the game clock. Any such event (either team) sets `W["_"]["ek_on"]`.
   Until `ek_on`, today's code paths run unchanged (the selfcheck sims carry no kill events).
   **Isolation (not in `world_death.diff`, add it):** the scan lives in its own function called inside `try / except Exception: log.exception(...)`,
   and skips an event whose decoded `data` is not a dict. An event stays in GSI's array for about 29 s; an exception here would otherwise drop
   every payload for that long (`merge_gsi` drops a payload on any raise).
2. **A corpse marker kills only with a kill event next to it.** For a hero with a `visionrange 0` object and no live object: remember
   `mark_at` (first payload of this marker) and `mark_fresh` (the hero was visible just before). It claims the earliest unused kill event
   with `mark_at - 6 <= time <= mark_at + 2.5`; fresh markers claim first, a non-fresh one only once the event is 1.5 s old. On a claim:
   `dead=True, death_mark=True, died_at=time, dead_until=time + respawn_est(time), last_xy=marker spot, vanished_at=None`.
   No event within 2.5 s: `death_mark=True` and nothing else (an illusion's corpse or an old corpse back in view). A pending marker also
   runs `_vis(..., False)` so the hero does not stay "visible". `mark_at` / `mark_fresh` are dropped when the hero is seen or the marker is absent.
3. **Kill events nobody claimed after 5 s** (death out of sight): the enemy that left sight most recently with
   `time - 6 <= last_seen <= time + 1` is marked dead the same way. This replaces `_kills()` once `ek_on`.
4. **Respawn is the timer's job.** "Marker gone for 4 s" only clears `death_mark`. In `tick`, at `clock >= dead_until` for an enemy with
   `died_at`: `dead=False, last_seen=clock, last_xy=FOUNTAIN[team], vanished_at=None, resp_at=clock`.
   `respawn_est(t) = min(60, 8 + 2.0 * minutes)` in Turbo (lower envelope of "death -> first seen again": 16 / 24 / 34 / 47 s at 2.5 / 7.5 / 12.5 / 17.5 min),
   times 2 in normal mode (not verified).
5. **A sighting** (`_vis` on) pops `resp_at`, `died_at`, `hint`, `hint_at`. A summon hint pops `resp_at`.
6. **Every unseen dead -> alive flip of an enemy sets `resp_at = clock`** (not in `world_death.diff`, add it): also the two old paths that
   still run before `ek_on` ("marker gone for 4 s", and `dead_until` running out without `died_at`). The fog layer treats every entry
   without `resp_at` as a real sighting, so no respawn may slip through as one.

### 2.5 Hooks, compatibility, cost

- `rules.evaluate`: `W["hud"]` gets `"fog": []` at line 42. Inside the in-progress block, before `danger()`:
  `W["hud"]["fog"] = fog.build(W["heroes"], W["me"], c, _.get("sight") or (), _.setdefault("fog", {}))`. It therefore exists while I am dead.
  **The call is wrapped in `try / except Exception`** (log once per match, leave `fog: []`, and clear `_["fog"]`): `evaluate` runs inside
  `tick_loop`'s single try block, so a raise in new code would skip the voice, the alerts and the JSON push on every tick until the next restart.
- **Kept exactly as they are:** `last_xy`, `last_seen`, `mia_s`, `reach_u`, `vanished_at`, `visible`, `x`, `y`; `hud.mia`, `hud.threats`,
  `hud.fight`; R1-R16, voice, jev, `to_text`. `dead` keeps its meaning and becomes more accurate (see risk 4).
- **New hero fields**, present only while relevant (zero bytes otherwise): `resp_at`, `died_at`, `mark_at`, `mark_fresh`.
  `hint` / `hint_at` are now removed on a sighting (nobody reads them today).
- `world.reset()` recreates `W["_"]`, which clears `ek`, `ek_on`, `sight`, `fog`.
- Cost budget: `fog.build` <= 0.3 ms per evaluate (measured twice: 0.10 ms mean, 0.22 ms p99, 5 hidden enemies, 70 sources none of them a hit, niced during a live game);
  `_merge_gsi` + <= 0.3 ms per payload (one `num()` per own-team object, one string test per event).
- Deploy: modules load at server start, there is no auto-reload (`uvicorn.run` without it), so `.py` edits on disk are inert until the
  next restart. Restart only between games, and never by an agent. **The player restarts the coach between games, so the project
  directory must never hold a half-done change: all work happens in a scratch copy and is installed in one gated step (section 6).**

### 2.6 Harness (`predict_eval.py`)

- `extract`: new bool column `resp` (`e.get("resp_at") is not None`); hint flag becomes
  `e.get("hint_at") is not None and e.get("hint_at") != hinted.get(s)` (the pop would otherwise read as a new hint); `Obs.heroes[h]["resp"]`.
- `PREDICTORS["fog"]`: port of `<ws>/lead/fog_pred.py` (point = `last_xy`, region = today's circle, `p = p10`), using `resp` instead of the fountain test.
- `display`: port of `<ws>/lead/display_ref.py` plus the `extra()` block of `<ws>/skeptic/display_ref2.py`, without the import of
  `proto_glance/sim.py` (it needs `prep`'s `has`, `alive`, `d`, `age`, `y10`, arrivals, deaths). Port both unchanged so the output can be
  compared digit for digit. `display_ref.py`'s own "ghost on" line still counts a mark from 1 s unseen; the number section 5.1 uses is
  the "minimap mark drawn" line of `extra()`, which counts from 2 s as the overlay draws it.
- No `deaths` sub-command (cut): section 5.2 is checked by running `<ws>/lead/deaths2.py` and `<ws>/lead/miss.py` with `PREDICT_TRACKS`
  pointed at the new tracks. They are one-off validation of the world.py patch, not a tool the project needs to keep.
- Re-extract into a new tracks directory after the world.py change (4-6 s per game); keep the old one for the before / after tables.

---

## 3. Data contract: `W["hud"]["fog"]`

A list, one entry per enemy hero that is alive and not in sight (0-5 entries). Seen and dead enemies are absent. Rebuilt on every evaluate.

```json
[{"hero": "mirana", "slot": 3, "age": 6.2, "x": -900, "y": 600, "conf": 0.47, "src": "seen", "p10": 0.44, "tier": 2, "ttl": 0.75},
 {"hero": "slark", "slot": 2, "age": 48.0, "x": 5200, "y": 3100, "conf": 0.08, "src": "seen", "p10": 0.02, "tier": 0, "ttl": 0},
 {"hero": "death_prophet", "slot": 0, "age": 12.0, "x": null, "y": null, "conf": 0.0, "src": "respawn", "p10": 0.03, "tier": 0, "ttl": 0}]
```

| Field | Unit | Meaning | Overlay use |
|---|---|---|---|
| `hero` | short name (key of `W.heroes`) | | Sprite |
| `slot` | 0-4 | Fixed position among the enemies | Strip slot |
| `age` | s, 0.1 | Since the last position evidence (sighting, hint - 3 s, respawn) | Fade-in, chip text |
| `x`, `y` | world u, or `null` | Last-seen spot (or the summon's). `null` for `respawn` | Ghost position, cue direction |
| `conf` | 0-1, 0.01 | P(the hero shows up within 1000 u of `x,y`), measured at the moment it is seen again; never rises within a stretch; 0 only for `respawn` | Ghost iff `>= 0.30` or `tier >= 1`; opacity |
| `src` | `seen` \| `hint` \| `respawn` | Where the position came from | Dashed ring for `hint` |
| `p10` | 0-1, 0.01 | P(seen within 1200 u of me in 10 s); 0 while I am dead | Not drawn (logged in world.jsonl for refits) |
| `tier` | 0 \| 1 \| 2 | The 2 s gate, hysteresis, 15 s life and the two-cue cap already applied | Cue, ring colour, chip colour, keeps the ghost drawn |
| `ttl` | 0-1, 0.01 | Remaining share of the cue's life; 0 at tier 0 | Drain bar |

Size: 105-135 bytes per entry, 670 bytes worst case (measured 567 compact / 666 default `json.dumps` for five). `hud.mia` and the rest are unchanged.
Guarantees the overlay may rely on: at most 5 entries, at most 2 with `tier > 0`, `conf` non-increasing while `src` and the stretch last,
`tier == 0` when `x` is `null`, `tier == 0` while `age < 2`, `x` and `y` are numbers for `seen` and `hint`.
The overlay must tolerate an empty list at any moment (GSI silent for 5 s, or the backend's guard tripped): everything fog-related disappears.

---

## 4. Overlay (`static/overlay.html`) and dashboard

Start from `<ws>/proto_glance/mock.html` (136 lines, working ghosts / strip / cues over a recorded frame; screenshots in
`<ws>/proto_glance/shots/`). Differences from that mock are marked NEW / CUT below.

**Safety rules for this file** (it is read from disk on every `GET /overlay`, and a game is live):
- Work on a scratch copy; install with one `mv` (atomic) only when the screenshot checklist passes (section 6).
- **Legacy fallback:** when `W.hud.fog` is not an array (the running server is the old one), run today's `marks()` / `arrows()` **verbatim**
  (keep the two functions as `marksOld` / `arrowsOld`, no F1 / F7 fix inside them). `coach.py` restarts a crashed overlay, which would
  load the new file in the middle of a game against the old server: that case must be pixel-identical to today. Delete the path in a later release.

**Render budget** (new; the overlay is a full-screen transparent window composited over the game, and the fog layer has something on
screen for most of a match: the strip alone is non-empty 74 % of the time):
- No looping or long-running animation in the fog layer. A change is one `.4s` opacity transition, then the layer is static again.
- Every value that drives the DOM is quantized first, and the DOM is written only when the quantized value changes: ghost opacity in
  steps of 0.1, chip text in the steps of 4.2, the drain bar in 15 steps. `age` and `conf` change on every 0.25 s message; the page must not.
- One keyed node per hero for ghost, chip and cue sprite; no `innerHTML` rebuild of the fog layer per message.
- Sprites: preload the five enemy icons once the enemies are known (`new Image().src`), and hide a ghost or chip whose image fails to
  load (`onerror`): an empty dark disc is worse than nothing.

Unit: `ip` = one real minimap hero icon in overlay px = `(cv.icon_px || 0.083 * cv.minimap_rect[2]) * innerWidth / cv.size[0]`
(31 px on the 2560 display, 21 px on the 1728 screen). No raw px sizes on the minimap.

### 4.1 Ghost (in `svg#mm`; needs `cv.affine_ok`, as today)

- Drawn for an entry with `x != null`, `age >= 2`, and (`conf >= 0.30` or `tier >= 1`). One keyed `<g>` per hero, updated in place (no `innerHTML` rebuild: fixes F4).
  The `tier` clause keeps a cued hero's mark on the map after its confidence has run out (27 % of cue seconds): an arrow never points at nothing.
- Dark disc `r = 0.45 ip` (`rgba(16,21,28,.75)`), hero sprite `0.8 ip` (`saturate(.3)`), ring `r = 0.45 ip`, 1.5 px `--ink-2`.
- Opacity = `fadeIn * max(min(1, .35 + .75 * conf), tier >= 1 ? .6 : 0)`, rounded to 0.1. `fadeIn` for `src == "seen"`: 0 below 2 s unseen,
  0.5 from 2 s, 1 from 3 s (two steps; the transition smooths them); 1 for `hint`. Computed from `age` on every render, written only when
  the rounded value changes; `transition: opacity .4s` on the node. (The first version started the fade at 1 s, which showed a faint ghost
  for the 1-2 s dropouts, the commonest kind: 2.8 per game minute.)
- `src == "hint"`: ring dashed `3 2`.
- `tier >= 1`: ring 2 px `--warn`, sprite `saturate(.85)`.
- `x,y` changes by more than 600 u for the same hero: remove the node and create a new one (fade out / in, never a slide).
- Two ghosts closer than `0.6 ip`: the higher slot is shifted `0.5 ip` right. Draw order: lower `conf` first.
- Clamp the y position to `innerHeight - 0.45 ip` (the minimap's bottom strip is outside the captured frame).
- No text, no "?", no circle. CUT from the mock: traces, the tier-2 reach ellipse.

### 4.2 Missing strip (needs only `cv.minimap_rect` and `cv.size`)

- Five fixed slots across the minimap's top edge: `left = rect.x * kx`, `width = rect.w * kx`, bottom edge 5 px above `rect.y * ky`.
- A chip in slot `slot` iff the entry has `age >= 8` or `tier >= 1`. Chip = sprite `1.75em` + time in steps: every second below 10 s,
  then `10s 15s 20s 25s 30s 45s`, then `1m`, `2m` (floor to the step). Ticking every second is 106 text changes per minute next to the
  minimap; the steps are 36 (`sk6.txt`). Font `max(10px, .42 ip)`, not upper-cased, panel recipe of the pill.
- Colour `--ink-2`; 70 % opacity from 30 s; `tier 1`: amber text; `tier 2`: amber text, 2 px amber underline, full-colour sprite.
- Same look for `src == "respawn"` (the time counts from the respawn).

### 4.3 Edge cue (in `arrows()`)

- New source: entries with `tier >= 1` (they always have `x,y`). Removed sources: `hud.mia`, and `hud.threats` of kind `missing`.
  `seen`, `smoke` and `fight` arrows are unchanged.
- One arrow per 45 degree sector as today, with: sector angle = vector mean `atan2(sum sin, sum cos)` (fixes F1, for all kinds);
  a fog hero is not added to a sector that already holds a seen (red) arrow (CUT: the dashed joined sprite);
  vertical position clamped to 13-80 % of the height (F7); label offset `6.2rem` instead of 100 px.
- Faint (`tier 1`): chevron 3.2 rem `--warn`, whole cue 60 % opacity, label = sprite(s) 1.9 rem, no text, no motion.
- Close (any `tier 2` in the sector): chevron 5 rem `--warn`, sprites 2.6 rem (max 3), a 4 px drain bar = the sector's largest `ttl`
  shown in 15 steps with no transition (a 15 s CSS animation would keep the window compositing), `nudge` twice at the onset only.
  No text, no number (CUT: `eta`).
- Fog never colours anything red, never makes a pill, a glow or a sound.

### 4.4 Removed when `hud.fog` is present

The `hud.mia` circles and "?", the `? ~Ns` / `? NEAR` arrows, the red ping and the arrow for `missing` threats. The `seen` / `smoke` pings and the fight X stay.

### 4.5 Mock scenarios (`?mock&sc=`)

Set the mock `cv` to the live geometry: `minimap_rect [2186,1159,374,325]`, `size [2560,1440]`, `A [[.021052,0,186.63],[0,-.02107,187.8]]`, `icon_px 31`.
Add, each with a `hud.fog` fixture that obeys section 3:

| Scene | Shows |
|---|---|
| `fog_calm` | `fog: []`: nothing |
| `fog_fresh` | Ghost 4 s (conf .49), ghost 7 s dark (.31), a 9 s dark entry (.27: no ghost, chip only), a 1.5 s entry (conf .9: nothing drawn yet) |
| `fog_lit` | A 20 s entry at conf .36 (vanished inside my vision): ghost still drawn, plus its chip |
| `fog_faint` | One `tier 1`: small cue, amber ghost ring, amber chip |
| `fog_cued_stale` | One `tier 1` at 12 s with conf .20: the ghost is still drawn (amber ring, 60 % opacity) because its cue is up |
| `fog_close` | Two `tier 2` in one sector: full cue with two sprites and the drain bar |
| `fog_drain` | The same with `ttl .15` and lower conf |
| `fog_stale` | 48 s, 95 s and a `respawn` entry: strip only |
| `fog_hint` | `src: "hint"`, conf .45: dashed ring |
| `fog_seen` | A red GANK arrow (`hud.threats`) plus a separate amber cue in another sector |
| `fog_dead` | `me.alive false`: ghosts and strip, no cue |
| `fog_flicker` (animated, fixtures at 4 Hz) | A hero 700 u from me vanishes for 0.5 s, for 1.9 s, and is seen for 0.5 s between two 1.5 s gaps: nothing may appear at any frame (no ghost, no chip, no cue) |
| `fog_empty` (animated) | A scene with two ghosts and a cue whose `hud.fog` turns `[]` for 3 s and comes back: everything fog-related leaves and returns without an error |
| `fog_life` (animated, 4x) | One hero from vanish to 40 s: ghost, fade, chip; conf from the dark curve |
| `fog_noaffine` | `cv.affine_ok false`: strip and cues only |
| `fog_legacy` | No `hud.fog` key, today's `warn` fixture: must look exactly like today |

`&small=1` rescales the mock `cv` to a 1728-wide frame for the MacBook check.

### 4.6 Dashboard (`static/index.html`, map at about 993-1016): minimal mirror (optional; last; skip it if anything in 5.5 is still open)

When `W.hud.fog` exists, per unseen enemy: ghost opacity `max(.25, conf)` (the second screen keeps stale ghosts, faint, with their age label);
amber ring when `tier >= 1`; dashed ring for `hint`; no ghost for `src == "respawn"` (nothing on the fountain); drop the reach disc.
Add a `hud.fog` fixture to the dashboard mock next to `hud.mia` (about line 1413). About 12 lines. Same atomic install.

---

## 5. Acceptance

### 5.1 Fog layer, TEST split (7 games, 199 min), `predict_eval.py display --split test`

What these numbers are: **port-fidelity gates**. The reference is `<ws>/skeptic/fog_ref2.py` scored by `<ws>/skeptic/display_ref2.py` on the
patched tracks (`<ws>/skeptic/display_amended_tracks2.txt`); "today" = today's overlay rules on the same tracks. A correct port lands on the
reference to the digit, and the "must be" column only leaves room for the re-extraction. They are not a forecast for the next game: the
thresholds were chosen with these 7 games in view (cue tiers in `tiers.py`, the lit split, the 2 s gate), so no untouched game is left,
and all 19 are Turbo games of one player. The honest test is the first games played with it; `hud.fog` (with `p10`, `conf`, `tier`) is
logged in `world.jsonl` for exactly that.
On today's unpatched tracks the reference gives the numbers in `<ws>/skeptic/display_amended_tracks.txt` (ghost hit 75 %, cue time 18.1 %): A's
port must reproduce those first, before touching world.py.

| Metric | Today | Reference | Must be |
|---|---|---|---|
| Hero shows up within 1000 u of its mark | 57 % | 74 % | >= 70 % |
| Mark drawn (unseen >= 2 s and (conf >= .30 or cued)), share of hidden hero-seconds | 44 % | 34 % | <= 37 % |
| Marks reaching full opacity per game minute | 7.7 | 7.5 | <= 8 (as many marks as today; the gain is that each one is a located icon) |
| `conf` Brier (no-skill 0.247) | - | 0.1615 | <= 0.166 |
| Cue up, share of alive time | 45.7 % | 21.1 % | <= 23 % |
| Somebody within 1200 u in 10 s while: close / faint / no cue | 28 % (any arrow) | 43 / 31 / 16 % | close >= 39 %, faint >= 26 % |
| The cued hero itself within 1200 u in 10 s: close / faint | - | 30 / 20 % | close >= 26 %, faint >= 17 % |
| Cues whose hero is within 1200 u of me within 15 s of the cue's start: close / faint | - | 54 / 35 % | close >= 49 %, faint >= 30 % |
| Cue onsets per 10 min (any-cue level) / cues per 10 min counted per hero | 20.4 / - | 16.0 / 23.5 | <= 18 / <= 26 |
| Cues on screen <= 2 s that end without the hero being seen | - | 8 % of cues | <= 10 % |
| Cue seconds with the hero unseen < 2 s | - | 0 | 0 |
| Arrivals with that hero cued in the 15 s before (cold) | 67 % (66 %) | 56 % (44 %) | >= 53 % (>= 40 %): accepted loss, see risk 1 in section 8 |
| My deaths after a fog arrival, cue up before it | - | 39 of 52 | >= 36 |
| `p10` Brier (always-base-rate 0.0916; today's rule 0.1368) | 0.1368 | 0.0833 | <= 0.085 |

42 % of cues are on screen for 2 s or less; in 34 of those 42 points the hero itself came back into view (the real icon or the red arrow
takes over), which reads as the cue being answered. The line above bounds the other kind, a cue that blinks and explains nothing.

`predict_eval.py eval --predictor fog --split test` (reference `eval_fog_both.txt`; `p10` is not touched by the amendments): at `p >= 0.3` recall >= 45 % (ref 48 %; baseline 37 %),
precision per second >= 27 % (ref 29 %; baseline 25 %); calibration bin 0.3-0.5 observed 25-35 %, bin 0.5-0.7 observed 45-65 %.
The WHERE columns of this predictor equal the baseline's by construction (the ghost never moves): they are a guard, not a target.

### 5.2 Death state, all 19 games, `<ws>/lead/deaths2.py` and `<ws>/lead/miss.py` on the new tracks

| Metric | Today | Reference | Must be |
|---|---|---|---|
| False-death intervals (no kill event) | 602 (5050 s) | 3 (8 s) | <= 10 |
| Living enemy flagged dead while unseen, hero-s per game | 505 | 7 | <= 20 |
| Dead enemy shown as alive and unseen, hero-s per game | 316 | 59 | <= 80 |
| Kill events with no death flag within 8 s | 6 of 661 | 25 of 661 | <= 5 % (accepted: illusion heroes and same-second deaths) |

"Truly dead" in this table is the kill event until the first sighting or `respawn_est`, so the respawn timing is scored against its own
formula; the kill attribution and the false deaths are not.

### 5.3 Guards (must not regress)

- `predict_eval.py eval --predictor baseline --split test` on the re-extracted tracks: fog marker error p50 inside 977-1195 u (ref 1107),
  hit@10 % >= 74 % (ref 75 %); respawn episodes with a marker >= 180 of about 198 (ref 191; today 203 of 404).
- `tools.py selfcheck` fully green, including the `hud.mia` hero-list check (tools.py:582) and the summon-hint check (829-843).
  Run it only between games (it was not run for this spec: it imports the voice and listen modules).
- Alert shift: replay 3 test games through `rules.evaluate` before and after the world.py change (`<ws>/tools/replay_world.py`) and
  report alert onsets per rule. A rise of more than 20 % in R1 or R2 goes back to the lead before deploy.
- `fog.build` mean <= 0.3 ms; `len(json.dumps(W["hud"]["fog"])) <= 700`.
- A forced raise inside `fog.build` (monkeypatched in the selfcheck) leaves `hud.fog == []` and every other alert of that tick intact;
  a `CHAT_MESSAGE_HERO_KILL` event whose `data` is not valid JSON, or decodes to a list, does not drop the payload.

### 5.4 New selfcheck checks (`tools.py`)

1. `fog.demo()` passes.
2. Sim timeline replay: every `hud.fog` entry has exactly the 10 keys; <= 5 entries; <= 2 with `tier > 0`; never an entry for a visible or dead enemy; `tier == 0` when `x is None`.
3. Enemy visible 3 s at 1000 u from me, then unseen: tier 0 while `age < 2`; conf >= 0.8 and tier 2 at `age` 2.0-2.5; conf < 0.30 at 9 s; conf never rises; tier 0 from 15 s after the cue started and for the rest of the stretch.
4. Enemy visible for 1 s only at 700 u from me, then unseen: `conf > 0` (a brief sighting is a sighting), tier 0 while `age < 2`. The same enemy dropping out for 1.5 s four times in a row: never a tier.
5. Hand-made payloads with a `CHAT_MESSAGE_HERO_KILL` event and a corpse marker: hero dead, no entry; at `dead_until`: entry with `src "respawn"`, `x None`, `conf 0`, `tier 0`, `last_xy` on the fountain.
   Also the old path (no kill event in the match, marker gone for 4 s): the entry is `src "respawn"` too (`resp_at` set).
6. Corpse marker with no kill event (after one earlier kill event set `ek_on`): hero not dead, its entry stays.
7. Kill event + marker for hero A while hero B left sight 2 s earlier: B is not dead.
8. The existing plague-ward payload: entry `src "hint"`, `0 < conf <= 0.45`; a later sighting removes `hint_at`.
9. `me.alive False`: entries present, all `tier 0`, `p10 0`.
10. Lit: last spot 500 u inside an own vision disc, 20 s unseen: `conf >= 0.30`; the same spot dark: `conf < 0.30`.

### 5.5 Screenshot checklist (B; headless Chrome, command in `understand_overlay.md` 3b / 3c, always `nice -n 19`, never `open`)

For each `fog_*` scene at 2560x1440, and `fog_fresh`, `fog_close`, `fog_stale`, `fog_seen` also with `&small=1` at 1728x1117:
- transparent mock shot, and a shot over the recorded frame (`preview_server.py` + `bg.jpg`);
- ghosts sit on the real minimap where the fixture says (compare with `proto_glance/shots/sheet_a.jpg`);
- the hero is recognisable at 17 px; chips do not cover the minimap or the kill feed; chip text >= 10 px;
- `fog_calm`: pixel-empty; `fog_flicker`: empty at every sampled time (sample every 250 ms); `fog_legacy`: identical to today's `warn` shot, pixel for pixel;
- `fog_cued_stale`: the cued hero has an amber-ringed mark on the minimap; `fog_empty`: no console error, marks leave and return;
- with the page left on `fog_stale` for 30 s, the DOM mutation count of the fog layer is under 2 per second (a `MutationObserver` in the mock);
- `fog_close`: one amber arrow with two sprites and a bar, nothing red; `fog_seen`: red and amber in different sectors;
- two threats due west (north and south of west) put the arrow on the west edge (F1);
- `fog_dead`: no arrow; `fog_noaffine`: no minimap marks, strip and cue present.

---

## 6. Work split (two implementers, disjoint files, the contract of section 3 is the only coupling)

**Where the work happens (both).** Not in the project directory: the player restarts the coach between games, and a restart loads whatever
is on disk (a `rules.py` that imports a missing `fog.py` would keep the coach from starting). Each implementer works in a scratch copy:

```
rsync -a --exclude recordings --exclude .venv --exclude .git --exclude data --exclude __pycache__ "<project>/" <ws>/build_a/   # build_b for B
ln -s "<project>/recordings" <ws>/build_a/recordings
cd <ws>/build_a && COACH_DATA_DIR=<ws>/data PREDICT_TRACKS=<ws>/tracks nice -n 19 "<project>/.venv/bin/python" predict_eval.py ...
```

(`<project>/.venv/bin/python`, not `uv run`: uv may re-sync the environment the live server runs on.) The project's own files are not
touched until the install step.

**A: backend.** Files: `fog.py` (new), `world.py`, `rules.py`, `predict_eval.py`, `tools.py`.

1. `fog.py` = port of `<ws>/skeptic/fog_ref2.py`; `python fog.py` runs `demo()`.
2. `world.py`: `W["_"]["sight"]`. `rules.py`: the guarded hook. Check `hud.fog` appears in a replay (`<ws>/tools/replay_world.py`).
3. `predict_eval.py`: predictor `fog`, sub-command `display`. On the existing `<ws>/tracks` it must print the numbers of
   `<ws>/skeptic/display_amended_tracks.txt` (same code, same tracks: equal to the digit).
4. `world.py`: apply `world_death.diff` (re-check each hunk against the current file), plus the two additions of 2.4 (the guarded
   kill-event scan, `resp_at` on every unseen respawn). `predict_eval.py`: `resp` column, hint-flag fix.
5. Re-extract all 19 games into a new tracks directory (`nice -n 19`). Check 5.1, 5.2 (scratch scripts), 5.3.
6. Selfcheck additions 5.4. The full selfcheck runs from `<ws>/build_a` and only between games.
7. Alert-shift report (5.3).
8. **Install**, only when 1-7 are green and no game is in progress (`GET /events` shows no `GAME_IN_PROGRESS` state; never POST): copy
   to temp names inside the project, then `mv` in this order: `fog.py`, `world.py`, `tools.py`, `predict_eval.py`, `rules.py` last
   (it is the file that imports `fog`). If the selfcheck could not be run because a game was live the whole time, do not install:
   hand over `<ws>/build_a` and say so.

**B: display.** Files: `static/overlay.html`, `static/index.html`. Needs nothing from A: the fixtures follow section 3.

1. Scratch copy; mock `cv` to the live geometry; the `fog_*` fixtures.
2. Ghosts (keyed nodes, `ip` sizing, quantized opacity), then the strip, then the cue tiers.
3. Legacy fallback when `hud.fog` is missing (the old functions verbatim); removals of 4.4 behind it.
4. Fixes F1 (vector mean), F4 (keyed nodes), F7 (clamp, rem offset), in the new path only.
5. Screenshot checklist 5.5; fix what it shows.
6. Dashboard mirror 4.6 and its mock fixture (optional).
7. Install each file with one `mv` once 5.5 passes. Safe at any time because of the legacy path: the running overlay keeps its
   loaded page, and a restarted one gets the new file talking to the old server.

Integration (after both): one replayed recording through the real server on another port between games, overlay over the recorded
frames; watch the frame rate of a real match with the new overlay once before calling it done (the composite cost of the new layer
cannot be measured from a mock).

---

## 7. Non-goals

- No predicted position for long-unseen or respawned heroes, no grid belief, no occupancy / wave / mates priors, no zone names (section 0).
  Reopen only with a region-style display in mind; `proto_behaviour/behave.py` is the starting point (hit@10 % 88 % on test).
- No ETA number, no reach circle, no "?" glyph, no percentages on screen.
- No pill, glow, red or voice from fog. R1 and its "missing" threat, R2, R3, R4, `hud.mia`, `hud.threats` are not changed.
- No player id -> hero mapping, no buyback / Roshan / tower-kill event parsing, no duplicate-icon (illusion) handling beyond the kill-event rule.
  (Cheap follow-up, not in this build: enemy `CHAT_MESSAGE_BUYBACK` events are in GSI, 2 seen in 3 games. Remembering the victim's player id
  on the hero at the kill claim would let a buyback end `dead` at once, without any learned mapping.)
- No enemy teleport, smoke or item inference (not in GSI).
- No local sprite route in `app.py`, no font bundling (the overlay keeps the Steam CDN and Google Fonts).
- No key presses, no process restarts by any agent, no change to `overlay.py`, `app.py`, `voice.py`.
- No tuning for normal mode (no data); the constants are Turbo-fitted.

---

## 8. Amendments (skeptic pass, 2026-11-16)

Method: every claim was checked against the code, the recorded GSI payloads and the harness; the reference was re-run (it reproduces
`display_tracks2.txt` to the digit) and then varied. Scripts and outputs: `<ws>/skeptic/`. The document above already contains the
changes; this is the list of what moved and why.

| # | Change | Why (measured on test, patched tracks, unless noted) |
|---|---|---|
| 1 | **No cue in the first 2 s unseen** (`CUE_MIN_S`, 2.3 step 6). | "Close on at once" fired the moment an enemy near me left sight: 79 % of close cues began on a hero unseen for under 2 s, 49 % of all cues lasted 2 s or less. Live it is worse than the harness shows: at 4 Hz, icons within 1200 u of me drop out for under 2 s 10-22 times per 10 min, and the 1 Hz tracks cannot see the sub-second ones at all (`sk1.txt`, `sk3.txt`). Cost: cold recall 46 -> 44 %, pre-cued deaths 41 -> 39 of 52. Gain: cue time 25.9 -> 21.1 %, cues per 10 min 32.7 -> 23.5. |
| 2 | **Any real sighting earns a ghost**; `vanished_at` is no longer read by the fog layer; world sets `resp_at` on every unseen respawn (2.3 step 4, 2.4 rule 6). | The "visible for 2 s first" gate dropped 36 % of the scoreable ghosts, and those are as right as the rest (74 % vs 73 %; by unseen time in `sk4.txt`). It also made the display inconsistent: the cue ignored the gate, so 38 % of cue seconds pointed at a hero with no mark on the map, and a one-tick sighting of a long-missing hero deleted its ghost exactly when the position was freshest. Cost: as many marks per minute as today's "?" (7.5 vs 7.7), each now a located hero icon. |
| 3 | **A cued hero's mark stays on the map** (`conf >= .30 or tier >= 1`, 4.1). | After amendment 2, 27 % of cue seconds still had no mark because confidence had run out. An arrow with nothing on the map to look at is half a message. |
| 4 | **Ghost starts at 2 s, not 1 s** (4.1), and the `fog_flicker` scene is now a real 4 Hz test. | The spec promised "a 2 s flicker never shows" but its formula drew a ghost at 45 % strength by 1.9 s. Dropouts of 1-2 s are the commonest kind (2.8 per game minute). |
| 5 | **The cue's promise restated** (sections 0, 1) and the acceptance table rebuilt (5.1). | "Close = 1 in 2" counted any hero showing up and the seconds right after a hero stepped out of view. For the cued hero, past the flicker window: 30 % within 10 s (faint 20 %), 54 % of close cues answered within 15 s. New lines in 5.1: the cued hero itself, cues per hero, cues that blink without an answer, cue seconds under 2 s. The table is now labelled as what it is, a port-fidelity gate; there is no untouched hold-out. |
| 6 | **Failure isolation** (2.4 rule 1, 2.5, 5.3). | `rules.evaluate` runs inside `tick_loop`'s one try block and `merge_gsi` drops a payload on any raise. A bug in the new code would silence the whole coach for the rest of a match (restarts are only between games), or drop every payload for the 29 s a kill event stays in GSI's array. Both new entry points are wrapped, and the event decoder checks its type. |
| 7 | **Scratch build, one gated install, fixed order** (2.5, section 6). | The first version edited `world.py` / `rules.py` in the project directory while a game was live. Python would not reload them, but the player restarts the coach between games: a half-done tree would go live, and `rules.py` without `fog.py` would not start at all. Also `<project>/.venv/bin/python`, not `uv run`. |
| 8 | **Render budget for the overlay** (section 4): quantized values, writes only on change, no long animation, stepped chip text and drain bar, verbatim legacy path, sprite preload. | `age` and `conf` change on every 0.25 s message, and the first version recomputed opacity (with a transition) and the chip text each time: 106 text changes per minute in the strip alone, on a full-screen transparent window over the game, for marks that are up 34 % of hidden hero time. Steps bring the strip to 36. The legacy path had been told both "unchanged" and "F1 fixed for all kinds". |
| 9 | **Vision sources are collected before the loop's `continue`** (2.2). | The harness's `obs.vision()` counts every own-team object with a vision range; the live loop skips `GHOST` images first. Placed after it, live "lit" would differ from the measured one. |
| 10 | **Cut:** the `predict_eval.py deaths` sub-command (scratch scripts check 5.2 once); the dashboard mirror is optional and last. | One-off validation does not need to live in the project; the dashboard is not the GUI over the game. |

Checked and left as they were: kill events (`time` is the game clock, victim ids match the top bar, 58 of 58); `fog.build` cost (0.10 ms
mean, 0.22 ms p99, 635 bytes); lit versus dark (holds on test: 61 % vs 19 % at 5-9 s); summon hints (2.5 ghosts per 10 min, right 84 % on
test, 49 % on train: inside the 0.45 cap); arrow direction (the hero comes from within 45 degrees 67-82 % of the time when it comes);
the screen capture reads the Dota window only, so vision.py cannot mistake an overlay ghost for a real icon; two tiers kept (a close cue
is answered 54 % of the time, a faint one 35 %).

**Risks not resolved**

1. **Long-unseen heroes get a timer and nothing else.** 43 % of arrivals on me come from a hero unseen for more than 15 s; the build shows
   a chip for those and no place. Cold-gank recall falls from 66 % (today's arrow, which is up 46 % of the time) to 44 %. This is a
   trade of coverage for credibility, and it answers "where might they be" only for about the first 10 s (25 s on lit ground). The next
   step, if the player wants more, is a region display from the grid prototype (hit within the best 10 % of the map: 88 %).
2. **No untouched test data, Turbo only, one player, 19 games.** On test a dark ghost at 5-9 s is right 19 % of the time against the
   0.30-0.36 the table claims (train 33 %); I did not retune on test. Expect to refit the two tables from the logged `hud.fog` after a
   few games.
3. **The harness is GSI-only; the live world also merges minimap CV.** In three live recordings a CV-only sighting ended about 8 % of
   the sampled unseen stretches, which matches CV leading GSI by a frame, but a false CV detection would move a ghost and reset a chip,
   and nothing here measures how often that happens (`sk7.txt`).
4. **The death rules change `dead` for every rule and the voice**, and the respawn timer is scored against its own formula. The
   alert-shift report (5.3) is the only guard; buyback is not handled (follow-up noted in section 7).
5. **Frame-rate cost of the new layer is unmeasured.** It cannot be measured during a live game or from a mock. Watch one real match.
6. **A cue is still right about its hero only 2-3 times in 10**, 23.5 times per 10 min. That is the ceiling of what GSI gives
   (precision tops out near 30 %), not a tuning problem. If it reads as noise, raise `SOFT_ON` to 0.25 or drop the faint arrow and keep
   the amber chip and ring.
7. **The selfcheck cannot run while a game is live** (it imports the voice and listen modules), so A's install may have to wait; the
   spec now says to hand over the build directory in that case.
