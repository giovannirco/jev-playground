# Jev playground

Twenty small sites, one process. Each site is a use case for [Jev](https://docs.typesafe.ai), TypeSafe's decision model, called through OpenRouter as `typesafe/jev-1.13`.

Jev does not write paragraphs. You give it a state and a set of typed questions. It answers every question in that call at the same time, with probabilities:

- **Choice** picks one option out of up to 255 and returns a probability for each.
- **Score** places the state on an ordered scale.
- **Noul** is a yes or no, returned as the probability of yes.

Arithmetic, dates, and ranking stay in ordinary code. Jev only judges.

Nothing on these pages calls Jev when the page loads. A call starts when you press a button, or when you pause while typing. Every page has an **x-ray** control that shows the request and the response.

## Run it locally

You need Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), and an [OpenRouter](https://openrouter.ai) API key that can call `typesafe/jev-1.13`.

```bash
cp .env.example .env
```

Put your key in `.env`:

```bash
OPENROUTER_API_KEY=sk-or-...
```

Then:

```bash
uv sync --frozen
uv run jev-playground
```

Open [http://127.0.0.1:8780](http://127.0.0.1:8780). The hub lists every site and shows a live feed of calls (`/api/feed`). Prometheus-style counters are at `/metrics`.

`.env` is gitignored. Do not commit it.

The server will still start if the key is missing. Pages open, and the first real call fails until the key is set. Export the file if you prefer not to rely on the `.env` reader:

```bash
set -a
. ./.env
set +a
uv run jev-playground
```

## Docker

```bash
docker build -t jev-playground .
docker run --rm -p 127.0.0.1:8780:8080 --env-file .env jev-playground
```

Inside the container the process listens on port 8080. The `-p` flag above publishes that as 8780 on your machine, so the URL matches the local run.

The v0.1.1 image builds with Python 3.14.10 and runs as a non-root user. `/healthz`
checks the local process without calling a model. The server has no general user
authentication; keep it on loopback or behind your own access controls.

## Development checks

```bash
uv run --frozen python -m unittest discover -s tests -v
```

The HTTP regression suite uses local fixtures and requires no OpenRouter key.
Passing it does not verify model answers or authorize live trading.

## The sites

| Path | Kind | What you are looking at |
|------|------|-------------------------|
| `/rapid-dynamo/` | lab | Latency from 1 to 250 questions in one call, and a parallel flood |
| `/taboo-sprinter/` | game | Describe a word without saying it. One Choice over 200 words on each pause |
| `/clever-genie/` | game | Think of an animal. Each answer is checked against 120 animals, then Bayes runs in code |
| `/fickle-dungeon/` | game | A text adventure with no parser. Jev classifies the move; dice stay in code |
| `/honest-bookie/` | game | Your odds against Jev's sealed odds, scored with Brier |
| `/nimble-contestant/` | game | A quiz race against Jev's real latency |
| `/petty-tribunal/` | silly | Twelve persona jurors deliberate in one call |
| `/groaning-referee/` | silly | A pun battle with a composite score |
| `/smug-catwhisperer/` | silly | What the cat wants, how hard it is judging you, and whether it is plotting |
| `/flimsy-alibi/` | silly | An excuse scored for believability, originality, and whether the audience buys it |
| `/swift-dispatcher/` | helper | A live ticket storm in English, Portuguese, and Spanish |
| `/vigilant-bouncer/` | helper | A guardrail: hazard checks plus severity, as you type |
| `/wary-gatekeeper/` | helper | An agent shell plan marked run, confirm, or block |
| `/sleepy-butler/` | helper | A sentence mapped onto rooms, devices, and actions |
| `/picky-newsreader/` | helper | The live Hacker News front page, about 120 questions in one call |
| `/keen-highlighter/` | helper | One Choice over every line of a document. The sample lease is fiction |
| `/hungry-matchmaker/` | helper | What is in the fridge, checked against 60 recipes and ranked in code |
| `/polite-proofreader/` | helper | Tone, passive-aggression, and vagueness while you type |
| `/tidy-committer/` | helper | A diff and a message: conventional type, honesty, breaking changes, secrets |
| `/sober-satoshi/` | helper | A Bitcoin-only desk. Public prices, paper trades unless you turn live mode on |

Sample names, addresses, phone numbers, and API keys inside the pages are fake fixtures for the demos.

## Sober Satoshi

`/sober-satoshi/` loads public candles from LN Markets with no key. Orders are paper unless you opt in.

Live orders need all of these in the environment, and `LNM_LIVE=true`:

- `LNM_KEY`, `LNM_SECRET`, `LNM_PASSPHRASE`
- `LNM_NETWORK` (`signet` or `mainnet`)
- `LNM_MAX_QTY_USD` and `LNM_MAX_LEVERAGE` (hard caps in code)
- `LNM_TRADE_TOKEN` (you type this into the page; it is not a cookie)

The page buttons stay locked until that token matches. Leave the block in `.env.example` commented out if you only want the chart and the paper desk.

## HTTP API

| Endpoint | Purpose |
|----------|---------|
| `POST /api/jev` | `{site, state, questions}` returns answers, timing, and cost. At most 300 questions and 768 KB per call |
| `GET /api/feed` | Server-sent events, one per Jev call |
| `GET /api/stats`, `/metrics` | Totals per site |
| `GET /api/hn` | Hacker News top stories, cached for 5 minutes |
| `GET /api/btc/market` | LN Markets ticker, candles, and worded features |
| `GET /api/btc/status` | Whether live trading is on |
| `GET /api/btc/account`, `POST /api/btc/trade`, `POST /api/btc/close` | Signed LN Markets calls. They require `X-Trade-Token` and `LNM_LIVE=true` |

A call is usually a few tenths of a second whether it contains one question or a hundred, and costs on the order of $0.00001 to $0.0005. Input tokens are what you pay for.
