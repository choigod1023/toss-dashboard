# Toss Trading Analysis Dashboard

[한국어](README.md) · [日本語](README.ja.md) · **English**

A personal dashboard that reads your account through the Toss Securities Open API and analyzes your portfolio
alongside **news, community sentiment, institutional holdings (13F), and fear & greed indices**. Unlike an HTS that
only shows prices, it surfaces "what others are saying about this stock" and "the structural risk in my portfolio".

![Next.js](https://img.shields.io/badge/Next.js-000000?logo=nextdotjs&logoColor=white)
![React](https://img.shields.io/badge/React-20232A?logo=react&logoColor=61DAFB)
![TypeScript](https://img.shields.io/badge/TypeScript-3178C6?logo=typescript&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Neon Postgres](https://img.shields.io/badge/Neon%20Postgres-4169E1?logo=postgresql&logoColor=white)
![TimescaleDB](https://img.shields.io/badge/TimescaleDB-FDB515?logo=timescale&logoColor=black)
![Gemini](https://img.shields.io/badge/Gemini%20API-8E75B2?logo=googlegemini&logoColor=white)

> ⚠️ **This is a personal analysis tool, not investment advice.** It does not predict prices
> and does not tell you to trade. The judgment and the responsibility are yours.
> It is **BYOK (bring your own key)** — everyone runs it on their own machine with their own keys,
> so credentials never leave your environment.

## Screenshots

<p align="center">
  <img src="docs/screenshots/dashboard-demo.png" width="820" alt="Dashboard — strategy diagnosis, portfolio, price charts" />
  <br/><sub>Dashboard — strategy diagnosis · three expert perspectives · portfolio · price charts · flows by investor type.
  <b>The screen above uses demo data, not real account information.</b> The <code>🔒 hide balance</code> control in the header masks amounts.</sub>
</p>

<p align="center">
  <img src="docs/screenshots/onboard.png" width="560" alt="Onboarding — entering the Toss Open API key" />
  <br/><sub>Onboarding — enter your Toss Open API key and validation, storage, and collection follow automatically.</sub>
</p>

## What it shows

- **Portfolio diagnosis** — concentration (HHI), volatility, beta, max drawdown, and win rate, all computed in code
- **Three expert perspectives** — a risk manager, a PM, and a quant read the same data differently
- **Rebalancing suggestions** — target weights computed by rules; ETF and stock candidates limited to ones that actually exist
- **Sentiment analysis** — news, Reddit, YouTube, and NAVER News classified for sentiment by Gemini
- **Institutional holdings (SEC 13F)** — which managers hold the US names you own
- **Fear & greed** — CNN and crypto indices plus a domestically computed one
- **Conversational Q&A** — answers grounded in your real numbers (no predictions, no recommendations)

## Design principles

- **Numbers come from code, prose comes from the LLM.** Returns, weights, and volatility are all computed in SQL
  and handed over, so the LLM can't invent them. The LLM only interprets and explains.
- **No price prediction.** The moment you answer "will it go up?" with "yes", trust is gone.
- **Candidates must exist in the DB.** If the LLM invents a delisted name or a nonexistent ticker, the code filters it out.
- **Orders are blocked by default.** Without `ALLOW_ORDERS` set, the order path is closed.

## Layout

```
web/        Next.js dashboard + onboarding        → Vercel (or local)
worker/     Python collection/analysis scheduler  → local / Fly.io
            └ Toss prices & account · RSS · DART · SEC 13F · Gemini analysis
Neon Postgres + TimescaleDB (hypertable) + pgcrypto (credential encryption)
```

## Getting started (local)

### 1. What you need

- Python 3.11+, Node.js 20+, PostgreSQL (the Neon free tier works well)
- **A Toss Securities Open API key** — Toss Securities WTS → Settings → Open API
  - ⚠️ Toss uses an **IP allowlist**. Register the public IP of the machine running this tool
    in Toss's allowed IPs.
- **A Gemini API key** — https://aistudio.google.com/apikey (free tier available)
- (Optional) DART and NAVER Search API keys

### 2. Install

```bash
git clone https://github.com/choigod1023/toss-dashboard
cd toss-dashboard

# Generate the master key (once only — changing it makes stored credentials unreadable)
python3 worker/crypto.py --genkey >> .env
# Fill in GEMINI_API_KEY, DATABASE_URL, and the rest in .env
cp .env.example .env    # if it already exists, just edit it
chmod 600 .env

pip install -r requirements.txt
cd web && npm install && cd ..
```

### 3. Initialize the database

```bash
python3 worker/db/apply.py        # create the schema
```

### 4. Run

```bash
# dashboard
cd web && npm run dev              # localhost:3100

# worker (separate terminal)
python3 worker/main.py run         # long-running scheduler
```

### 5. Onboarding

Enter your Toss key at `localhost:3100/onboard` and validation, storage, and collection follow automatically.
Credentials are stored **encrypted in the DB with pgcrypto** and never appear on screen or in logs.
Reissuing the Client Secret in the Toss WTS cuts this tool's access immediately, at any time.

## Deployment (optional)

- **Dashboard → Vercel**: connect `web/` and register environment variables such as `DATABASE_URL`.
- **Worker → Fly.io**: Toss's IP allowlist means you need a fixed outbound IP.
  See [`FLY.md`](FLY.md) for the detailed procedure and pitfalls, and [`DEPLOY.md`](DEPLOY.md) for the overall setup.

## Constraints worth knowing (measured)

- The Toss Open API has **no WebSocket** — REST polling only. Ultra-low-latency trading is impossible.
- Tokens are **one per client** — running several tools at once invalidates each other.
- US indices (S&P 500 and the like), per-stock flows, and sectors aren't in the Toss API — some are filled in from outside sources.
- The Neon free tier is 512 MB — raw ticks aren't stored, only candles and aggregates.
- The predictive power of sentiment analysis on prices is academically contested — treat it as context, not a signal.

## License

MIT. For personal study and use. The author is not responsible for investment losses caused by this tool.

---

## 👤 Contribution & development environment

| Item | Detail |
|---|---|
| **Contribution share** | **100%** (solo development) |
| **Commits** | 15 / 15 (mine / all human commits) |
| **Contributors** | 1 |
| **AI coding tool** | Claude Code |

<sub>Counting basis (snapshot as of 2026-08-12): commits reachable from **every branch** on origin (merge commits and empty commits excluded), counted by commit author email with one person’s multiple addresses merged; bot and automation commits are excluded.</sub>
