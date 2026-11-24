# Cost-minimal LLM layer for the live Dota coach (researched 2026-11-09)

## 1. Bottom line

- **Cost is not the real limit. Latency and reliability are.** With compact text snapshots, a full 40-minute game costs about **$0.02 on the cheap tier** (gpt-6-luna or claude-haiku-5-5) and about **$0.16–0.32 on a mid-tier model** (gpt-6.1-terra or claude-sonnet-5-5).
- **Default model:** `gpt-6-luna` via OpenAI Chat Completions, with `reasoning_effort: "low"` and `response_format` json_schema strict. The user said "Jeff", which is probably GPT, and this is OpenAI's cheapest current model.
- **Fallback model:** `claude-haiku-5-5` through Anthropic's OpenAI-compatible endpoint. It costs the same per token and needs the same client code.
- **Optional escalation** for draft analysis and post-game review: `gpt-6.1-terra` or `claude-sonnet-5-5`.
- **Anything that must be fast stays rule-based, with no LLM.** That covers gank warnings, "5 enemies missing near Roshan" and the like. An LLM call takes seconds (UNVERIFIED, roughly 1–3 s for small models at low effort), which is too slow for a warning.

## 2. Verified pricing (USD per 1M tokens, from official pages fetched today)

| Model (ID) | Input | Cached read | Cache write | Output | Notes |
|---|---|---|---|---|---|
| **`gpt-6-luna`** | 0.10 | 0.01 | 0.125 | 0.50 | Context 1.05M. Chat Completions and Responses. Structured outputs, prompt caching. Reasoning effort none to max, default `medium`. **On Chat Completions, function calling works only with `reasoning_effort: none`**, so use `response_format`, not tools. Prompts over 272K are billed 2x. |
| `gpt-6.1-terra` | 1.00 | 0.05 | – | 5.00 | Mid-tier |
| `gpt-6.1-sol` | 2.00 | 0.10 | – | 10.00 | |
| `gpt-5-nano` (older) | 0.05 | 0.005 | – | 0.40 | Cheapest on the page, but older generation |
| `gpt-5.6-luna` | 0.20 | 0.02 | – | 1.20 | |
| **`claude-haiku-5-5`** (prompt ≤100K) | 0.10 | 0.01 | 0.125 (5 min) / 0.20 (1 h) | 0.50 | Prompts over 100K: 0.50 / 2.50. Adaptive thinking is on by default, effort default `medium`. Minimum cacheable prefix 512 tokens. |
| `claude-sonnet-5-5` | 2.00 | 0.10 | 2.50 / 4.00 | 10.00 | |
| `gemini-3.5-flash-lite` | 0.30 | 0.03 (+ $1.00/1M tokens/hour storage) | – | 2.50 | Free tier exists, but free-tier content may be used by Google |
| `gemini-3.8-flash` | 0.75 (rises to 1.50 on 2027-01-01) | 0.075 | – | 3.75 (rises to 7.50) | Free tier exists |
| `gemini-3.1-flash-lite` | 0.25 | 0.025 | – | 1.50 | |

On the Anthropic and OpenAI batch APIs, prices are 50% off. That only matters for post-game analysis.

Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing), [gpt-6-luna model page](https://developers.openai.com/api/docs/models/gpt-6-luna), [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Gemini pricing](https://aistudio.google.com/docs/pricing).

**Prompt caching:**
- **Anthropic native API:** explicit `cache_control`. The 5-minute TTL is enough if you call at least every 2–3 minutes.
- **OpenAI GPT-5.6 and later:** minimum 1,024 tokens. Writes cost 1.25x for the 30-minute TTL; reads cost 0.1x. The `prompt_cache_options` and TTL controls are documented for the Responses API. Whether caching applies automatically on Chat Completions is UNVERIFIED.
- **Caveat:** the Anthropic OpenAI-compat layer does **not** support prompt caching. At these prices that adds about $0.008 per game, so it doesn't matter.

## 3. One client for every provider (OpenAI Chat Completions format)

| Provider | `base_url` | JSON output | Caveats (verified) |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `response_format` json_schema strict | Tools need `reasoning_effort: none` on luna |
| Anthropic | `https://api.anthropic.com/v1/` | Only `json_schema` with `strict: true` is accepted; `json_object` and `text` return a 400 | See notes below |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | Structured output via `parse` with a Pydantic model or Zod schema | See notes below |
| OpenRouter | `https://openrouter.ai/api/v1` | `json_object` and `json_schema` (strict) | Support varies by model. Fee or markup not stated in the API docs (UNVERIFIED). |
| Ollama (local) | `http://localhost:11434/v1/` (api_key: any string, e.g. `"ollama"`) | `response_format` supported. Native `/api/chat` takes a JSON schema in `format`. | `tool_choice` not supported. Images must be base64. `num_ctx` needs a Modelfile. |

**Anthropic compat notes:**
- No prompt caching.
- `strict` on tools is ignored.
- `reasoning_effort` is ignored. `{"type":"adaptive"}` returns a 400. Claude 5 models think by default, so you can't lower effort through compat.
- Leave out `temperature` and `top_p`: on Claude 4.7 and later, `temperature` below 1 returns a 400.
- Anthropic labels the layer "not … production-ready" and says it is for testing.

**Gemini compat notes:**
- `reasoning_effort` maps to Gemini thinking levels.
- Thinking can't be turned off on Gemini 3 models.
- Caching is explicit only, via `extra_body.cached_content`.
- Google still calls the OpenAI libraries "beta".

Sources: [Anthropic OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk), [Gemini OpenAI compatibility](https://aistudio.google.com/docs/openai), [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility), [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs), [OpenRouter API overview](https://openrouter.ai/docs/api/reference/overview).

**What the client must do:** only send `reasoning_effort` when the host is OpenAI or Gemini, never send `temperature`, and always use `response_format` json_schema strict. With those three rules the same request body works on all five endpoints.

```python
# llm.py — one client, env-configured, with fallback
import os, json
from openai import OpenAI

def _client(prefix):
    url = os.environ.get(f"{prefix}_BASE_URL")
    return url and (OpenAI(base_url=url, api_key=os.environ.get(f"{prefix}_API_KEY", "none"),
                           timeout=float(os.environ.get("LLM_TIMEOUT_S", 10)), max_retries=1),
                    os.environ[f"{prefix}_MODEL"], url)

TARGETS = [t for t in (_client("LLM"), _client("LLM_FALLBACK")) if t]

def ask(system: str, state: str, schema: dict) -> dict | None:
    for client, model, url in TARGETS:
        extra = {"reasoning_effort": os.environ.get("LLM_REASONING_EFFORT", "low")} \
                if ("openai.com" in url or "googleapis" in url) else {}
        try:
            r = client.chat.completions.create(
                model=model, max_completion_tokens=1500,
                messages=[{"role": "system", "content": system},   # static -> cacheable prefix
                          {"role": "user", "content": state}],       # dynamic snapshot last
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "coach", "strict": True, "schema": schema}},
                **extra)
            return json.loads(r.choices[0].message.content)
        except Exception:
            continue   # ponytail: blind failover; add per-error handling if it flaps
    return None
```

## 4. Call design

**When to call. Event triggers, debounced, with a hard cap per game:**
- **Pre-game (one larger call):** from GSI `hero.name` plus the 10 heroes identified by CV/OCR, produce a lane plan, enemy weak points and an item path. About 6K tokens in, 1.5K out.
- **Periodic:** every 120–180 s of `map.clock_time` while alive. Produces a strategic summary and the next 2 items.
- **Events:**
  - your death (why, and what to buy on respawn)
  - reaching level 6 or another power spike
  - gold above the cost of the next core item
  - tower or Roshan changes in GSI `map`, if the client GSI exposes them (UNVERIFIED)
  - the CV pipeline flags a sustained anomaly, e.g. 3 or more enemies missing for over 20 s
- **Guards:** at least 20 s between calls and at most about 60 calls per game. Never call while a call is already in flight. Drop stale results.

**What to send:** a terse text snapshot, never images. Example, about 300–600 tokens:

```
t=18:42 day mode=AP me=npc_dota_hero_juggernaut lvl14 hp62% mp40% gold2840 gpm545 xpm610 k/d/a 7/3/5
items: phase,bfury,yasha | bp: tp | next_target: manta(1400 left)
pos: our_jungle_bot (x=3100,y=-4200)  last_seen_enemies(s_ago,zone): lion(4,mid_river) pa(31,?) es(31,?) lina(2,top_t2) ck(31,?)
alerts_rules: 3_enemies_missing_30s, rosh_pit_vision_none
towers: rad t1top/mid/bot down,t2bot down | dire t1mid down
recent_events: 17:55 died to pa+es at bot_river; 18:20 lion seen mid
```

**Static system prompt, about 2.5K tokens, placed first so it can be cached.** It holds:
- the coach role
- the output schema rules
- compact Dota heuristics
- per-game static data: my hero's skills and popular builds from OpenDota/Stratz, plus enemy hero counters

It stays byte-identical for the whole game. Don't put timestamps in it.

**Output schema, short so output tokens stay low:** `{"headline": str≤90, "do_now": [str≤60]≤3, "next_items": [str]≤3, "where": enum_zone, "risk": "low|med|high", "why": str≤200}`.

## 5. Budget per game (40 minutes)

Assumptions:
- 35 calls in total: 1 pre-game, 20 periodic, about 14 events.
- Each call sends 2,500 static tokens (cached) and 1,500 dynamic tokens.
- Each call produces about 600 output tokens, thinking included, at low effort.
- Totals: 87.5K cached input, 52.5K fresh input, 21K output.

| Option | Calculation | **$ per game** |
|---|---|---|
| **gpt-6-luna** (default) | 87.5K×0.01 + 52.5K×0.10 + 21K×0.50 (+ cache writes ≈ $0.0003) | **≈ $0.017** |
| **claude-haiku-5-5** via compat (no cache) | 140K×0.10 + 21K×0.50 | **≈ $0.025** (native SDK with caching: ≈ $0.017) |
| gemini-3.5-flash-lite | 140K×0.30 + 21K×2.50 | ≈ $0.095 (or $0 on the free tier) |
| gpt-6.1-terra | 87.5K×0.05 + 52.5K×1 + 21K×5 | ≈ $0.16 |
| claude-sonnet-5-5 (native, cached) | 87.5K×0.10 + 52.5K×2 + 21K×10 | ≈ $0.32 |

Even at 10x this call rate, the cheap tier stays under $0.25 per game. Medium effort on Haiku through compat roughly doubles output tokens, which is still under 4¢ per game.

## 6. Is vision on screenshots worth it?

The game renders 1728×1117 logical. Retina captures are 3456×2234 physical pixels, so **always downscale before sending**.

| Image | Model | Tokens | $ per image |
|---|---|---|---|
| Full frame at 1728×1117 | claude-haiku-5-5 (high-res tier, 28-px patches: 62×40) | 2,480 | $0.00025 |
| Full frame | claude-sonnet-5-5 | 2,480 | $0.0050 |
| Full frame | gpt-5.6-luna / 5.4-mini rule (32-px patches ×1.2) | 2,268 | ≈ $0.00023 at luna's $0.10 (luna's image rule is UNVERIFIED) |
| Full frame | gemini-3.5-flash-lite (default 1,120 tokens; `low` = 280) | 1,120 | $0.00034 (low: $0.00008) |
| Minimap crop at about 520×520 | claude-haiku-5-5 (19×19 patches) | 361 | $0.00004 |

Sources: [Claude vision](https://platform.claude.com/docs/en/build-with-claude/vision), [OpenAI images and vision](https://developers.openai.com/api/docs/guides/images-vision), [Gemini media resolution](https://aistudio.google.com/docs/media-resolution).

**Continuous vision, at 1 frame per second for 40 minutes (2,400 frames, about 150 output tokens each):**
- Haiku: about **$0.78 per game**.
- Sonnet: about **$15 per game**.
- At one frame every 5 seconds: Haiku about $0.15, Sonnet about $3.

**Verdict: not worth it for continuous perception, but the reason isn't cost.**
- Each call takes seconds, so at best you get about 0.3–1 Hz.
- LLMs are unreliable at reading exact minimap coordinates and telling apart 600-size hero icons.
- Rate limits and output jitter add up.

CV (template matching, colour masks, and OCR on GSI-free regions) runs at 5–10 Hz locally for free and is deterministic.

**Where vision is worth it:**
- One call at game start to name the 10 heroes from the top bar if CV template matching isn't confident. Costs under $0.001.
- An occasional "sanity check" frame while you're debugging the CV pipeline.
- Offline: auto-labelling recorded frames to build or validate CV templates, at batch prices.

## 7. Local option (Ollama), for experiments only, not the default

This machine has **36 GB** of unified memory, and Ollama is **not installed**.

| Model (verified on ollama.com) | Size | Context | Capabilities |
|---|---|---|---|
| `gemma4:12b` | ≈ 8 GB | 256K | tools, thinking, vision |
| `gemma4:26b` (MoE, 4B active) | 16–19 GB | | |
| `qwen3.6:27b` | 18–19 GB | 256K | tools, thinking, vision; also `-mlx` tags |
| `qwen3.6:35b` | 23 GB | | |

- Third-party guides rank Qwen 3.6 and Gemma 4 highest for schema-constrained JSON (UNVERIFIED: these are SEO-style articles, and you should benchmark on your own snapshots).
- **Main risk:** the model would share the M3 Pro GPU and memory with Dota 2 running on the same machine. A 19 GB model plus Dota plus a browser is tight, and inference will likely cost FPS during calls (UNVERIFIED, so measure it).
- **Saving:** at most about $0.02 per game, so local only makes sense for offline or privacy use.
- If you try it, start with `gemma4:12b` or the MoE `gemma4:26b`.

Sources: [ollama.com/library/qwen3.6](https://ollama.com/library/qwen3.6), [ollama.com/library/gemma4](https://ollama.com/library/gemma4); third-party guides from the search, not verified: [insiderllm.com](https://insiderllm.com/guides/best-local-llms-mac-2026/), [bestllmfor.com](https://bestllmfor.com/best/structured-data-extraction-json/), [promptquorum.com](https://www.promptquorum.com/local-llms/best-models-apple-silicon-2026), [toolhalla.ai](https://toolhalla.ai/blog/best-ollama-models-2026).

## 8. Recommendation and environment variables

- **Default:** `gpt-6-luna` with `reasoning_effort: low`.
- **Fallback:** `claude-haiku-5-5` on `https://api.anthropic.com/v1/`.
- **Zero-cost alternative:** a Gemini free-tier key with `gemini-3.5-flash-lite`. Free-tier content may be used by Google, and there are rate limits.

The app should read these variables:

```
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-6-luna
LLM_REASONING_EFFORT=low            # sent only to OpenAI/Gemini hosts
LLM_FALLBACK_BASE_URL=https://api.anthropic.com/v1/
LLM_FALLBACK_API_KEY=sk-ant-...
LLM_FALLBACK_MODEL=claude-haiku-5-5
LLM_TIMEOUT_S=10
LLM_MIN_INTERVAL_S=20
LLM_MAX_CALLS_PER_GAME=60
# optional: LLM_STRATEGIC_MODEL=gpt-6.1-terra  (pre-game draft + post-game only)
```

Leaving `LLM_BASE_URL` unset should turn the LLM off. The coach then runs on rules alone, and the dashboard keeps working without a key.

**Still to check before building:**
1. That `gpt-6-luna` caches automatically on Chat Completions.
2. Real latency for luna and Haiku at low effort. Time both on real snapshots.
3. Whether the client GSI `map` block exposes Roshan or tower state.

If caching or latency checks fail, use Anthropic's native SDK path for the fallback. That adds caching and lets you set `effort`, at the cost of a second code path.