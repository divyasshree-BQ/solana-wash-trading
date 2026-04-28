#!/usr/bin/env python3
"""
Solana wash-trade investigation pipeline (Bitquery).

Inputs:
  BQ_TOKEN         — Bitquery bearer token (env var or first CLI arg)
  --since          — ISO8601 start of detection window  (default: 12h ago)
  --funding-since  — ISO8601 start of funding lookback  (default: 7d ago)
  --top-n-tokens   — top N candidate tokens to drill into  (default: 5)
  --traders-per-token — top wash traders per token       (default: 30)

Outputs (next to this script's parent dir):
  data/candidates_ranked.json
  data/top_wash_traders.csv
  data/wash_wallets_by_token.json
  funding/first_funding.json
  funding/upstream_funding.json
  data/funders_summary.json

Endpoints:
  V2 / EAP   https://streaming.bitquery.io/graphql   (capital Solana)
  V1         https://graphql.bitquery.io             (lowercase solana, supports limitBy)
"""

import argparse, csv, json, math, os, subprocess, sys, time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

V2 = "https://streaming.bitquery.io/graphql"
V1 = "https://graphql.bitquery.io"

ROOT = Path(__file__).resolve().parents[1]
(DATA, FUNDS, Q) = (ROOT/"data", ROOT/"funding", ROOT/"queries")
for p in (DATA, FUNDS, Q): p.mkdir(parents=True, exist_ok=True)

# ---------- query bodies ----------

Q1 = """query WashCandidates($since: DateTime!) {
  Solana {
    DEXTradeByTokens(
      where: {
        Block: {Time: {since: $since}},
        Trade: {
          Currency: {MintAddress: {notIn: [
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
          ]}},
          Side: {Currency: {MintAddress: {in: [
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
          ]}}}
        }
      }
      orderBy: {descendingByField: "trades"}
      limit: {count: 60}
    ) {
      Trade { Currency { Symbol Name MintAddress } }
      trades:    count
      traders:   count(distinct: Trade_Account_Owner)
      volume_usd: sum(of: Trade_Side_AmountInUSD)
      buyers:    count(distinct: Trade_Account_Owner, if: {Trade: {Side: {Type: {is: buy}}}})
      sellers:   count(distinct: Trade_Account_Owner, if: {Trade: {Side: {Type: {is: sell}}}})
    }
  }
}"""

Q2 = """query TopWashTraders($since: DateTime!, $mint: String!) {
  Solana {
    DEXTradeByTokens(
      where: { Block: {Time: {since: $since}}, Trade: {Currency: {MintAddress: {is: $mint}}} }
      orderBy: {descendingByField: "trades"}
      limit: {count: 30}
    ) {
      Trade { Account { Owner } }
      trades:   count
      buy_usd:  sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: buy}}}})
      sell_usd: sum(of: Trade_Side_AmountInUSD, if: {Trade: {Side: {Type: {is: sell}}}})
      buys:     count(if: {Trade: {Side: {Type: {is: buy}}}})
      sells:    count(if: {Trade: {Side: {Type: {is: sell}}}})
    }
  }
}"""

Q3 = """query FirstFunding($wallets: [String!]!, $since: ISO8601DateTime!) {
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
}"""

# ---------- helpers ----------

def gql(endpoint: str, query: str, variables: dict, token: str) -> dict:
    payload = json.dumps({"query": query, "variables": variables})
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", endpoint,
         "-H", "Content-Type: application/json",
         "-H", f"Authorization: Bearer {token}",
         "-d", payload],
        capture_output=True, text=True, check=True,
    )
    out = json.loads(r.stdout)
    if "errors" in out:
        raise RuntimeError(f"GraphQL errors: {out['errors']}")
    return out["data"]

def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

# ---------- pipeline steps ----------

def step1_candidates(token: str, since: str) -> list[dict]:
    print(f"[1/4] Candidates since {since}")
    data = gql(V2, Q1, {"since": since}, token)
    rows = data["Solana"]["DEXTradeByTokens"]
    out = []
    for r in rows:
        trades = int(r["trades"]); traders = int(r["traders"])
        vol = float(r["volume_usd"] or 0)
        buyers = int(r["buyers"]); sellers = int(r["sellers"])
        avg = vol/trades if trades else 0
        tpt = trades/max(traders,1)
        overlap = (buyers + sellers - traders) / max(traders, 1)
        score = (math.log10(max(tpt,1))
                 * (1 if avg < 5 else 0.4)
                 * (overlap if overlap > 0.3 else 0.2))
        out.append({
            "symbol": r["Trade"]["Currency"]["Symbol"],
            "name":   r["Trade"]["Currency"]["Name"],
            "mint":   r["Trade"]["Currency"]["MintAddress"],
            "trades": trades, "traders": traders,
            "volume_usd": round(vol, 2),
            "avg_trade_usd": round(avg, 4),
            "trades_per_trader": round(tpt, 1),
            "both_sides_pct": round(overlap*100, 1),
            "wash_score": round(score, 3),
        })
    out.sort(key=lambda x: x["wash_score"], reverse=True)
    (DATA/"candidates_ranked.json").write_text(json.dumps(out, indent=2))
    print(f"      kept {len(out)} candidates  (top: {out[0]['symbol']} score={out[0]['wash_score']})")
    return out

def step2_top_traders(token: str, since: str, candidates: list[dict], top_n: int):
    print(f"[2/4] Top wash traders for top-{top_n} candidates")
    per_token = {}; csv_rows = []
    for c in candidates[:top_n]:
        sym = c["symbol"]; mint = c["mint"]
        data = gql(V2, Q2, {"since": since, "mint": mint}, token)
        rows = data["Solana"]["DEXTradeByTokens"]
        ranked = []
        for r in rows:
            b = float(r["buy_usd"] or 0); s = float(r["sell_usd"] or 0)
            ratio = min(b,s)/max(b,s) if max(b,s) > 0 else 0
            ranked.append({
                "token": sym, "mint": mint,
                "wallet": r["Trade"]["Account"]["Owner"],
                "trades": int(r["trades"]),
                "buys": int(r["buys"]), "sells": int(r["sells"]),
                "buy_usd": round(b,2), "sell_usd": round(s,2),
                "roundtrip_ratio": round(ratio,3),
            })
        ranked.sort(key=lambda x:(x["roundtrip_ratio"], x["trades"]), reverse=True)
        # keep only round-trip wallets (≥0.4 — strict)
        per_token[sym] = [x["wallet"] for x in ranked if x["roundtrip_ratio"] >= 0.4]
        csv_rows.extend(ranked)
        print(f"      {sym:<14} {len(ranked)} rows -> {len(per_token[sym])} round-trip wallets")
        time.sleep(0.2)
    with open(DATA/"top_wash_traders.csv","w",newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
        w.writeheader(); w.writerows(csv_rows)
    (DATA/"wash_wallets_by_token.json").write_text(json.dumps(per_token, indent=2))
    return per_token

def step3_funding(token: str, funding_since: str, per_token: dict) -> list[dict]:
    wallets = sorted({w for ws in per_token.values() for w in ws})
    print(f"[3/4] First-inbound SOL trace for {len(wallets)} wash wallets")
    rows = []
    for batch in chunked(wallets, 25):
        data = gql(V1, Q3, {"wallets": batch, "since": funding_since}, token)
        rows.extend(data["solana"]["transfers"] or [])
        time.sleep(0.3)
    (FUNDS/"first_funding.json").write_text(json.dumps(rows, indent=2))
    print(f"      got {len(rows)} funding rows")
    return rows

def step4_aggregate(funding: list[dict], per_token: dict, token: str, funding_since: str):
    print("[4/4] Aggregating funders + hop-2 trace where needed")
    fund_by_recv = {r["receiver"]["address"]: r for r in funding}
    summary = {}
    intermediates = set()
    for tok, wl in per_token.items():
        senders = Counter()
        for w in wl:
            if w in fund_by_recv:
                senders[fund_by_recv[w]["sender"]["address"]] += 1
        if not senders:
            summary[tok] = {"funder": None, "wallets_traced": 0}
            continue
        top, cnt = senders.most_common(1)[0]
        if cnt >= 5:                                       # direct funder
            seeded = [r for r in funding if r["sender"]["address"] == top and r["receiver"]["address"] in wl]
            sol = sum(float(r["amount"]) for r in seeded)
            summary[tok] = {
                "level": "direct", "funder": top, "wallets_seeded": cnt,
                "sol_distributed": round(sol, 4),
                "first_seed": min(r["block"]["timestamp"]["iso8601"] for r in seeded),
                "last_seed":  max(r["block"]["timestamp"]["iso8601"] for r in seeded),
            }
        else:                                              # ephemeral intermediates
            for w in wl:
                if w in fund_by_recv:
                    intermediates.add(fund_by_recv[w]["sender"]["address"])
            summary[tok] = {"level": "needs_hop2", "intermediates_count": len(senders)}
    if intermediates:
        print(f"      tracing hop-2 for {len(intermediates)} intermediates")
        rows = []
        for batch in chunked(sorted(intermediates), 25):
            data = gql(V1, Q3, {"wallets": batch, "since": funding_since}, token)
            rows.extend(data["solana"]["transfers"] or [])
            time.sleep(0.3)
        (FUNDS/"upstream_funding.json").write_text(json.dumps(rows, indent=2))
        # link each token's intermediates back to a hop-2 sender
        i_to_tok = defaultdict(set)
        for tok, wl in per_token.items():
            for w in wl:
                if w in fund_by_recv:
                    i_to_tok[fund_by_recv[w]["sender"]["address"]].add(tok)
        roots = defaultdict(lambda: defaultdict(int))
        for r in rows:
            for tok in i_to_tok.get(r["receiver"]["address"], []):
                roots[tok][r["sender"]["address"]] += 1
        for tok, sd in summary.items():
            if sd.get("level") == "needs_hop2":
                cnt = roots.get(tok, {})
                if cnt:
                    top, n = max(cnt.items(), key=lambda x: x[1])
                    sd.update({"level":"hop2","funder":top,"intermediates_seeded":n})
    (DATA/"funders_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary

# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="ISO8601 start of detection window (default: 12h ago)")
    ap.add_argument("--funding-since", default=None, help="ISO8601 start of funding lookback (default: 7d ago)")
    ap.add_argument("--top-n-tokens", type=int, default=5)
    ap.add_argument("token", nargs="?", default=os.environ.get("BQ_TOKEN"),
                    help="Bitquery bearer token (or set BQ_TOKEN env)")
    args = ap.parse_args()
    if not args.token:
        sys.exit("Bitquery token required (env BQ_TOKEN or arg)")
    now = datetime.now(timezone.utc)
    since = args.since or (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
    funding_since = args.funding_since or (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    cands = step1_candidates(args.token, since)
    pt    = step2_top_traders(args.token, since, cands, args.top_n_tokens)
    fund  = step3_funding(args.token, funding_since, pt)
    step4_aggregate(fund, pt, args.token, funding_since)

if __name__ == "__main__":
    main()
