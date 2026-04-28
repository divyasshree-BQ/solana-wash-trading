# Solana Wash-Trading Detector

A pipeline that identifies wash-trading activity on Solana DEXes by querying [Bitquery](https://bitquery.io) on-chain data, ranking suspect tokens by a heuristic wash score, tracing the top wash-trader wallets, and then following the SOL funding chain one or two hops upstream to identify the orchestrating wallets.

## How it works

Two phases on two complementary Bitquery surfaces — trades are not transfers, transfers are not trades, both are required.

### Phase 1 — Wash-trade detection (Bitquery Crypto Price MCP)

Runs against the Bitquery MCP at [http://mcp.bitquery.io/](http://mcp.bitquery.io/), which exposes a ClickHouse-backed DEX-trade index for Ethereum, Arbitrum, Base, Matic, Optimism, Binance Smart Chain, Tron, and Solana with 1-second resolution for the last ~30 days.

1. **Candidate ranking** — `trending_tokens` (or a custom `execute_sql` against `trades_*` / `tokens_*`) over the detection window, scored by trades-per-trader, average trade size, and the fraction of traders that appeared on both sides of the market.
2. **Top trader extraction** — `top_traders_by_token` for each top-N candidate, filtered to wallets with a round-trip ratio ≥ 0.4 (buy USD ≈ sell USD).

### Phase 2 — Origin tracing (Bitquery Solana transfers API)

Runs against the [Bitquery Solana transfers API](https://docs.bitquery.io/v1/docs/Examples/Solana/transfers) at **`https://graphql.bitquery.io`** with `solana(network: solana) { transfers(...) }`. The MCP indexes trades, not transfers, so this phase uses the transfers API directly.

3. **Funding trace (hop 1)** — first inbound SOL transfer for every wash wallet within a configurable lookback window.
4. **Hop-2 trace** — when the direct senders look like ephemeral one-time intermediates, repeats the funding query on those senders to surface the true root funder.

### Wash score formula

```
score = log10(trades_per_trader)
      × (1.0 if avg_trade_usd < $5 else 0.4)
      × max(both_sides_overlap, 0.2)
```

`both_sides_overlap = (buyers + sellers − traders) / traders`

A value above ~70 % for "both-sides participation" is a strong red flag; genuine retail trading typically sits below 30 %.

## Repository layout

```
solana-wash-trading/
├── scripts/
│   └── run_pipeline.py     # end-to-end pipeline
├── queries/
│   ├── 01_wash_candidates.graphql
│   ├── 02_top_wash_traders.graphql
│   ├── 03_funding_origins.graphql
│   ├── 04_first_funding.graphql
│   └── 05_verify_funder_outbound.graphql
├── REPORT.md               # findings from the 2026-04-27/28 12-hour run
├── SKILL.md                # reproducible workflow for the Bitquery MCP
├── .env.example            # template for environment variables
└── .gitignore
```

Output directories (git-ignored, created on first run):

| Path | Contents |
|---|---|
| `data/candidates_ranked.json` | Top 60 tokens with wash scores |
| `data/top_wash_traders.csv` | Top 30 traders × N tokens with round-trip ratios |
| `data/wash_wallets_by_token.json` | Round-trip-filtered wash wallets per token |
| `data/funders_summary.json` | Final aggregated funder per token |
| `funding/first_funding.json` | Raw first-inbound-SOL rows for all wash wallets |
| `funding/upstream_funding.json` | Raw first-inbound-SOL rows for ephemeral intermediates |

## Requirements

- Python 3.11+
- `curl` on `PATH` (used to call the Bitquery Solana transfers API)
- A [Bitquery](https://bitquery.io) account with:
  - The **Bitquery MCP** at `http://mcp.bitquery.io/` connected to your client (Cowork plugin/connector flow, or any MCP-aware tool). Used by phase 1.
  - A **bearer token** for the [Bitquery Solana transfers API](https://docs.bitquery.io/v1/docs/Examples/Solana/transfers) at `https://graphql.bitquery.io`. Used by phase 2. The same dashboard issues both — connecting the MCP and obtaining the API key are one-time setup.

## Setup

```bash
git clone https://github.com/your-org/solana-wash-trading
cd solana-wash-trading

# copy the env template and fill in your token (used for phase 2)
cp .env.example .env
# edit .env and set BQ_TOKEN=<your token>
```

To use phase 1 from an MCP-aware client, install the Bitquery MCP server once — the client (Cowork, Claude Desktop, etc.) handles the OAuth handshake. From an agent, the MCP wrapper tools `trending_tokens`, `top_traders_by_token`, and `execute_sql` then become callable directly.

## Running the pipeline

The reference implementation in `scripts/run_pipeline.py` runs all four steps against the Bitquery API directly (so it works with just the `BQ_TOKEN`). To use the MCP for phase 1, swap the candidate-ranking and top-trader steps for `mcp.trending_tokens` / `mcp.top_traders_by_token` calls; phase 2 stays on the Bitquery Solana transfers API regardless.

```bash
# use token from .env
export $(cat .env | xargs)

python scripts/run_pipeline.py

# or pass the token as a positional argument
python scripts/run_pipeline.py <YOUR_BQ_TOKEN>
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--since` | 12 h ago | ISO 8601 start of the detection window |
| `--funding-since` | 7 d ago | ISO 8601 start of the funding lookback |
| `--top-n-tokens` | `5` | How many candidate tokens to drill into |

**Example — scan the last 24 hours:**

```bash
python scripts/run_pipeline.py \
  --since 2026-04-27T00:00:00Z \
  --funding-since 2026-04-20T00:00:00Z \
  --top-n-tokens 10
```

## Sample findings (2026-04-27/28 run)

See [`REPORT.md`](REPORT.md) for the full write-up. Summary:

| Token | Primary funder | Wallets seeded | SOL deployed |
|---|---|---|---|
| OpenLie | `Etvs47...V39F` | ≥ 200 | ~100 SOL |
| JFDS | `EZn8SD...w2T` (via 27 ephemerals) | 27 | 82.1 SOL |
| SP500 | `6iTTuZ...iQn` (via 28 ephemerals) | 28 | 2.25 SOL |
| babyai16z | diffuse (bot-generated funders) | 30 | n/a |
| QOTUS | diffuse (two distinct fleets) | 29 | n/a |

## Saved queries

Every query the bootstrap pipeline issues is saved under `queries/` so it can be pasted directly into the [Bitquery IDE](https://ide.bitquery.io). When the MCP is wired up, queries 01 and 02 are replaced by `trending_tokens` / `top_traders_by_token` (or a custom `execute_sql` against the trades cube); queries 03 to 05 remain as-is on the [Bitquery Solana transfers API](https://docs.bitquery.io/v1/docs/Examples/Solana/transfers).

| File | Phase | Purpose |
|---|---|---|
| `01_wash_candidates.graphql` | 1 (or use MCP `trending_tokens` / `execute_sql`) | Rank tokens by wash score |
| `02_top_wash_traders.graphql` | 1 (or use MCP `top_traders_by_token`) | Top 30 wallets per token with buy/sell split |
| `03_funding_origins.graphql` | 2 | Funding origin aggregation |
| `04_first_funding.graphql` | 2 | First inbound SOL per wallet (transfers API, `limitBy`) |
| `05_verify_funder_outbound.graphql` | 2 | Verify funder's outbound burst |

## Caveats

- **SOL-only trace.** Funding via USDC, jitoSOL, or wrapped assets is not included.
- **Causation vs correlation.** The funding-then-wash-trade timeline is strongly suggestive but does not prove the funder controls the bots.
- **CEX vs operator.** Distinguishing exchange hot wallets from operator wallets requires additional profiling (transaction count, counterparty diversity).

## License

MIT
