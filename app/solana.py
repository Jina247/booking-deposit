"""Solana Pay helpers: build a payment link and verify the payment on-chain.

Idea: every booking gets a unique random "reference" public key. The customer's
wallet adds it to the payment transaction. Later we ask the RPC node for
transactions that mention that reference, then check the USDC balance change of
the shop's wallet. The browser is never trusted to say "paid".
"""
import os
from decimal import Decimal
from urllib.parse import quote, urlencode

import base58
import httpx

from . import config


def new_reference() -> str:
    return base58.b58encode(os.urandom(32)).decode()


def payment_url(amount_cents: int, reference: str, label: str, message: str) -> str:
    amount = (Decimal(amount_cents) / 100).quantize(Decimal("0.01"))
    query = urlencode(
        {
            "amount": str(amount),
            "spl-token": config.USDC_MINT,
            "reference": reference,
            "label": label,
            "message": message,
        },
        quote_via=quote,
    )
    return f"solana:{config.MERCHANT_WALLET}?{query}"


def _rpc(client: httpx.Client, method: str, params: list):
    r = client.post(config.RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(body["error"])
    return body["result"]


def _usdc_received(tx: dict) -> Decimal:
    """USDC the merchant wallet gained in this transaction."""
    meta = tx.get("meta") or {}

    def total(balances):
        t = Decimal(0)
        for b in balances or []:
            if b.get("owner") == config.MERCHANT_WALLET and b.get("mint") == config.USDC_MINT:
                t += Decimal(b["uiTokenAmount"]["uiAmountString"])
        return t

    return total(meta.get("postTokenBalances")) - total(meta.get("preTokenBalances"))


def find_payment(reference: str, amount_cents: int) -> str | None:
    """Return the transaction signature if a valid payment exists, else None."""
    needed = Decimal(amount_cents) / 100
    with httpx.Client(timeout=10) as client:
        sigs = _rpc(client, "getSignaturesForAddress", [reference, {"limit": 5, "commitment": "confirmed"}])
        for s in sigs:
            if s.get("err") is not None:
                continue
            tx = _rpc(
                client,
                "getTransaction",
                [s["signature"], {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
            )
            if tx and _usdc_received(tx) >= needed:
                return s["signature"]
    return None
