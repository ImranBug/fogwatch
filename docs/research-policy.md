# Valve rules, anti-cheat and ToS limits for a second-monitor Dota 2 coach (as of 2026-11-09)

**Bottom line:** the planned design is a GSI listener, plus OS-level screen capture of the minimap, plus computer vision, plus a dashboard on the second monitor, with no inputs and no client access. That design stays on the safe side of every line Valve has drawn publicly. Valve's enforcement (2022 to 2024) went after three things:
1. Reading internal client data that is never shown to the player.
2. Scouting opponents' profiles and history before the draft ends.
3. Modifying the client (the Overplus case).

I found no Valve statement that addresses screen-reading tools either way. That part is UNVERIFIED and is my inference.

## 1. Primary Valve statements (verbatim, pulled from the Steam News API for app 570)

**2023-02-21, "Cheaters Will Never Be Welcome in Dota"** (https://www.dota2.com/newsentry/3677788723152833273)
> "Today, we permanently banned over 40,000 accounts that were using third-party software to cheat in Dota… This software was able to access information used internally by the Dota client that wasn't visible during normal gameplay, giving the cheater an unfair advantage."
> "This patch created a honeypot: a section of data inside the game client that would never be read during normal gameplay, but that *could* be read by these exploits."
> "**If you are running any application that reads data from the Dota client as you're playing games, your account can be permanently banned from playing Dota.** This includes professional players, who will be banned from all Valve competitive events."

How this applies to us: "Reads data from the Dota client" plus the honeypot means reading process memory. GSI is different: the client pushes it to a URI we configure. Screen capture reads the OS compositor, not the Dota process.

**2023-02-28, "Gameplay Update"** (Steam announcement gid 5059268543373304374)
> "Disabled some functionality used by third-party tools:
> - Disabled the "record" command in matchmaking games. This was used to record, in realtime, a local demo that would contain information that wasn't intended to be visible to the client during a game…
> - Disabled a large number of console commands during matchmaking games that could be used to introspect client state…
> - Player profiles can no longer be accessed in matchmaking during the pregame phase. They can still be accessed once the picking phase ends."

**2022-03-11, "Dota 2 Update - March 11th, 2022"**
> "Game State Integration now requires the command-line option "-gamestateintegration" to function. This is because there can be a per-frame performance impact to using game state integration."

This is the only GSI policy change in Valve's announcements from 2020 to 2026. Valve keeps GSI and tells you how to turn it on, which amounts to a sanctioned API. GSI has no Dota-specific page on the Valve developer wiki that I could reach; the CS:GO GSI page covers the same mechanism, but the wiki is behind a bot wall, so this point is UNVERIFIED.

**2024-02-08, "The Dragon's Gift"**
> "We've rolled out some new cheat detection code… we've spent the time since accumulating quite the set of cheaters… automated ban waves roll out."

Press coverage ties this wave to Overplus users, with around 10,000 bans by GosuGamers' unconfirmed count.

**2024-03-21, "Gameplay Patch 7.35d And Matchmaking Features"**
> "Over the last year or so, we've taken a number of visible actions against users of third-party cheats… Many cheats last year focused on gaining an advantage during the ten-second hero ban phase…"

Valve then removed the ban phase and replaced it with account ban preferences.

**2024-12-19, "Frostivus is Upon Us"**: 65,594 smurf accounts actioned, plus "high-MMR griefers… exploiting bugs, trading wins". Nothing about tools.

**Valve's own "coach" features show what Valve considers fair.** "Dota Labs" (2024-03-06) added an **Overlay Map**: "sort of just like the minimap, but it's large instead of small, and transparent… full-screen." Later Labs updates added "new data in the pre-match analytics". A bigger, reprojected view of your own minimap is something Valve itself ships.

## 2. Steam Subscriber Agreement (current text at https://store.steampowered.com/subscriber_agreement/english/)

- **§4.B Cheating:** "…functionality designed to identify software or hardware processes… that may give a player an unfair competitive advantage… ("Cheats"). You agree that you will not create Cheats or assist third parties in any way to create or use Cheats… you will not tamper with the execution of Steam or Content and Services unless otherwise authorized by Valve. You may not use Cheats, mods, hacks, or any other unauthorized third-party software, to modify… or otherwise in interacting with or controlling the processes or user interface of Steam Content and Services…"
- **§4.C Automation:** "You may not use any form of scripts, bots, macros, or other non-human-controlled systems ("Automation") to interact with Content and Services on Steam in any manner…" The examples include "Earning rewards or progress without genuine user input" and automated Overwatch participation.
- **§4.D Enforcement:** "We may enforce this provision using both automated detection methods and human review… Valve may restrict or terminate your Account(s)… for any conduct or activity that is illegal, constitutes a Cheat or Automation… Valve is not required to provide you notice…"

How this applies to us: we send no inputs, so §4.C does not apply. Note the phrase "unfair competitive advantage" in §4.B. It is vague, and Valve decides what it means. Information-only tools that use only what the player can already see (Dota Coach, DotaPlus and the Dotabuff app on Overwolf) have operated for years without user bans.

## 3. What was actually hit, and how

| Date | Target | Mechanism | Consequence |
|---|---|---|---|
| 2022-06-14 hotfix | Overwolf DotaPlus draft scouting | Dota stopped writing player IDs to `server_log.txt` at match accept (per a Russian-language report and the r/DotA2 "Today's Hotfix" thread) | Feature killed. The DotaPlus developer reportedly said: "the message from Valve is loud and clear — no more draft stage information". Secondary source, UNVERIFIED wording. **No user bans.** |
| 2023-02 | Memory-reading cheats | Honeypot in client memory | 40,000+ permanent bans |
| 2023-02-28 | Overwolf and other tools | `record` disabled, introspection console commands disabled, pregame profile access blocked | Features killed; Overwolf's GEP docs now say roster `steamId`/`name` stay empty until `DOTA_GAMERULES_STATE_STRATEGY_TIME` |
| 2024-02 | Overplus | Client mod: skin changer, camera zoom, terrain mods, plus draft counter-pick scouting of enemy history | Permanent bans, including pros. Valve later closed a lobby ban-sync loophole: "your bans for a matchmade lobby won't update once you're added to the lobby" (via hawk.live) |

The pattern: Valve **disables features** of information tools that use public or GSI data. It **bans users** of tools that read memory, mod the client, or exploit hidden state. Overwolf says "Overwolf and all official Overwolf apps will NOT get you banned" (support.overwolf.com, modified 2025-11-27); that is a self-claim. Dota Coach's FAQ says "The Dota Coach app will NOT get you banned" and that Overwolf is "built in collaboration with Valve" (UNVERIFIED by Valve).

## 4. Screen reading on a second monitor

- **No Valve statement either way** (UNVERIFIED; I searched and found none). Technically, macOS ScreenCaptureKit or `screencapture` reads composited pixels and never touches the Dota process. Nothing for the honeypot to catch, nothing to inject, nothing on the GPU hook path (unlike an Overwolf in-game overlay on Windows).
- **What we would derive:** minimap icon positions that the player can already see. Valve's own Overlay Map is the same information, bigger. Inferring "enemy probably at Roshan" from last-seen positions and timers is human-equivalent reasoning. My assessment: **low risk**.
- **VAC or Overwatch exposure:** VAC and Dota's detection target processes that read or tamper with the client. Overwatch is human review of replays for suspicious play. Advice-only tooling produces no tell-tale input pattern. VAC behaviour on macOS specifically is UNVERIFIED.
- **Grey area to avoid:** OCR of anything not rendered to you (impossible anyway), and pregame identification and scouting of opponents. Valve has specifically and repeatedly shut that down (2022, 2023, 2024).

## 5. Do / Don't list for our design

**DO**
- Use GSI: put `gamestate_integration_coach.cfg` in `.../dota 2 beta/game/dota/cfg/gamestate_integration/` (the folder must be created) and add the Steam launch option `-gamestateintegration` (required since 2022-03-11). Example:
  ```
  "dota2-coach"
  {
    "uri" "http://127.0.0.1:3000/"
    "timeout" "5.0" "buffer" "0.1" "throttle" "0.1" "heartbeat" "30.0"
    "data" { "provider" "1" "map" "1" "player" "1" "hero" "1" "abilities" "1" "items" "1" "draft" "1" "wearables" "0" }
  }
  ```
- Capture the screen at the OS level (ScreenCaptureKit, `mss`, `screencapture`) as a separate process, and only parse what is rendered.
- Show everything on the second monitor in a browser. No in-game overlay or injection. That is both simpler and safer than Overwolf's model.
- Use public APIs (OpenDota, STRATZ, Steam Web API) for hero, item and matchup stats. For player-history lookups, do them **only after the picking phase ends**, which matches Valve's own line: "They can still be accessed once the picking phase ends." Respect "Expose Public Match Data" opt-outs.
- Changing in-game settings is fine and helps parsing. Turning on `dota_minimap_show_hero_icon 1` and larger hero icons are ordinary options.
- Keep the app passive and advisory, with the human making every decision.

**DON'T**
- Read, scan or hook Dota process memory, inject DLLs or dylibs, or use `task_for_pid`/`vm_read` on macOS. That is the 2023 honeypot category: permanent ban.
- Modify VPKs or game files, or use skin changers or camera-distance hacks. That was Overplus.
- Use `record`, demo or console tricks, or parse `console.log`/`server_log.txt` (`-condebug`) for lobby or player IDs to scout pre-draft. Valve explicitly killed this path. Note that the open-source frontys/dota2-mcp does exactly this ("engine console logs… during the draft phase"), so avoid copying it.
- Send keystrokes, mouse input, chat or ping automation (SSA §4.C). Dota Coach's "click a timer to post in chat" feature is something we skip.
- Recommend pre-match bans based on opponents' hero history. That is what Valve described cheats targeting in 7.35d.
- Use it in ticketed or tournament games. GSI is delayed there (Dota Coach FAQ), and event rules typically forbid outside assistance (UNVERIFIED for specific events).

## 6. Existing products: feature ideas and where their data comes from

| Product | Data source | Features worth copying |
|---|---|---|
| **Dota Plus** (Valve, subscription) | Internal | Plus Assistant item and ability builds by bracket, hero relics/levels, post-game analytics. Dota Labs adds the Overlay Map and pre-match analytics (exact feature names from memory, UNVERIFIED) |
| **Dota Coach** (Overwolf, dotacoach.gg) | GSI (`-gamestateintegration`) plus public profiles | Rune timers (bounty/water/power/wisdom), pull and stack reminders, Roshan/Aegis/Glyph timers, buyback and ultimate trackers, catapult/flagbearer wave alerts, neutral item tiers, your-hero and per-enemy tips with counter items, enemies' recent item builds, real-time KDA/CS/GPM against peers, voice coaching |
| **DotaPlus** (Overwolf) | Public match history plus GSI | Smurf, booster and account-buyer detection, player notes, ally and enemy quick history, post-match score. Draft scouting was killed in 2022–23 |
| **Dotabuff App** (Overwolf) | GSI plus Dotabuff data | Timing, items and abilities assistants, power-spike module, draft advisor |
| **Overwolf GEP for Dota** (dev.overwolf.com) | **GSI** | Useful as a map of what GSI exposes: game and match state, clock and daytime, `nightstalker_night`, ward purchase cooldown, kills/assists/deaths, CS, GPM/XPM, gold split, buyback cost and cooldown, health and mana, status effects (stunned, silenced, hexed), ability and item cooldowns and charges, roster/draft/bans, damage dealt. It does **not** expose enemy positions; that is why we need the minimap CV |
| **Open source:** antonpup/Dota2GSI (C#, 236 stars), xzion/dota2-gsi (Node, 127), MrBean355/dota2-gsi (JVM, active 2026), Daniel-EST/dota2gsipy (Python), BrightGir/dota-ai-coach (Go, GSI plus RAG plus `gpt-4o-mini` every 60 s, Windows-only), shizzhang0/dota2-game-helper (GSI timer voice alerts, 7.39), bontscho/dota2-minimap-hero-sprites (spritesheet of minimap hero icons, useful as CV templates) | GSI only | The pattern is consistent: GSI for your own state, deterministic timers, an LLM on a slow cadence. **None does minimap CV**, so our replica map is new ground |

**What only the screen can give us:** enemy and ally positions on the minimap, visible tower and building state, enemies missing from lanes, smoke and Roshan-pit inferences, and enemy items on the scoreboard or top bar while visible. GSI covers your own hero, clock and timers in full, and draft or teams where available.

Sources:
- [Valve: Cheaters Will Never Be Welcome in Dota (2023-02-21)](https://www.dota2.com/newsentry/3677788723152833273)
- [Steam News API, app 570](https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=570) (2022-03-11, 2023-02-28, 2024-02-08, 2024-03-06, 2024-03-21, 2024-12-19 posts)
- [Steam Subscriber Agreement](https://store.steampowered.com/subscriber_agreement/english/)
- [Overwolf GEP: Dota 2](https://dev.overwolf.com/ow-native/live-game-data-gep/supported-games/dota-2)
- [Overwolf: "won't get you banned"](https://support.overwolf.com/support/solutions/articles/9000182312-overwolf-won-t-get-you-banned)
- [Dota Coach FAQ](https://dotacoach.gg/en/app/faqs) and [Dota Coach features](https://dotacoach.gg/en/app/features)
- [DotaPlus](https://dotaplus.overwolf.com/)
- [Dotabuff App on Overwolf](https://storeqa.overwolf.com/app/Elo_Entertainment_Inc-DOTABUFF_App)
- [esports.gg: Overwolf kill](https://esports.gg/news/dota-2/dota-2-update-kills-third-party-applications-including-overwolf)
- [escorenews: AdmiralBulldog / DotaPlus draft shutdown](https://escorenews.com/en/dota-2/news/44368-admiralbulldog-shows-on-stream-that-overwolf-still-works-on-same-day-it-was-patched-but-dev-announced-he-is-going-to-shut-draft-help-down-anyway)
- [hawk.live: What is Overplus](https://hawk.live/posts/what-is-overplus-dota-2) and [Valve has declared Overplus useless](https://hawk.live/posts/valve-has-declared-overplus-useless)
- [Strafe: Overplus lifetime bans](https://www.strafe.com/news/read/valve-handed-lifetime-ban-to-overplus-users/)
- [GosuGamers on X: ban count](https://x.com/GosuGamers/status/1756009026486198386)
- [Dotesports: Dotabuff privacy settings](https://dotesports.com/dota-2/news/dotabuff-violates-dota-2s-player-privacy-settings-giving-players-an-unfair-edge)
- GitHub: [antonpup/Dota2GSI](https://github.com/antonpup/Dota2GSI), [xzion/dota2-gsi](https://github.com/xzion/dota2-gsi), [MrBean355/dota2-gsi](https://github.com/MrBean355/dota2-gsi), [Daniel-EST/dota2gsipy](https://github.com/Daniel-EST/dota2gsipy), [BrightGir/dota-ai-coach](https://github.com/BrightGir/dota-ai-coach), [frontys/dota2-mcp](https://github.com/frontys/dota2-mcp), [shizzhang0/dota2-game-helper](https://github.com/shizzhang0/dota2-game-helper), [bontscho/dota2-minimap-hero-sprites](https://github.com/bontscho/dota2-minimap-hero-sprites)