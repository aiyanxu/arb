#!/usr/bin/env python3
"""One-time Polymarket Perps proxy-credential generator.

Polymarket Perps trading authenticates through a PROXY wallet: a fresh EVM
keypair whose address the OWNER wallet delegates via an EIP-712 CreateProxy
signature. The API answers with a secret; the bot then needs exactly three
values in .env (POLYMARKET_PROXY_ADDRESS / _PRIVATE_KEY / _SECRET).

Credentials expire (default ~1 week) — re-run this script and update all
three values together.

Usage:
  python tools/polymarket_make_proxy.py --owner-key 0x... [--expiry-days 7]
                                        [--label entropy-arb] [--api-url URL]

The owner key is read from the flag or POLYMARKET_OWNER_PRIVATE_KEY and is
never written anywhere; the PROXY key is generated fresh each run.

Requires: eth_account, aiohttp (both in the [live] extras).
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import time

API_URL = "https://api.perpetuals.polymarket.com"

EIP712_DOMAIN = {"name": "Polymarket", "version": "1", "chainId": 137}
EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "CreateProxy": [
        {"name": "addr", "type": "address"},
        {"name": "exp", "type": "uint64"},
        {"name": "salt", "type": "uint64"},
        {"name": "ts", "type": "uint64"},
    ],
}


async def create_proxy(owner_key: str, expiry_days: float, label: str,
                       api_url: str) -> dict:
    from eth_account import Account
    import aiohttp

    owner = Account.from_key(owner_key)
    proxy = Account.create()                    # fresh keypair — throwaway old
    now_ms = int(time.time() * 1000)
    exp_ms = int(now_ms + expiry_days * 86400_000)
    salt = secrets.randbits(63)

    sig = Account.sign_typed_data(
        owner.key,
        full_message={"types": EIP712_TYPES, "primaryType": "CreateProxy",
                      "domain": EIP712_DOMAIN,
                      "message": {"addr": proxy.address, "exp": exp_ms,
                                  "salt": salt, "ts": now_ms}}).signature.hex()

    body = {"op": {"type": "createProxy",
                   "args": {"owner": owner.address,
                            "proxy": proxy.address, "expiry": exp_ms}},
            "sig": "0x" + sig if not sig.startswith("0x") else sig,
            "salt": salt, "ts": now_ms, "label": label}
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url + "/v1/account/proxy", json=body,
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            text = await r.text()
            if r.status != 200:
                raise RuntimeError(f"createProxy failed: HTTP {r.status} "
                                   f"{text[:300]}")
            resp = await r.json(content_type=None)
    secret = resp.get("secret")
    if not secret:
        raise RuntimeError(f"no secret in response: {str(resp)[:200]}")
    return {"proxy_address": proxy.address,
            "proxy_private_key": proxy.key.hex(),
            "proxy_secret": secret, "expires_ms": exp_ms}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--owner-key",
                    default=None,
                    help="Polymarket OWNER wallet private key (defaults to "
                         "POLYMARKET_OWNER_PRIVATE_KEY env var)")
    ap.add_argument("--expiry-days", type=float, default=7.0,
                    help="credential lifetime in days (default 7)")
    ap.add_argument("--label", default="entropy-arb",
                    help="credential label shown in Polymarket settings")
    ap.add_argument("--api-url", default=API_URL)
    args = ap.parse_args()

    import os
    owner_key = args.owner_key or os.getenv("POLYMARKET_OWNER_PRIVATE_KEY")
    if not owner_key:
        print("error: pass --owner-key or set POLYMARKET_OWNER_PRIVATE_KEY",
              file=sys.stderr)
        return 2

    try:
        out = asyncio.run(create_proxy(owner_key, args.expiry_days,
                                       args.label, args.api_url))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    exp = time.strftime("%Y-%m-%d %H:%M:%S UTC",
                        time.gmtime(out["expires_ms"] / 1000))
    print("Proxy credential created — add these to .env:")
    print(f"POLYMARKET_PROXY_ADDRESS={out['proxy_address']}")
    print(f"POLYMARKET_PROXY_PRIVATE_KEY=0x{out['proxy_private_key']}")
    print(f"POLYMARKET_PROXY_SECRET={out['proxy_secret']}")
    print(f"\nExpires {exp} — re-run this script afterwards and replace all "
          f"three values together.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
