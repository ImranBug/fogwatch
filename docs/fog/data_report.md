# Where / when are the unseen enemies: accuracy harness, baseline, mined facts

Data scientist report, 2026-11-16. Code: `<project>/predict_eval.py` (dev tool, stdlib + numpy,
never imported by the app). All outputs quoted here are saved under
`<scratch>/ws/tracks/`.

## 0. Read this first

- There are **19 finished recorded games, not 32** (the other session folders are menu / demo / aborted sessions without a match).
  All 19 are **Turbo**, one player, on Lich x7, Enigma x4, Warlock x4, Sniper x4. Nothing here is validated for normal mode.
  (meta.json labels three of them "normal"; today's world.py turbo detection, replayed, says Turbo for all 19.)
- Split by game: **train = 12 oldest, test = 7 most recent** (`N_TEST = 7`; I kept more train games than "~8 test" would leave).
- Today's baseline, test games, 1257 fog episodes: the marker (last_xy) is a median **1096 u** from where the hero reappears
  (p75 2584, p90 6170). The 330 u/s circle contains the reappearance 84 % of the time, but only because it balloons: after 20 s
  unseen it covers **~30 % of the walkable map** and still misses 22-40 %.
- Today's "missing enemy near you" rule catches **32 %** of the 225 arrivals on me (38 % of the 71 cold ones), and **3 of 4
  warning seconds are false** (precision 24 %). Its yes/no is barely informative: 20 % arrive when it fires, 9 % when it does not.
- Biggest measured levers (test games): a learned "where enemies show up, by game phase" prior (hit within the best 10 % of
  the map 75 % -> 87 %; after a respawn 21 % -> 70 %), then an honest distance-by-time kernel instead of the capped circle
  (75 % -> 80 %). Cutting visible ground does not move the reappearance score but removes the "obviously wrong" markers
  (9.0 % -> 0.7 % of marker-seconds). Heading at the vanish, hero role and summon hints are small.

## 1. Harness usage

```
cd "<project>"
export PREDICT_TRACKS=<...>/scratchpad/ws/tracks        # where the cached tracks live (default: <COACH_DATA_DIR or ./data>/tracks)
uv run predict_eval.py extract                # once: every finished game through the real world.merge_gsi/tick -> tracks/<session>.npz
uv run predict_eval.py occupancy              # walkable grid from TRAIN hero positions -> tracks/occupancy.npy
uv run predict_eval.py stats                  # sessions, split, episode counts, displacement table
uv run predict_eval.py eval --predictor baseline            # test + train tables
uv run predict_eval.py eval --predictor predict:Belief --split test --lead 5 --out run.json
uv run predict_eval.py demo                   # self-check (synthetic game, asserts)
```

- I ran it as `nice -n 19 .venv/bin/python predict_eval.py ...` so `uv` could not touch the venv the live server runs from.
  `uv run --no-sync` is the equivalent.
- Cost: `extract` took 6-58 s per game (about 3.5 min for all 19, single process, niced). It skips games already extracted
  (`--force` redoes them) and any GSI file written in the last 10 minutes (the live game). `eval --predictor baseline` on
  both splits: **1.4 s**. A grid predictor: 2.5 s (test); 15 s when it builds a vision mask every second.
- `extract` points `COACH_DATA_DIR` at a temp dir unless it is already set, so world.py never reads the live `settings.json`.
- Tracks: one `.npz` per game (0.3-0.7 MB). Per game second: clock, day, my x/y/alive/hp/level, score; per hero: world.py's
  `visible, x, y, dead, last_xy, mia_s, vanished_at, hint`; and **every object on the GSI minimap** (x, y, visionrange, unit name,
  image, team, yaw) so vision sources, enemy creeps, summons, buildings and facing can all be re-derived without the raw files.

### Predictor interface (also at the top of predict_eval.py)

`--predictor <built-in name> | <module>:<callable> | <path/file.py>:<callable>`. `callable()` is called once per game and returns
an object with an optional `reset(game)` and a `step(obs) -> {enemy: pred}` that is called **for every recorded second, in
order**. The predictor never holds the game: it only ever receives the current second, so it cannot read the future.

- `obs`: `i, clock, day, score, me {x, y, alive, hp, level}`, `heroes {name: {team, ally, visible, x, y, dead, last_xy, mia_s,
  vanished_at, hint}}` (exactly `world.W["heroes"]` at that second), `units` (int16 [n,7] minimap objects, `obs.names` /
  `obs.images` decode ids), `obs.vision()` (my team's rows with visionrange > 0).
- `pred`: `{"xy": (x, y), "r": radius}` or `{"xy": (x, y), "grid": float[64,64]}` plus `"p"` = P(seen within 1200 u of me in
  the next 10 s). Grid = density over `GRID` (64 x 64 cells of 275 u from -8800), `grid[iy, ix]`.
- A later `predict.py` needs one adapter: build its state from `obs` in `step`, return its belief as `grid`, its marker as `xy`,
  its arrival probability as `p`. Working example with a grid: `<ws>/proto_predict.py` (prototype yardsticks, not a design).
- Built-ins: `baseline` (today: point = last_xy, circle r = min(330 * mia_s, 6000), p = rules.danger's "missing" threat
  as yes/no, including its "I just walked out of base" exception), `reach` (same region, naive graded p for a warning curve).

### What is scored

- **Fog episode**: an enemy seen >= 2 s, unseen >= 3 s, seen again alive, not dead in between. **Respawn episode**: died in
  between (reported apart). The prediction from the last unseen second (`--lead 1`) is scored against the reappearance spot.
- **Point error** (u). **Region hit** and **region area** (% of walkable cells; a density's region = its best cells holding
  90 % of the weight). **hit@5/10/20 %**: is the truth inside the predictor's best 5/10/20 % of the walkable map (density rank;
  for a circle the concentric inner part, which is the most favourable reading of the baseline). **rank p50**: median share of
  the map the predictor rates above the truth.
- **Tolerance 400 u** (`--tol`): the last unseen snapshot is 0-1 s older than the sighting and a hero walks ~300-400 u/s, so a
  region within 400 u of the sighting counts. Without it the baseline's 3-5 s hit rate reads 58 % instead of 91 %.
- **Markers in plain sight**: share of point estimates (unseen >= 4 s, every 5th second) more than 600 u inside one of my team's
  vision circles. Needs no truth. This is the measurable form of "that mark is obviously wrong".
- **Arrival**: enemy unseen >= 4 s appears within 1500 u of me, me alive. **Cold** = no enemy in sight within 2000 u of me in
  the 5 s before (a gank, not one more hero joining a fight). Warning = p >= threshold; lead = first warning in the 15 s before;
  a warning second is false when that hero does not arrive within 15 s. Alarms = runs of warning seconds per hero.
- **Calibration** of p against "seen within 1200 u of me in the next 10 s" over all unseen, alive hero-seconds.
- 90 % intervals resample whole games (episodes of one game are not independent).

### Honesty limits of the harness

1. **Truth exists only where my team has vision.** Every number is conditional on the enemy being seen again. A predictor that
   puts weight where my team looks (lanes, the rim of my vision) is flattered; one that removes visible ground is punished
   (82 % of reappearance spots are inside a nominal vision circle one second before the hero is seen there). Enemies who stay
   in their jungle for a minute are never scored. Use "markers in plain sight" as the counterweight, and distrust a gain that
   shows at `--lead 1` but not at `--lead 5`.
2. `--lead 5` scores the forecast "where does it show up 5 s from now", with the same bias. It is not a test of "where is it now".
3. Replay is GSI only. The live coach also merges minimap CV; with GSI's minimap present GSI is the authority, so this is close.
4. `dead` is world.py's inference (death marker, kill score). An unseen death shows up as a teleport-like jump.
5. The walkable mask comes from train games; 0.64 % of test hero positions fall outside it.

## 2. Baseline (today's behaviour)

### Test: 7 games, 199 game minutes

```
FOG EPISODES (prediction vs reappearance spot; err in world units, area = share of walkable cells)
bucket         n  err p50    p75    p90   hit  area%  hit@5%  @10%  @20%  rank p50
all         1257     1096   2584   6170   84%   3.8%     69%   75%   81%      0.7%
3-5 s        366      639    896   1181   91%   0.6%     91%   91%   91%      0.1%
5-10 s       349     1055   1677   2497   87%   3.5%     84%   87%   87%      0.5%
10-20 s      257     2107   3455   6249   84%  16.0%     54%   71%   83%      3.8%
20-40 s      186     2744   6060   7943   78%  31.0%     46%   54%   67%      7.2%
40- s         99     5604   8556  12827   60%  30.0%     19%   27%   37%     25.5%
0-8 min      466      758   1341   2940   94%   2.4%     86%   90%   92%      0.2%
8-18 min     424     1606   3706   7500   78%   6.8%     58%   65%   72%      2.0%
18+ min      367     1360   3174   6170   80%   5.1%     62%   69%   75%      1.3%
90 % interval over games: median err 977-1195 u, hit@10% 74%-77%

RESPAWN EPISODES (death -> first sighting; the coach's last_xy is the fountain)
all          203     8587  11886  13441   27%  15.0%     16%   21%   27%    100.0%
(203 of 404 scored: in the other 201 the baseline had no marker one second before the sighting, almost always because
 world.py still had the hero dead)

MARKERS IN PLAIN SIGHT: 9.0% of 4348 sampled hero-seconds (unseen >= 4 s)

ARRIVAL WARNING: 225 arrivals on me, 71 of them cold
        recall  cold  lead>=3s  cold  lead p50  warn s/10min  precision(s)  alarms/10min  precision(alarm)
rule       32%   38%       24%   31%       7 s            61           24%          12.7               29%

CALIBRATION (21813 hero-seconds, base rate 9.9%, Brier 0.1321, always-base-rate Brier 0.0891)
rule silent   20604 hero-s   9.3% arrive        rule fires   1209 hero-s   20.0% arrive
```

At `--lead 5` (forecast, 791 episodes): err p50 2114 u, hit 61 %, hit@5/10/20 % = 42 / 49 / 55 %.

### Train: 12 games, 321 game minutes

```
bucket         n  err p50    p75    p90   hit  area%  hit@5%  @10%  @20%  rank p50
all         1896     1049   2430   5698   84%   3.7%     71%   77%   80%      0.6%
3-5 s        565      531    866   1252   90%   0.6%     90%   90%   90%      0.0%
5-10 s       559     1050   1836   2603   85%   3.5%     83%   85%   85%      0.6%
10-20 s      380     2072   3682   6911   81%  15.3%     54%   70%   79%      3.8%
20-40 s      253     2440   5684   9425   80%  30.4%     49%   59%   67%      5.1%
40- s        139     4199   7469   9938   68%  31.2%     35%   42%   52%     17.2%
0-8 min      684      662   1280   3092   93%   2.4%     87%   90%   91%      0.1%
8-18 min     708     1338   2771   5692   84%   5.3%     65%   73%   79%      1.3%
18+ min      504     1662   3377   7086   72%   4.8%     58%   65%   69%      2.6%
90 % interval over games: median err 931-1174 u, hit@10% 74%-80%
RESPAWN all  428     6275   9258  12141   37%  13.2%     24%   30%   36%    100.0%
MARKERS IN PLAIN SIGHT: 8.9% of 6794
ARRIVAL: 322 arrivals, 82 cold. rule: recall 38% (cold 37%), lead>=3 s 25%, 54 warn s/10 min, precision 26% (s), 11.8 alarms/10 min, 33% of alarms true
CALIBRATION: base rate 8.2%, Brier 0.1044 (always-base-rate 0.0751); rule silent 7.3% arrive, rule fires 25.9% arrive
```

Train and test agree within the game-level intervals, so the split is not hiding a drift.

How to read the baseline:

- **Under 10 s unseen it is fine** (hit 87-91 %, area 0.6-3.5 % of the map). Nothing to win on "where" there.
- **10-20 s**: the circle already covers 16 % of the map.
- **20 s+**: 30 % of the map and it still misses 22-40 %, because the 6000 u cap is below what heroes really cover (teleports).
  A circle this size is noise on a small screen. This is 24-27 % of all unseen hero-seconds after 8:00.
- **Respawn**: the fountain marker is wrong by a median 8600 u.
- **Arrival rule**: as a yes/no it does worse than always saying the base rate (Brier 0.132 vs 0.089).

## 3. Mined facts (train games only: 12 games, 321 min, 1897 fog + 712 respawn episodes)

Full output: `tracks/mined_train.txt`, `tracks/mined_arrival.txt`. Scripts: `<ws>/mine.py`, `<ws>/mine_arrival.py`.

### A. How far heroes really get

```
unseen       n    p25    p50    p75    p90    p95  <600u  u/s p50   p90   p95  outside 330*dt   330*dt
3-5s       565    342    657    982   1372   1640    47%      192   379   437        18.9%       990
5-10s      559    599   1231   1923   2715   3708    25%      189   409   529        19.1%      1980
10-20s     380   1219   2263   3892   7314  10291    12%      168   524   781        18.9%      4290
20-40s     254   1183   2822   5970   9242  13042     9%      112   340   431        25.2%      6000
40-80s     112   2123   4781   7533   9446  11249     4%       91   197   223        37.5%      6000
80-s        27   3308   5886   8567  12195  12876    11%       49   123   133        48.1%      6000
```

- Typical effective speed is **~190 u/s for the first 10 s, ~110 after 20 s**, not 330: heroes farm, wait, turn around. Half of
  the 3-5 s episodes moved under 660 u; 47 % under 600 u.
- The tail is fat: 19 % are already outside the 330 u/s circle (blink, haste, teleport), and the 6000 u cap is exceeded by
  25-48 % of long episodes. One radius cannot be both tight and safe; a density with a tight core and a thin tail can.
- **Episodes are short**: p50 7 s, 30 % end within 5 s, 59 % within 10 s, only 7 % last 40 s+. There are **5.9 fog episodes per
  game minute**. A mark for every vanish is one mark every 10 s, and 77 % of the 3-5 s ones moved under 1000 u (fog-edge
  flicker). This is the "question marks look random" complaint in numbers.
- Test games show the same table (`tracks/stats.txt`).

### B. Direction and zone

Episodes that moved >= 800 u; share within 45 degrees of a direction (chance 25 %):

| unseen | last 2 s velocity | facing (yaw) | toward me | toward its fountain | toward my fountain |
|---|---|---|---|---|---|
| 3-10 s (n 592; velocity column n 317, moving heroes only) | 51 % | 53 % | 30 % | 21 % | 40 % |
| 10-40 s (n 533; velocity n 237) | 36 % | 36 % | 39 % | 25 % | 33 % |
| 40 s+ (n 128; velocity n 65) | 26 % | 28 % | 34 % | 34 % | 17 % |

- Heading is real for under 10 s (2x chance) and gone by 40 s. But 36 % of heroes are standing still when they vanish, and
  using it barely moves the error: "keep velocity for 2 s" takes the median error from 657 to 640 u (3-5 s) and 1231 to 1134 u
  (5-10 s); extrapolating 4 s or 8 s makes it worse.
- "Toward me" (39 % at 10-40 s) is inflated by selection: I see the ones that come to me.
- Zone (from the enemy's side). Same zone at reappearance: 81 % (3-5 s), 68 % (5-10 s), 52 % (10-20 s), 46 % (20-40 s),
  36 % (40-80 s). Reappearance zone overall barely depends on unseen time: own safe lane 27-30 %, own off lane 23-27 %, mid
  17-22 %, river 12-15 %, own jungle 7-11 %, my jungle 2-4 %, base 2-6 %.
- After >= 20 s unseen: vanished in safe lane -> back in safe lane 56 % (n 104); off lane -> off lane 46 % (n 101); mid -> mid
  33 % (n 67); river -> river 28 % (n 53); own jungle -> a lane 63 % (n 41); base -> base 79 % (n 14, thin).

### C. Game time and role

| group | n | displacement p50 | p90 | u/s p50 | moved < 600 u |
|---|---|---|---|---|---|
| 0-8 min | 685 | 759 | 3497 | 127 | 38 % |
| 8-18 min | 708 | 1494 | 5896 | 175 | 19 % |
| 18+ min | 504 | 1866 | 7575 | 213 | 18 % |
| carry (30 hero-games) | 934 | 1360 | 6546 | 179 | 24 % |
| support (16) | 542 | 987 | 5040 | 139 | 30 % |
| other (14) | 421 | 1205 | 6069 | 172 | 23 % |

- Game phase matters about 2x; role about 1.3x (roles from OpenDota heroStats: Carry-not-Support / Support-not-Carry / rest).
- Unseen enemies at a typical second: 1.8 (0-8 min), 2.7 (8-18), 2.4 (18+). Alive enemies are unseen 41 % / 68 % / 64 % of
  the time. Of unseen seconds after 8:00, 40-43 % are under 10 s old, 32-33 % are 10-30 s, 24-27 % are 30 s+.

### D. Does a missing enemy end up on me

- 12-13 % of fog episodes end within 1500 u of me, almost independent of unseen time (10-15 % in every bucket).
- Distance decides it: when I was within 1500 u of the vanish spot, 34 % (3-5 s) / 27 % (5-10 s) came back on me; from
  1500-3000 u 19-23 %; beyond 5000 u 2-3 % for short episodes but 12-14 % after 20 s+ (rotations, teleports).
- Arrivals on me: **27 per game (10 per 10 min), 7 per game cold**. Unseen time before arriving: p25 6 / p50 11 / p75 32 / p90
  57 s (cold: 7 / 14 / 30 / 57). **43 % had been unseen > 15 s** (49 % of cold) and **39 % had last_xy more than 2500 u from
  me** one second before (46 % of cold): today's rule cannot fire for those by construction. 25 % came back from a death.
- **My deaths: 91 (7.6 per game). 77 % had an enemy arrive from fog within 1500 u in the 20 s before.** From that first
  appearance to my death: p25 5 / p50 9 / p75 15 s. So a warning even 3-5 s before the hero is visible adds real reaction time.
- Arrival odds, P(seen within 1200 u of me in the next 10 s) by distance last_xy -> me and unseen time:

```
d \ unseen     0-2s    2-4s    4-7s   7-10s  10-15s  15-20s  20-30s  30-45s    45s+
0-800           67%     52%     41%     32%      9%      6%     38%     10%      3%
800-1200        70%     53%     49%     41%     42%     26%     14%     17%     14%
1200-1600       47%     43%     35%     30%     17%     20%     23%     15%      5%
1600-2000       38%     37%     28%     15%     18%     21%     11%      9%     13%
2000-2500       26%     27%     20%     18%     17%      7%     21%      3%     15%
2500-3000       16%     18%     15%     12%     10%      9%     12%      5%     10%
3000-4000       15%     14%     13%      8%      7%      6%      9%      2%      7%
4000-5000        5%      4%      3%      6%      8%      6%      5%      2%      5%
5000-7000        3%      2%      4%      4%      5%      6%      9%      9%     10%
7000+            3%      3%      2%      3%      3%      4%      3%      5%      3%
```

  Even the best cell is a coin flip. Cells in the top-right corner have few samples (the 38 % at 0-800 u / 20-30 s is noise).
- Cheap extra signals barely move it (candidate set: unseen 4-30 s, last_xy 1200-4000 u from me, 4531 hero-seconds, base
  15.0 %): was moving toward me when last seen 15.6 % vs 14.8 % (lift 1.1); I am on the enemy half 17.0 % vs 13.8 %; night
  13.2 % vs 17.2 %; no ally near me 12.3 % vs 17.2 %; respawned (last_xy = fountain) 4.3 %. The one strong signal is context:
  **an enemy already in sight within 2000 u of me: 28.4 % vs 11.8 % (lift 2.4)**. Others join fights.

### E. My team's vision (nominal GSI visionrange circles; trees and high ground are not modelled)

- Share of the walkable map inside some circle: p10 21 %, **p50 30 %**, p90 45 % (day p50 41 %, night 25 %).
- **The coach's last_xy for an enemy unseen >= 4 s lies inside a vision circle 29 % of the time** (15 % inside 0.7 r, 8 %
  inside 0.5 r; 9 % more than 600 u deep = the "markers in plain sight" number). It is flat across unseen time (27-34 %).
- The circles overstate real sight. Per hero-second, an enemy standing inside a circle is hidden at least 13 % of the time
  within 300 u of the rim, 6-7 % at 300-1200 u deep, 3 % deeper than 1200 u (lower bounds: hidden seconds are interpolated
  inside short episodes only). 6.3 % of visible enemies are seen from outside every circle (spell vision, sources not in GSI).
- At reappearance moments the leak looks far bigger: one second before the hero is seen, the spot is already inside a circle
  82 % of the time, more than 600 u deep 29 %, more than 900 u deep 16 % (popping out of trees, uphill, invisibility, blink,
  teleport). Cutting the nominal circles out of the baseline region removes 28 % of its area but drops its hit rate at
  reappearance from 67 % to 13 % (exact scoring); even shrinking the circles to 0.6 r leaves 45 %.
- Conclusion: vision is a **soft** signal. Never zero a cell because a circle covers it. Use it to move the marker and to fade
  cells deeper than ~600 u.

### F. After a respawn

- Death -> first seen again: p25 12 / p50 34 / p75 66 s. world.py had already flipped the hero to alive in 60 % of cases.
- First sighting is a median **7875 u from its fountain** (p25 4209, p75 10795); only 17 % within 3000 u.
  **44 % are first seen within 1200 u of one of their own buildings, 72 % within 2500 u**: they teleport to a tower.
- By phase: first seen 9841 u from the fountain before 8:00 (straight back to lane), 7524 u at 8-18 min, 4096 u after 18:00
  (defending). First-seen zone: own off lane 24 %, safe lane 21 %, mid 17 %, base 15 %, own jungle 11 %, river 8 %.
- 12 % of respawned heroes are first seen within 1500 u of me.

### G. Teleport-like jumps

- 79 of 1897 fog episodes (4.2 %, **6.6 per game**) cover >= 2500 u faster than 550 u/s. They sit in the 10-20 s bucket (10 %
  of it); 2.3 % at 3-5 s, 4.3 % at 5-10 s, 1.6 % at 20-40 s.
- Landing: 38 % within 1200 u of one of their own buildings, 10 % within 1500 u of me, 1 % at their fountain.
- GSI gives no warning: `minimap_ping_teleporting` appears for my team only (1619 object-seconds, 0 for the enemy).

### H. Summon sightings (yesterday's "hint")

- world.py fired 106 hints in 12 games (about 9 per game). Summons actually seen: Wraith King skeletons (5480 object-seconds),
  Invoker forged spirits (1919), Warlock imps (1178) and golem (721), Weaver swarm (982), Furion treants (931), Enigma eidolons
  (776), Gyrocopter homing missile (384), Slark visual (290), Juggernaut healing ward (135).
- 94 hints could be scored against the next sighting: error before the hint p50 2121 u, after 1662 u; the hint was closer in
  61 %. When the hero was seen again within 10 s (n 51): 1429 -> 1021 u, closer in 69 %. At 10-30 s (n 24): closer in 42 %.
- A modest, real gain for under 10 s. Several of these units are not near their owner (homing missile, swarm on a victim,
  skeletons that keep walking, the golem). Thin per unit type.

### I. Other

- Distance from me to a reappearance: p25 1976, p50 4668, p75 8427 u; 17 % within 1500 u, 33 % within 2500 u. Two thirds of
  what the overlay marks happens off my screen.
- Walkable grid: `tracks/occupancy.npy` + `tracks/occupancy.md`. 74 % of the 64 x 64 grid; it trims map edges, not jungle walls.

## 4. Which information would reduce error most (ranked, with measured effect)

Measured with throwaway prototypes in `<ws>/proto_predict.py`, learned on train, scored on **test** through the harness
(`tracks/eval_proto_*.txt`). They are yardsticks, not designs.

| # | information source | measured effect (test, fog episodes unless noted) | trust |
|---|---|---|---|
| 1 | **Where enemies show up, by game phase** (learned map of enemy sightings, mirrored per side; includes "teleports to a tower after respawn") multiplied into the belief | hit@5/10/20 %: 69/75/81 -> **81/87/93**. 20-40 s: 46/54/67 -> 61/71/82. 40 s+: 19/27/37 -> 44/56/69. Respawn: 16/21/27 -> **58/70/84**. At lead 5: 42/49/55 -> 67/78/87. Region area 3.8 % -> 2.7 % of the map at the same 84 % hit | Large and stable, but partly the selection bias: the prior is "where my team sees enemies" and the score is "where my team sees them again". It also raises markers in plain sight (9.0 -> 12.9 %) unless combined with #3 |
| 2 | **Honest distance-by-time kernel** (table A) instead of 330 u/s capped at 6000 | 69/75/81 -> 72/80/87; region hit 84 -> 90 %. Same ranking as the circle close in, so the gain is all in the 10 s+ tail the cap cuts off | Solid. Also says: stop drawing a circle after ~15-20 s; it is 16-30 % of the map |
| 3 | **My team's vision as soft negative evidence** | Reappearance score slightly down (72/80/87 -> 68/78/85, expected from the bias), point error 1096 -> 1208 u. **Markers in plain sight 9.0 % -> 0.7 %**; 29 % of unseen-enemy markers sit inside a vision circle today | The gain is in credibility, not in the score. Must be soft (section E) |
| 4 | **Death / respawn state** | Baseline is wrong by 8600 u after a respawn and shows nothing in half of them; a phase prior fixes most of it (row 1). 25 % of arrivals on me come back from a death | Solid (n 203 test, 428 train) |
| 5 | **Calibrated arrival odds from distance x unseen time** (table D) instead of the yes/no rule | Brier 0.132 -> **0.082** (always-base-rate 0.089). At p >= 0.3: recall 42 % (cold 35 %), 66 warning s/10 min, precision 29 % vs rule 32 % / 38 % / 61 / 24 %. At p >= 0.2: recall 54 % (cold 51 %) for 114 warning s/10 min at 26 % | Calibration gain is solid. Recall gain at equal precision is modest: **precision tops out near 30 %**, so 2 of 3 alarms stay false whatever the threshold. Cold arrivals: n 71, thin |
| 6 | **Fight context** (an enemy already in sight near me) | Lift 2.4 on arrival odds (28 % vs 12 %) | Solid, cheap. Not yet in any prototype |
| 7 | **Heading / facing at the vanish** | Median error 1096 -> 1092 u overall; 5-10 s: 1055 -> 1003 u. hit@ unchanged | Small. Worth it only as a 2 s nudge of the marker |
| 8 | **Summon sightings** | 2121 -> 1662 u median on the 94 hints (about 9 per game); useful under 10 s | Small, hero dependent, thin |
| 9 | **Hero role / speed** | 1.3x in displacement (support 987 u vs carry 1360 u) | Small. Not worth a model dimension at 60 hero-games |
| 10 | **Enemy teleports** | 6.6 per game, invisible in GSI | Cannot be predicted; cover with the tower part of #1 |

What the numbers say about the display, in order of confidence:

1. Under ~10 s unseen the last spot is good (median 640-1060 u, 87-91 % inside a small circle). Show the real hero icon at the
   last spot with a small ring; no question mark needed. Debounce: 30 % of vanishes are back within 5 s.
2. Past ~15-20 s a circle around last_xy is 16-30 % of the map. Replace it with the few most likely places (lane / tower zones
   from the phase prior), each with the hero icon, or nothing.
3. Never draw a marker where the player can see no hero: 9 % of marker time today is deep inside own vision.
4. After a death, do not park the icon on the fountain: show "respawned, likely at a tower / lane" from the phase prior.
5. "When": a binary incoming alarm cannot be made precise from GSI (ceiling ~30 %). A graded, calibrated "could be on you"
   level from distance and unseen time is honest; boost it when a fight is already on me. Earliest-arrival time should use
   ~400 u/s (p90 of real effective speed under 10 s), not 330.

## 5. Too thin to trust

- 19 games, one player, four heroes, all Turbo, one patch. No normal-mode game.
- 40 s+ episodes: 99 test / 139 train; 80 s+: 27. Cold arrivals: 71 test / 82 train. My deaths: 91 (train).
- Teleport jumps: 79. Summon hints: 94, dominated by three heroes. Zone transitions out of base (n 14) and my jungle (n 10).
- Role split: 30 / 16 / 14 hero-games.
- The arrival-odds table has under ~50 samples in many cells with distance < 1200 u and unseen > 15 s.
- The "hidden inside vision" shares are lower bounds from short episodes only.
- The prototypes ran with one untuned setting each (no search), so their gains are not overfit to test, and they are also not
  the best each idea can do.
