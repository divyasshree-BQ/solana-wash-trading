---
name: solana-wash-trade-investigation
description: Detect wash-traded Solana tokens in a recent window and trace the funder wallets behind the wash-trade fleets. Two phases that use two different Bitquery datasets in sequence — Phase 1 (wash-trade detection) uses the Bitquery Crypto Price MCP at http://mcp.bitquery.io/ (trending_tokens, top_traders_by_token, execute_sql against ClickHouse trades_*/tokens_*); Phase 2 (origin tracing) uses the Bitquery Solana transfers API (https://docs.bitquery.io/v1/docs/Examples/Solana/transfers) at graphql.bitquery.io to walk the funding edges between wallets. Trigger on requests like "find wash trading on Solana", "who is funding wash trading", "trace bot fleets on Solana", "detect coordinated trading", "Bitquery MCP wash detection".
---

# Solana wash-trading investigation via Bitquery

This skill walks through a reproducible workflow to (1) identify wash-traded Solana tokens in a time window and (2) trace the on-chain wallet that funded the wash-trading fleet.

## When to use

The user asks any of:
- "Find wash-traded tokens on Solana in the last N hours."
- "Who is funding wash trading on Solana?"
- "Detect coordinated bot trading."
- "Trace the funder of these wash wallets."
- "Cluster traders that look like one operator."

## Connecting to Bitquery

The investigation has two phases. Each phase uses a different Bitquery surface because they operate on different datasets.

**Phase 1 — wash-trade detection.** Bitquery Crypto Price MCP at `http://mcp.bitquery.io/`. ClickHouse-backed DEX-trade index for Ethereum, Arbitrum, Base, Matic, Optimism, Binance Smart Chain, Tron, and Solana, down to 1-second resolution for the last ~30 days. Used to find wash-traded tokens and rank the wallets running the wash trades.

**Phase 2 — origin tracing.** [Bitquery Solana transfers API](https://docs.bitquery.io/v1/docs/Examples/Solana/transfers) at `https://graphql.bitquery.io` with the `solana(network: solana) { transfers(...) }` query. Indexed native-SOL and SPL transfer ledger. Used to walk the funding edges from each wash wallet back to the wallet that seeded it, then one more hop back to the orchestrator.

The MCP is not a fallback for the GraphQL endpoint — they cover different data. Trades are not transfers, transfers are not trades. You will use both in every investigation.

The MCP exposes both purpose-built wrapper tools and a raw SQL surface:

| Tool | Use for |
|---|---|
| `trending_tokens` | Hot tokens in a window (volume, % change, market cap). Quick first cut. |
| `top_traders_by_token` | Per-trader trade count and buy/sell USD volume for one token. **The core wash-trader ranker.** |
| `top_traders_by_network` | Whale ranking across an entire chain. |
| `find_token_by_address` | Resolve a contract address to canonical Token_Id and last USD price. |
| `find_tokens` / `find_currencies` | Fuzzy search by symbol/name. |
| `token_ohlcv` / `pair_ohlcv` / `currency_ohlcv` | OHLCV candles for charting. |
| `trader_activity` / `trader_profile` / `trader_positions` | Per-wallet timeline, P&L, open positions. |
| `execute_sql` | Read-only ClickHouse — write any custom query against `trades_*`, `pairs_*`, `tokens_*`, `currencies_*`. **The custom wash-score ranker.** |

### Why both surfaces

The MCP indexes DEX trades, OHLCV, and trader rankings. It does **not** index the native-SOL / SPL transfer ledger. Tracing funding edges requires the transfer ledger, which lives behind the Bitquery Solana transfers API. Two datasets, two surfaces, both required.

### Authentication

1. **MCP** — install/connect `http://mcp.bitquery.io/` once (Cowork's plugin/connector flow handles the OAuth). Every subsequent call is automatic. If a tool returns `requires authentication`, ask the user to reconnect the server.
2. **GraphQL** — ask the user once for their Bitquery API key (the same dashboard issues both), store it in `/tmp/.bq_env` as `BQ_TOKEN=ory_at_…`, and source from there. Never echo the full key into assistant-visible output.

```bash
# Phase 2 — transfer ledger. Same Bitquery account, GraphQL endpoint.
curl -s -X POST https://graphql.bitquery.io \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $BQ_TOKEN" \
  -d "$(jq -n --arg q "$Q" --argjson v "$VARS" '{query:$q,variables:$v}')"
```

## Wash-trade detection — the heuristic

A token is a wash-trade candidate when the trading ledger over the window shows:

1. **Very high trades/trader ratio** (`> 50` trades per wallet) — bot churn.
2. **Very low average trade size in USD** (`< $5`) — synthetic volume, not real fills.
3. **High both-sides participation** — share of wallets that appear as both buyer and seller is `> 70%` (real markets sit `< 30%`).
4. **Round-trip ratio per wallet** — `min(buy_usd, sell_usd) / max(buy_usd, sell_usd) ≥ 0.9` is the per-wallet wash signature.

A composite "wash score":

```
score = log10(max(trades_per_trader, 1))
      * (1.0 if avg_trade_usd < 5 else 0.4)
      * (both_sides_overlap if both_sides_overlap > 0.3 else 0.2)
```

## Funding-flow trace — the heuristic

For each wash wallet, pull the **first** inbound SOL transfer (the seed funding). Aggregate by sender. Two patterns emerge:

- **Direct funding** — one wallet shows up as the first sender for many wash wallets. That's the funder.
- **One-hop laundering** — every wash wallet has a unique one-time intermediate sender. Re-run the same query with the intermediates as receivers; the dominant sender at hop 2 is the orchestrator.

Disbursement bursts (many identical-amount sends within seconds) are a near-certain operator signature. Repeat the trace upstream until the funder source is either an exchange hot wallet (huge transaction counts, many counterparties) or an operator wallet (low counterparty diversity, large discrete deposits).

## The four steps

Two phases, two steps each.

**Phase 1 — Wash-trade detection (Bitquery MCP)** finds the wash-traded tokens (1.1) and the wallets running the wash trades on each (1.2).

**Phase 2 — Origin tracing (Bitquery Solana transfers API)** finds the seed funder for each wash wallet (2.1) and walks one hop upstream to the orchestrator when the seed funders are ephemeral intermediates (2.2).

### 1.1 Candidate tokens — Bitquery MCP

Two equivalent paths through the MCP, depending on how much shaping you need:

**Path A — wrapper tool (fastest).** Use `trending_tokens` to short-list candidates by activity, then keep only the rows whose price/volume profile matches the wash heuristic.

```
trending_tokens(blockchain="Solana", window_hours=12, sort="volume_usd",
                min_volume_usd=50000, limit=60)
```

This gets you symbol, mint, volume, % price change, and market cap. It does **not** give per-token trader counts or buy/sell breakdown — for that, run path B in parallel or fall through to step 2 for each candidate.

**Path B — custom ClickHouse SQL via `execute_sql` (full wash score).** Hit a `trades_*` table directly so you can compute trades-per-trader and the both-sides participation share in one shot. The MCP description lists the table groups (`trades_*`, `pairs_*`, `tokens_*`, `currencies_*`); confirm exact column names with `SHOW TABLES LIKE 'trades_%'` and `DESCRIBE trades_by_token_address` before customizing.

Sketch:

```sql
-- Replace column names after a quick DESCRIBE on your account's schema.
WITH base AS (
  SELECT
    Token_Address, Token_Symbol, Token_Name,
    Trader_Address,
    if(Side = 'buy',  Trade_AmountInUSD, 0) AS buy_usd,
    if(Side = 'sell', Trade_AmountInUSD, 0) AS sell_usd
  FROM trades_by_token_address
  WHERE Token_Network = 'Solana'
    AND Trade_Time >= now() - INTERVAL 12 HOUR
    AND Token_Address NOT IN (
      'So11111111111111111111111111111111111111112',  -- WSOL
      'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',  -- USDC
      'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'   -- USDT
    )
)
SELECT
  Token_Symbol, Token_Name, Token_Address,
  count() AS trades,
  uniqExact(Trader_Address) AS traders,
  round(count() / uniqExact(Trader_Address), 1) AS trades_per_trader,
  sum(buy_usd + sell_usd) AS volume_usd,
  round(sum(buy_usd + sell_usd) / count(), 4) AS avg_trade_usd,
  uniqExactIf(Trader_Address, buy_usd  > 0) AS buyers,
  uniqExactIf(Trader_Address, sell_usd > 0) AS sellers
FROM base
GROUP BY Token_Address, Token_Symbol, Token_Name
HAVING trades > 1000 AND avg_trade_usd < 5
ORDER BY trades_per_trader DESC
LIMIT 60
```

Compute the wash score client-side (or in another `SELECT`):
`log10(greatest(trades_per_trader, 1)) * if(avg_trade_usd < 5, 1, 0.4) * greatest(both_sides_overlap, 0.2)`.

### 1.2 Top wash traders per token — Bitquery MCP

Use `top_traders_by_token` against each shortlisted candidate. The wrapper already returns trade count, total / buy / sell USD volumes, and net positions per wallet — exactly what we need to compute the round-trip ratio.

```
top_traders_by_token(address="<mint>", blockchain="Solana",
                    window_hours=12, sort="trades", limit=30)
```

For each row, compute `min(buy_usd, sell_usd) / max(buy_usd, sell_usd)`. Keep wallets where the ratio is `≥ 0.4` — these are the wash traders.

If you need fields the wrapper doesn't expose (e.g. first/last trade time per wallet, per-pool split), drop into `execute_sql` against `trades_by_trader_address` and group by `Trader_Address`.

### 2.1 First inbound SOL per wash wallet — V1 GraphQL transfers

This is where origin tracing begins. The MCP doesn't index transfers, so this step queries the V1 GraphQL endpoint at `https://graphql.bitquery.io`:

```graphql
query FirstFunding($wallets: [String!]!, $since: ISO8601DateTime!) {
  solana(network: solana) {
    transfers(
      receiverAddress: {in: $wallets}
      currency: {is: "SOL"}
      amount: {gt: 0}
      time: {since: $since}
      options: {asc: "block.height", limit: 2000, limitBy: {each: "receiver.address", limit: 1}}
    ) {
      block { height timestamp { iso8601 } }
      sender { address }
      receiver { address }
      amount
      transaction { signature }
    }
  }
}
```

The `limitBy.each` clause is the key — it returns one row per distinct receiver, which combined with `asc: "block.height"` is exactly the seed-funding row.

> Gotcha: V1 sort fields must also appear in the projection. Sorting by `transaction.transactionIndex` without selecting it returns `Can't use transaction.transactionIndex in sorting`.

> Chunk the `wallets` array — batches of 25–30 work well; very large `in` lists time out.

### 2.2 Hop-2 upstream trace — same query, swapped inputs

If step 2.1 produces a sender that shows up many times, you've already found the funder — stop. If instead every wash wallet has a unique one-time sender (ephemeral intermediates), feed those sender addresses back into the same query as the new `wallets` argument. The dominant sender at this second hop is the orchestrator.

## Pipeline orchestration

```python
# Phase 1 — Wash-trade detection (Bitquery MCP).
candidates  = mcp.trending_tokens(blockchain="Solana", window_hours=12,
                                  sort="volume_usd", min_volume_usd=50000, limit=60)
# (or)        mcp.execute_sql(sql=WASH_CANDIDATE_SQL)

per_token = {}
for c in shortlist(candidates):
    rows = mcp.top_traders_by_token(address=c.mint, blockchain="Solana",
                                    window_hours=12, sort="trades", limit=30)
    per_token[c.symbol] = [r.wallet for r in rows
                           if roundtrip_ratio(r.buy_usd, r.sell_usd) >= 0.4]

# Phase 2 — Origin tracing (V1 GraphQL transfers).
funding   = gql_v1(FIRST_FUNDING_Q, wallets=flatten(per_token), since="2026-04-21")
funders   = aggregate_by_sender(funding)
upstream  = gql_v1(FIRST_FUNDING_Q, wallets=ephemeral_intermediates(funders), since="2026-04-15")
```

A complete reference implementation lives at `scripts/run_pipeline.py`. The bootstrap version in this repo runs **all four steps** through the V1 GraphQL endpoint (because the wash-detection step was easier to verify with the same auth surface). When the MCP is wired up, port phase 1 to `mcp.trending_tokens` + `mcp.top_traders_by_token`; phase 2 stays on the GraphQL endpoint regardless.

## What "good" output looks like

A clean wash-trade case (OpenLie in this dataset):

- One funder wallet sent the same fixed amount (0.5 SOL) to N receivers within seconds.
- Each receiver's later DEX activity is a single token, balanced buy and sell USD totals.
- The funder's own first inbound was a single discrete deposit a few minutes prior — easy to follow upstream.

A "diffuse" case (babyai16z, QOTUS):

- Each wash wallet has a unique one-time sender, almost all dust amounts (0.002 SOL).
- No address repeats. Likely a generator service; either accept the limit or pivot to USDC tracing.

## Reporting

Always cite the **transaction signatures** for the disbursement burst. Without them the case looks soft. The first-funding query already returns `transaction.signature` per row — surface 3–5 of those in the report alongside the funder address.

Save:
- The full query texts to `queries/`
- Raw API responses to `data/` and `funding/`
- The aggregated answer to `data/funders_summary.json`
- A markdown report at the workspace root

## Common failure modes

- **MCP tool returns `requires authentication`** — the Bitquery MCP at `http://mcp.bitquery.io/` isn't connected for this session. Ask the user to reconnect the connector, then retry. If they need the data right now, fall through to the GraphQL endpoint with their API key.
- **`execute_sql` errors on column names** — the MCP description lists table groups (`trades_*`, `pairs_*`, `tokens_*`, `currencies_*`) but exact column names vary by account. Run `SHOW TABLES LIKE 'trades_%'` and `DESCRIBE trades_by_token_address` first, then customize the SQL.
- **`Cannot query field "solana"`** — you're hitting the V2 GraphQL endpoint (`streaming.bitquery.io`) with a V1 query. Use lowercase `solana` only at `graphql.bitquery.io`. The transfer-trace step needs V1.
- **Empty arrays from V2 `Transfers` for known-active wallets** — V2 native-SOL representation differs by cube. The V1 `solana.transfers` shape used in step 2.1 is the one that works for SOL flows.
- **`null` aggregations on V1** — count-of-distinct fields like `count(uniq: senders)` aren't supported on the V1 transfers reducer. Either pull rows and aggregate client-side, or do the aggregation in the MCP via `execute_sql`.
