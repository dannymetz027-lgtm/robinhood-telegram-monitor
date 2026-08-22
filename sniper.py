"""Uniswap auto-buy sniper for Robinhood Chain (keyword matches only)."""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address
import requests

RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "").strip()
# ~$15–25 depending on ETH; override with BUY_AMOUNT_ETH
BUY_AMOUNT_ETH = float(os.environ.get("BUY_AMOUNT_ETH", "0.005"))
SLIPPAGE_BPS = int(os.environ.get("SLIPPAGE_BPS", "3000"))  # 30%
MAX_BUYS_PER_DAY = int(os.environ.get("MAX_BUYS_PER_DAY", "20"))
AUTO_BUY_ENABLED = os.environ.get("AUTO_BUY_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

CHAIN_ID = 4663
WETH = to_checksum_address("0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73")
UNI_V2_ROUTER = to_checksum_address("0x89e5DB8B5aA49aA85AC63f691524311AEB649eba")
UNI_V3_ROUTER = to_checksum_address("0xCaf681a66D020601342297493863E78C959E5cb2")

REQUEST_TIMEOUT = 20

_buys_today = 0
_buys_day: date | None = None
_bought_tokens: set[str] = set()


def _log(message: str) -> None:
    print(f"[sniper] {message}", flush=True)


def sniper_ready() -> bool:
    return bool(PRIVATE_KEY) and AUTO_BUY_ENABLED and BUY_AMOUNT_ETH > 0


def sniper_status() -> str:
    if not AUTO_BUY_ENABLED:
        return "disabled (AUTO_BUY_ENABLED=false)"
    if not PRIVATE_KEY:
        return "waiting for PRIVATE_KEY"
    try:
        acct = Account.from_key(PRIVATE_KEY)
        return f"ready wallet={acct.address} amount={BUY_AMOUNT_ETH} ETH/day_cap={MAX_BUYS_PER_DAY}"
    except Exception as exc:
        return f"invalid PRIVATE_KEY ({exc})"


def _rpc(method: str, params: list[Any]) -> Any:
    response = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def _reset_daily_counter() -> None:
    global _buys_today, _buys_day
    today = date.today()
    if _buys_day != today:
        _buys_day = today
        _buys_today = 0


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _build_v2_buy(token: str, amount_wei: int, recipient: str, deadline: int) -> tuple[str, bytes, int]:
    # swapExactETHForTokensSupportingFeeOnTransferTokens(uint amountOutMin, address[] path, address to, uint deadline)
    data = _selector(
        "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)"
    ) + abi_encode(
        ["uint256", "address[]", "address", "uint256"],
        [0, [WETH, to_checksum_address(token)], to_checksum_address(recipient), deadline],
    )
    return UNI_V2_ROUTER, data, amount_wei


def _build_v3_buy(
    token: str, fee: int, amount_wei: int, recipient: str, deadline: int
) -> tuple[str, bytes, int]:
    # exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))
    params = (
        WETH,
        to_checksum_address(token),
        int(fee),
        to_checksum_address(recipient),
        int(deadline),
        int(amount_wei),
        0,  # amountOutMinimum
        0,  # sqrtPriceLimitX96
    )
    data = _selector(
        "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))"
    ) + abi_encode(
        ["(address,address,uint24,address,uint256,uint256,uint256,uint160)"],
        [params],
    )
    return UNI_V3_ROUTER, data, amount_wei


def try_auto_buy(pool: dict[str, Any]) -> dict[str, Any]:
    """
    Attempt a keyword auto-buy. Returns a result dict for Telegram reporting.
    """
    global _buys_today

    base = pool.get("baseToken") or {}
    token = base.get("address")
    name = base.get("name") or ""
    symbol = base.get("symbol") or ""

    result: dict[str, Any] = {
        "attempted": False,
        "success": False,
        "skipped": True,
        "reason": "",
        "tx_hash": None,
        "wallet": None,
        "amount_eth": BUY_AMOUNT_ETH,
        "token": token,
        "name": name,
        "symbol": symbol,
    }

    if not sniper_ready():
        result["reason"] = sniper_status()
        return result

    if not token:
        result["reason"] = "missing token address"
        return result

    token_l = token.lower()
    if token_l in _bought_tokens:
        result["reason"] = "already bought this token"
        return result

    _reset_daily_counter()
    if _buys_today >= MAX_BUYS_PER_DAY:
        result["reason"] = f"daily buy cap reached ({MAX_BUYS_PER_DAY})"
        return result

    # Must be paired with WETH for ETH buy path
    t0 = (pool.get("token0") or "").lower()
    t1 = (pool.get("token1") or "").lower()
    if WETH.lower() not in {t0, t1}:
        result["reason"] = "pool is not WETH-paired (skipped)"
        return result

    try:
        account = Account.from_key(PRIVATE_KEY)
    except Exception as exc:
        result["reason"] = f"bad private key: {exc}"
        return result

    result["wallet"] = account.address
    result["attempted"] = True
    result["skipped"] = False

    amount_wei = int(BUY_AMOUNT_ETH * 10**18)
    balance = int(_rpc("eth_getBalance", [account.address, "latest"]), 16)
    # leave some ETH for gas
    if balance < amount_wei + 10**15:
        result["reason"] = f"insufficient balance ({balance / 1e18:.6f} ETH)"
        return result

    deadline = int(time.time()) + 120
    dex = pool.get("dex") or ""
    fee = pool.get("fee")

    try:
        if dex == "uniswap-v2" or fee is None:
            to, data, value = _build_v2_buy(token, amount_wei, account.address, deadline)
        else:
            to, data, value = _build_v3_buy(token, int(fee), amount_wei, account.address, deadline)
    except Exception as exc:
        result["reason"] = f"build tx failed: {exc}"
        return result

    nonce = int(_rpc("eth_getTransactionCount", [account.address, "pending"]), 16)
    gas_price = int(_rpc("eth_gasPrice", []), 16)
    # bump gas slightly for snipes
    gas_price = int(gas_price * 1.25)

    tx = {
        "to": to,
        "value": value,
        "data": data,
        "nonce": nonce,
        "gas": 500000,
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    }

    # eth_estimateGas optional; if it fails we still try with fixed gas
    try:
        estimate = int(
            _rpc(
                "eth_estimateGas",
                [
                    {
                        "from": account.address,
                        "to": to,
                        "value": hex(value),
                        "data": "0x" + data.hex(),
                    }
                ],
            ),
            16,
        )
        tx["gas"] = int(estimate * 1.3)
    except Exception as exc:
        _log(f"gas estimate failed (using fallback): {exc}")

    try:
        signed = account.sign_transaction(tx)
        raw = signed.raw_transaction.hex()
        if not raw.startswith("0x"):
            raw = "0x" + raw
        tx_hash = _rpc("eth_sendRawTransaction", [raw])
        _bought_tokens.add(token_l)
        _buys_today += 1
        result["success"] = True
        result["tx_hash"] = tx_hash
        result["reason"] = "submitted"
        _log(f"BUY submitted {symbol} {token} tx={tx_hash}")
        return result
    except Exception as exc:
        result["reason"] = f"send failed: {exc}"
        _log(result["reason"])
        return result
