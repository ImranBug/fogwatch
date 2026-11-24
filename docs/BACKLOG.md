# Fogwatch backlog (player feedback + coach reviews, 2026-11-12)

Done 2026-11-12: calm voice; R1 quiet when winning / in base / just after "join"; jump initiators seen from 2600; R4 ruled out by 3+ visible
far enemies; spoken defend only for hero damage / <50% / high ground; item lines only in base, never with a full inventory; debrief
"warned" when a retreat call came 5+ s before; no praise under 40% HP; minute budget ignores urgent/debrief; fight mode needs an enemy in
sight; respawn prompt asked 12 s early and dropped once alive; open mic merges pauses (2.5 s); gpt-4o-transcribe; talk replies know
when no match is running; end-of-game cost includes talk + review.

Done 2026-11-12 (game 9086253341): no "Level up" at levels 17/19/21-24 (no point there); death lines vary and a team-fight death
is "fair"; every death gets its cause; "<Hero> is close. Back off." when one hero is the threat; R14 only right after fresh kills, at
50%+ HP, nobody within 1000; wisdom rune call only on my side; no talk-key hint in open mic; draft screen shows the picked hero; no
urgent line at 0 HP; overlay arrows for fog-missing heroes that could reach me in ~12 s ("? ~6s"); OBS clips logged; OBS 1080p30,
no dashboard capture.

Done 2026-11-12 (game 9087074099): GSI last-seen markers / TP pings no longer turn enemies into allies or fake sightings; no
"gank" call inside a team fight (2+ allies, numbers about even, HP > 50); "Get out!" at 50% with an enemy in melee range and no
ally; no "Defend X" with an enemy on me or for a far tower under 30%; "Still in danger" timed from the first high alert; "They lost
four/five"; hints count in the 20 s gap; talent at levels 10/15/20/25; "good trade" needs my kill/assist and no ignored call; a won
team fight is "fair"; laning deaths name the lane opponent; lane line by role only when the role is set; open mic listens only
during a match (it answered 17 min of room talk after the game; transcripts redacted); vocabulary-only transcripts dropped;
dashboard shows "Finding a match: Turbo (0:40)" / "Match found" (on-device OCR of the Dota menu).

Done 2026-11-13 (game 9087464856, Enigma): R11 "too deep" spoken at < 50% HP or 3+ unseen even when demoted; no "It's lost" right after
an ally kill; no "Nice fight" with an enemy within 1200 or HP falling; "Get out!" at 50% when HP fell 25% in 3 s or an enemy is in
melee range alone; leaving while losing HP is not "obeyed"; a held call is never spoken late; no defend call for creeps alone; wisdom
call only with an ally or <= 1 unseen; skill hints not in fights, repeated every 90 s; TP call in base / calm moments; no timers or
"farm" in thinking prompts; death causes: late_call (I called it late), ganked (lane gank), long fight = fair, "Reset" never for a
fair death, silence counts as disabled; first_in reworded; buyback line once per 5 min; fog arrows after 1 s; no "behind you" right
after leaving base; got-out praise after 3 s; coach starts the overlay when it is not running; start.command closes old windows.

Done 2026-11-13 (game 9087505239, Sniper): talk answers about items use Dota's item data (it said Hydra's Breath + Pike range stack; they
don't); no "Defend" call under 60% HP or without a TP far away, none for a dying Ancient; "Get out!" only with an enemy within 700 and
HP not rising; warn on 3 enemies in sight within 1600; join call counts every enemy around the fight and waits while danger builds;
a 2-3 s anti-flicker after "obeyed"; "coach_sent" cause when my defend/join call led to the death; 10+ s warning = "warned" even if
stunned at the end.

Done 2026-11-13 (game 9087545827, Lich): dashboard draft shows both teams' portraits as they are picked (vision.draft_picks
reads the pick screen; enemies appear when Dota reveals them); a death that bought a kill/assist is a good trade even after a
call (debrief at 3 s so the trade lands); "burst" cause for full-HP bursts; "warned" only for a call 5-12 s before; lingering low
= stayed_low; no "heal" / lane advice / runes in respawn prompts, at most 3 respawn prompts, near-duplicates dropped; fight-result
prompt needs 2+ kills; "Take a tower" needs an ally near; "It's lost" / "Get out. Now." quiet while we trade even; lone support
warned about AM / LC within 800; "too deep" urgent with an enemy within 1000; one retreat line per 10 s; support lane line;
buyback line for supports only when affordable; inventory test skips frames of another selected unit.

Next (bigger features):
- [ ] Draft recommendations that use the picks the dashboard now sees (counters, lane partners).
- [ ] Power treads switching advice (player, 2026-11-13): GSI reports item_power_treads but not its stat; needs CV of the item icon.
- [ ] While finding a match: show hints from previous games on the overlay / dashboard (player, 2026-11-13).
- [ ] Draft: update live as each player picks (needs CV of the pick screen; GSI sends nothing).
- [ ] Items: research item choices by situation (enemy lineup, game state) instead of the static build.
- [ ] Respawn thinking prompt when the same hero killed me 2+ times (review 7:04).
- [ ] Channeling (Upheaval / Black Hole) is not "warned": GSI has no channel flag; maybe from ability cooldown start.
- [ ] "Buyback still showing after I died and came back" (3:21): unclear which panel; ask the player.
- [ ] Enemy picks in the draft (GSI doesn't send them; needs the top-bar CV = Screen Recording permission for the terminal).
- [ ] Draft: once enemy picks are visible, update recommended picks / counters live; if I haven't picked, rank best picks for my role.
- [ ] Goal variety: rotate goal types per game (deaths, no death before 10:00, LH@10 for cores, ...) with scoring + voice lines.
- [ ] Items: respect what I'm building / already own, Turbo item timings, shard-before-Skadi style orders, suggest what to sell when full.
- [ ] Fog map: predicted position + confidence for heroes that walked into fog; only show the ones that could reach my lane.
- [ ] Split push / backdoor warning while the team fights elsewhere (enemy hero on our buildings in another lane).
- [ ] Power treads switching advice.
- [ ] Praise trying a new hero (needs ~150 games of history first).
- [ ] Track warning frequency and whether the player avoided it, per rule (partly in postgame scorecard).
- [ ] Skill points: reports of "unspent" points the player had spent (Sniper 25:30) -> check skills.py point counting per hero.
- [ ] Push-to-talk keys: macOS Secure Input stuck on loginwindow blocks key taps; restart the Mac, then re-test Cmd+B.
