# OpenRouter and Jev

OpenRouter is one account and one key that reaches many models. jobhunter uses it for two
scorer providers: `openrouter:<slug>` (any chat model) and `jev:<slug>` (the Jev scoring
model). Both use the same key. The provider table is on [Choosing a scorer](scorers.md).

## Get an account, credits and a key

1. Create an account at openrouter.ai.
2. Add credits. OpenRouter is prepaid, like the Anthropic API, and separate from any other
   account you have.
3. Create an API key in its settings. You can give the key its own spending limit there. Set
   one: it is a second cap that does not depend on jobhunter being correct.
4. Put the key in `OPENROUTER_API_KEY`: in `~/.env` on the host, or as the podman secret
   `jobhunter_openrouter` in containers. If you keep the key under another variable name, set
   `[scoring.openrouter] api_key_env` to that name. [Settings and secrets](settings-and-secrets.md)
   has the details. Restart the console after changing it.
5. Set the scorer in `config.toml`:

```toml
[scoring]
screen_scorer = "jev:typesafe/jev-1.13"
```

OpenRouter slugs look like `author/model`. A value such as `openrouter:jev:<slug>` is refused
with a hint naming the right spelling.

## jev: or openrouter:

- `jev:<slug>` and `decisions:<slug>` call OpenRouter's decisions endpoint. Write the prefix
  `jev:`, never `openrouter:jev:<slug>`. There is no separate Jev key: it is
  `OPENROUTER_API_KEY`.
- `openrouter:<slug>` calls the chat completions endpoint.

## Your resume goes in every request

Every scoring request carries your resume and preferences. So the defaults ask OpenRouter to
route only to providers that do not keep prompts:

- `data_collection = "deny"` excludes providers that retain or train on what you send.
- `require_parameters = true` refuses providers that cannot honour the structured-output
  request, instead of silently dropping it.

Both are in `[scoring.openrouter] provider`. The decisions endpoint takes only
`data_collection`; `require_parameters` is a chat setting. Leave `deny` in place unless you
have a reason: a model that only some data-collecting providers serve will then be refused
rather than fed your resume.

## How cost is reported, and what the cap reserves

The two providers differ, because they cost differently.

**`jev:` and `decisions:`**

- Cost is the figure the response reports. When it reports none, jobhunter charges about
  $0.042 per million input tokens (the rate observed for Jev; output is free). It never uses
  the model catalog and never records zero.
- Before each request the spend cap is checked against a token estimate at that same rate. The
  setting `est_cost_per_request_usd` plays no part, so changing it does not change Jev's caps.

**`openrouter:`**

- Cost is the figure the response reports, else the public model catalog (cached under the
  cache folder, refreshed at most daily), else zero with a one-time warning. A model with no
  known price therefore shows as free on `/costs`: watch the OpenRouter dashboard too.
- The cap reserves, for each request, the mean cost per request measured for that model (today's
  spend, else all history). With no history it uses `[scoring.openrouter]
  est_cost_per_request_usd`. It is never below the model's catalog list price.

How the caps work is in [Costs and credits](costs-and-credits.md).

## Check it

```bash
jobhunter doctor -v
jobhunter llm bench --scorer jev:typesafe/jev-1.13 --n 5
```

`doctor` reports whether a key is present without printing it. `llm bench` times a scorer on a
few ingested jobs and writes no scores, but it does call OpenRouter, so it uses a little
credit.
