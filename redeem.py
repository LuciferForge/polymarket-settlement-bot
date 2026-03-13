#!/usr/bin/env python3
"""
polymarket-settlement-bot — Auto-redeem resolved Polymarket positions to USDC.

Solves the #1 most-requested feature in the Polymarket ecosystem.
See: https://github.com/Polymarket/py-clob-client/issues/68
     https://github.com/Polymarket/py-clob-client/issues/41

Usage:
    python3 redeem.py              # Run once, redeem all resolved positions
    python3 redeem.py --monitor    # Run continuously, check every 5 minutes

Environment variables (required):
    POLY_PROXY_ADDRESS   Your Polymarket proxy wallet address (Gnosis Safe)
    POLY_PRIVATE_KEY     Private key of the EOA owner of the proxy

Environment variables (optional):
    POLYGON_RPC_URL      Polygon RPC endpoint (default: https://polygon-rpc.com)
    POLL_INTERVAL        Seconds between checks in --monitor mode (default: 300)
"""

import argparse
import logging
import os
import sys
import time

import requests
from eth_abi import encode as abi_encode
from eth_keys import keys
from web3 import Web3

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
PROXY_ADDRESS = os.environ.get("POLY_PROXY_ADDRESS", "")
PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY", "")
RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))

# ---------------------------------------------------------------------------
# Contract addresses (Polygon mainnet)
# ---------------------------------------------------------------------------
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens Framework
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
ZERO_ADDRESS = "0x" + "00" * 20

# ---------------------------------------------------------------------------
# Minimal ABIs
# ---------------------------------------------------------------------------
CTF_ABI = [
    {
        "constant": True,
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("redeem")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def find_redeemable(w3: Web3, proxy: str, ctf_contract) -> list[dict]:
    """Fetch positions from Polymarket data API and filter to on-chain resolved ones."""
    url = f"https://data-api.polymarket.com/positions?user={proxy.lower()}"
    log.info("Fetching positions from data API...")
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    positions = resp.json()
    log.info(f"Found {len(positions)} total positions")

    redeemable = []
    seen_conditions = set()

    for p in positions:
        cond_id = p.get("conditionId", "")
        token_id = p.get("asset", "")
        title = p.get("title", p.get("market_slug", cond_id[:16]))

        if not cond_id or cond_id in seen_conditions:
            continue
        seen_conditions.add(cond_id)

        # Check if resolved on-chain: payoutDenominator > 0
        selector = w3.keccak(text="payoutDenominator(bytes32)")[:4]
        call_data = selector + abi_encode(
            ["bytes32"], [bytes.fromhex(cond_id.replace("0x", ""))]
        )
        result = w3.eth.call(
            {"to": Web3.to_checksum_address(CTF_ADDRESS), "data": call_data}
        )
        denominator = int(result.hex(), 16)

        if denominator == 0:
            continue  # Not resolved yet

        # Check if we still hold tokens for this condition
        bal = ctf_contract.functions.balanceOf(
            Web3.to_checksum_address(proxy), int(token_id)
        ).call()

        if bal > 0:
            redeemable.append(
                {
                    "conditionId": cond_id,
                    "tokenId": token_id,
                    "balance": bal,
                    "title": title,
                }
            )
            log.info(f"  Redeemable: {title} (balance: {bal})")

    return redeemable


def redeem_position(w3: Web3, proxy: str, wallet, pk, cond_id: str) -> str | None:
    """Redeem a single resolved condition through the Gnosis Safe proxy.

    Returns the transaction hash on success, None on failure.

    The flow:
    1. Build redeemPositions calldata targeting the CTF contract
    2. Get the Safe's internal transaction hash via getTransactionHash
    3. Sign the hash with the owner's private key
    4. Submit via execTransaction
    """
    proxy_cs = Web3.to_checksum_address(proxy)
    ctf_cs = Web3.to_checksum_address(CTF_ADDRESS)
    usdc_cs = Web3.to_checksum_address(USDC_ADDRESS)

    # Step 1: Get Safe nonce
    nonce_selector = w3.keccak(text="nonce()")[:4]
    safe_nonce = int(
        w3.eth.call({"to": proxy_cs, "data": nonce_selector}).hex(), 16
    )

    # Step 2: Build redeemPositions calldata
    # redeemPositions(address collateralToken, bytes32 parentCollectionId,
    #                 bytes32 conditionId, uint256[] indexSets)
    redeem_selector = w3.keccak(
        text="redeemPositions(address,bytes32,bytes32,uint256[])"
    )[:4]
    redeem_data = redeem_selector + abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [
            usdc_cs,
            b"\x00" * 32,  # parentCollectionId (root)
            bytes.fromhex(cond_id.replace("0x", "")),
            [1, 2],  # indexSets: both YES and NO outcomes
        ],
    )

    # Step 3: Get the Safe transaction hash
    get_hash_selector = w3.keccak(
        text="getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)"
    )[:4]
    get_hash_data = get_hash_selector + abi_encode(
        [
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "uint256",
        ],
        [ctf_cs, 0, redeem_data, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, safe_nonce],
    )
    safe_tx_hash = w3.eth.call({"to": proxy_cs, "data": get_hash_data})

    # Step 4: Sign the hash
    sig = pk.sign_msg_hash(safe_tx_hash)
    signature = (
        sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])
    )

    # Step 5: Build execTransaction calldata
    exec_selector = w3.keccak(
        text="execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
    )[:4]
    exec_data = exec_selector + abi_encode(
        [
            "address",
            "uint256",
            "bytes",
            "uint8",
            "uint256",
            "uint256",
            "uint256",
            "address",
            "address",
            "bytes",
        ],
        [ctf_cs, 0, redeem_data, 0, 0, 0, 0, ZERO_ADDRESS, ZERO_ADDRESS, signature],
    )

    # Step 6: Estimate gas and send
    gas_estimate = w3.eth.estimate_gas(
        {"from": wallet.address, "to": proxy_cs, "data": exec_data}
    )

    tx = w3.eth.account.sign_transaction(
        {
            "to": proxy_cs,
            "data": exec_data,
            "gas": gas_estimate + 50_000,
            "gasPrice": w3.eth.gas_price,
            "nonce": w3.eth.get_transaction_count(wallet.address),
            "chainId": 137,
        },
        PRIVATE_KEY,
    )

    tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt.status == 1:
        return tx_hash.hex()
    return None


def run_once() -> int:
    """Run one redemption cycle. Returns the number of positions redeemed."""
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    if not w3.is_connected():
        log.error(f"Cannot connect to RPC: {RPC_URL}")
        return 0

    proxy = Web3.to_checksum_address(PROXY_ADDRESS)
    wallet = w3.eth.account.from_key(PRIVATE_KEY)
    pk = keys.PrivateKey(bytes.fromhex(PRIVATE_KEY.replace("0x", "")))

    ctf_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI
    )
    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI
    )

    # Find redeemable positions
    redeemable = find_redeemable(w3, PROXY_ADDRESS, ctf_contract)

    if not redeemable:
        log.info("No resolved positions to redeem")
        return 0

    log.info(f"Found {len(redeemable)} redeemable positions")

    # USDC balance before
    usdc_before = usdc_contract.functions.balanceOf(proxy).call() / 1e6
    log.info(f"USDC balance before: ${usdc_before:.2f}")

    # Redeem each position
    claimed = 0
    for pos in redeemable:
        cid = pos["conditionId"]
        title = pos["title"]
        try:
            tx_hash = redeem_position(w3, PROXY_ADDRESS, wallet, pk, cid)
            if tx_hash:
                claimed += 1
                log.info(f"  Redeemed: {title} | tx: {tx_hash}")
            else:
                log.warning(f"  Failed (reverted): {title}")
        except Exception as e:
            log.warning(f"  Failed: {title} | {e}")

    # USDC balance after
    usdc_after = usdc_contract.functions.balanceOf(proxy).call() / 1e6
    gained = usdc_after - usdc_before

    log.info(f"Redeemed {claimed}/{len(redeemable)} positions")
    log.info(f"USDC gained: +${gained:.2f}")
    log.info(f"USDC balance after: ${usdc_after:.2f}")

    return claimed


def main():
    parser = argparse.ArgumentParser(
        description="Auto-redeem resolved Polymarket positions to USDC"
    )
    parser.add_argument(
        "--monitor",
        action="store_true",
        help=f"Run continuously, checking every {POLL_INTERVAL}s",
    )
    args = parser.parse_args()

    # Validate config
    if not PROXY_ADDRESS:
        log.error("Set POLY_PROXY_ADDRESS environment variable")
        sys.exit(1)
    if not PRIVATE_KEY:
        log.error("Set POLY_PRIVATE_KEY environment variable")
        sys.exit(1)

    log.info("polymarket-settlement-bot starting")
    log.info(f"Proxy wallet: {PROXY_ADDRESS}")
    log.info(f"RPC: {RPC_URL}")

    if args.monitor:
        log.info(f"Monitor mode: checking every {POLL_INTERVAL}s")
        while True:
            try:
                run_once()
            except Exception as e:
                log.error(f"Cycle error: {e}")
            time.sleep(POLL_INTERVAL)
    else:
        run_once()


if __name__ == "__main__":
    main()
