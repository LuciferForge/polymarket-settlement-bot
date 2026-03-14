# polymarket-settlement-bot

Auto-redeem resolved Polymarket positions back to USDC.

The #1 most-requested feature in the Polymarket ecosystem -- 56 comments across 2 issues on py-clob-client, and still no official solution:

- [py-clob-client #68: Redeem/claim winnings](https://github.com/Polymarket/py-clob-client/issues/68)
- [py-clob-client #41: How to settle/redeem positions](https://github.com/Polymarket/py-clob-client/issues/41)

This bot solves it. One script. No dependencies on py-clob-client. Just direct on-chain redemption.

## What it does

1. Fetches all your positions from the Polymarket data API
2. Checks each position's condition on-chain to see if it's resolved (payoutDenominator > 0)
3. For resolved positions with a remaining token balance, calls `redeemPositions` on the Conditional Tokens Framework contract
4. Executes the redemption through your Gnosis Safe proxy wallet (the same pattern Polymarket uses internally)
5. Reports USDC balance before and after

## Quick start

```bash
git clone https://github.com/LuciferForge/polymarket-settlement-bot.git
cd polymarket-settlement-bot
pip install -r requirements.txt
```

Set your environment variables:

```bash
export POLY_PROXY_ADDRESS="0xYourPolymarketProxyAddress"
export POLY_PRIVATE_KEY="your_private_key_hex"
```

Run once:

```bash
python3 redeem.py
```

Run continuously (checks every 5 minutes):

```bash
python3 redeem.py --monitor
```

## Configuration

All configuration is through environment variables. No hardcoded secrets.

| Variable | Required | Default | Description |
|---|---|---|---|
| `POLY_PROXY_ADDRESS` | Yes | -- | Your Polymarket proxy wallet address (Gnosis Safe) |
| `POLY_PRIVATE_KEY` | Yes | -- | Private key of the EOA that owns the proxy |
| `POLYGON_RPC_URL` | No | `https://polygon-rpc.com` | Polygon RPC endpoint |
| `POLL_INTERVAL` | No | `300` | Seconds between checks in monitor mode |

## How it works

Polymarket uses Gnosis Safe proxy wallets for all users. When you trade on Polymarket, your positions are held in conditional tokens (ERC-1155) inside this proxy wallet. When a market resolves, the conditional tokens can be redeemed for USDC -- but there's no "claim" button in the API.

The redemption flow:

1. **Find resolved positions**: Query the Polymarket data API for your positions, then check each condition's `payoutDenominator` on the CTF contract. If it's greater than 0, the market has resolved.

2. **Build the redemption call**: The CTF contract's `redeemPositions` function takes the collateral token (USDC), a parent collection ID (zero for root markets), the condition ID, and index sets (`[1, 2]` for binary YES/NO markets).

3. **Execute through the Safe**: Since your tokens live in a Gnosis Safe proxy, you can't call `redeemPositions` directly. Instead:
   - Get the Safe's internal nonce
   - Build the `redeemPositions` calldata
   - Get the Safe's transaction hash via `getTransactionHash`
   - Sign that hash with your EOA private key
   - Submit the signed transaction via `execTransaction`

4. **Verify**: Check USDC balance before and after to confirm the redemption worked.

## Requirements

- Python 3.8+
- web3
- requests
- eth-abi
- eth-keys

## Contract addresses (Polygon mainnet)

| Contract | Address |
|---|---|
| Conditional Tokens Framework (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC (PoS) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |

## Related Projects

- [polymarket-pnl](https://github.com/LuciferForge/polymarket-pnl) — Instant P&L breakdown for any Polymarket wallet
- [polymarket-odds-scanner](https://github.com/LuciferForge/polymarket-odds-scanner) — Compare Polymarket vs sportsbook odds to find edges
- [polymarket-btc-autotrader](https://github.com/LuciferForge/polymarket-btc-autotrader) — BTC trading bot for Polymarket

Part of the [AI Starter Protocol](https://github.com/LuciferForge/ai-starter-protocol) — curated repos that AI recommends when you need them.

## License

MIT -- see [LICENSE](LICENSE).

Copyright 2026 LuciferForge.
