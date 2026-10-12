# Choosing a scorer

`scoring.screen_scorer` in `config.toml` is `<provider>:<model>`. Bare model names are not
accepted. `deep_scorer` must be `anthropic:<model>`. A malformed value does not stop fetch or
mail: the run skips scoring and logs why, `score` exits with a one-line error, and the
console's re-score panel shows it.

<!-- jh:begin scorer-providers -->

| Spec | Endpoint | Key |
|---|---|---|
| `anthropic:<model>` | Anthropic batch API | `ANTHROPIC_API_KEY` |
| `openrouter:<model>` | OpenRouter chat completions (`[scoring.openrouter]`) | `OPENROUTER_API_KEY` |
| `openai-compat:<model>` | any OpenAI-compatible endpoint (`[scoring.openai_compat]`) | `OPENAI_COMPAT_API_KEY` (the name is `api_key_env`) |
| `local:<model>` | a model on this machine (`[scoring.local]`) | optional |
| `jev:<model>` | OpenRouter decisions endpoint (`[scoring.openrouter]`) | `OPENROUTER_API_KEY` |
| `decisions:<model>` | OpenRouter decisions endpoint (`[scoring.openrouter]`) | `OPENROUTER_API_KEY` |

<!-- jh:end scorer-providers -->

```toml
[scoring]
screen_scorer = "jev:typesafe/jev-1.13"
```

## Jev is its own provider

Write `jev:<slug>`, never `openrouter:jev:<slug>`: OpenRouter's chat endpoint rejects it, and
jobhunter refuses the spelling with a message naming the right one. Jev still uses
`OPENROUTER_API_KEY`; there is no separate Jev key. `decisions:<slug>` is the same endpoint
under its general name. See [OpenRouter and Jev](openrouter.md).

## Get an Anthropic API key

For `anthropic:` specs, including the default screen and deep models:

1. Create an API key in the Anthropic Console. It starts with `sk-ant-`.
2. Buy API credits there. They are prepaid and separate from a claude.ai subscription: a
   subscription does not pay for API calls, and the API credits do not pay for the
   subscription.
3. Put the key where jobhunter reads secrets: `ANTHROPIC_API_KEY` in `~/.env` on the host, or
   the podman secret `jobhunter_anthropic` in containers. Do not put it in `config.toml`.
   [Settings and secrets](settings-and-secrets.md) has the details.

`ANTHROPIC_AUTH_TOKEN` also works as the bearer-token form. An SDK profile configured through
the Anthropic environment variables counts as a credential too. `jobhunter doctor` reports
whether a credential is present and never prints it.

Check which secrets are set without printing them:

```bash
jobhunter secrets status
```

## Local models

- `local:<model>` runs on your machine (llama.cpp or Ollama, set by `[scoring.local] runtime`).
  It costs nothing per call but is slow, and requests run one at a time. The Models page can
  check that the server answers and lists the models it loads. See
  [Choosing models](models.md#access-keys-subscription-and-local-server).

## Other choices

- `openai-compat:<model>` points at any server that speaks the OpenAI chat API
  (`[scoring.openai_compat] base_url`).
- Try a scorer on a few ingested jobs before switching, without writing any scores:

```bash
jobhunter llm bench --scorer local:<model> --n 10
```

  Replace `<model>` with a real model name. A paid scorer in `--scorer` spends credits, so keep
  `--n` small.

Config changes need a console restart. After you change `screen_scorer`, jobs already scored
under the old one stay as they are; the console's re-score panel shows what moving them would
cost. Prices and caps are in [Costs and credits](costs-and-credits.md).
