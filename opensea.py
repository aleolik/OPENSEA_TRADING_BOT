import json
import os
from pathlib import Path
import time
from typing import Any, Dict, Optional, Tuple

from dotenv import load_dotenv
import requests
from web3 import Web3


dotenv_path = Path(__file__).resolve().parent / 'opensea.env'
load_dotenv(dotenv_path=dotenv_path)


class Opensea:
    BASE_URL = "https://api.opensea.io/api/v2"  # <- removed extra space & trailing slash
    API_KEY = (v := os.getenv("API_KEY")) or (_ for _ in ()).throw(ValueError("API_KEY is None"))
    DEFAULT_HEADERS = {"x-api-key": API_KEY, "accept": "application/json"}

    # ---------- HTTP helpers ----------
    @classmethod
    def _get(cls, path: str, params: Optional[Dict[str, Any]] = None, retries: int = 3, backoff: float = 0.8) -> Dict[str, Any]:
        """
        GET helper with:
          - timeouts
          - retry on 429/5xx
          - clear error messages (includes response body snippet)
        """
        url = f"{cls.BASE_URL}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                # separate connect/read timeouts; tweak as you like
                r = requests.get(url, headers=cls.DEFAULT_HEADERS, params=params, timeout=(5, 30))
                if r.status_code in (429, 500, 502, 503, 504) and attempt <= retries:
                    # best-effort respect Retry-After if provided
                    ra = r.headers.get("Retry-After")
                    delay = float(ra) if ra and ra.isdigit() else backoff * (2 ** (attempt - 1))
                    time.sleep(delay)
                    continue

                r.raise_for_status()

                # Try to parse JSON; raise if not JSON
                try:
                    return r.json()
                except json.JSONDecodeError:
                    snippet = (r.text or "")[:300]
                    raise RuntimeError(f"GET {path} returned non-JSON response (first 300 chars): {snippet}")

            except requests.Timeout as e:
                if attempt <= retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise RuntimeError(f"GET {path} timed out after {attempt} attempt(s).") from e
            except requests.HTTPError as e:
                # include server body to make debugging easy
                resp = e.response
                status = getattr(resp, "status_code", "?")
                reason = getattr(resp, "reason", "")
                body = (getattr(resp, "text", "") or "")[:500]
                raise RuntimeError(f"GET {path} failed ({status} {reason}). Body: {body}") from e
            except requests.RequestException as e:
                # network issues, DNS, SSL, connection reset, etc.
                if attempt <= retries:
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                raise RuntimeError(f"GET {path} network error after {attempt} attempt(s): {e}") from e
    
    @classmethod
    def _post(cls, path: str, json: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(f"{cls.BASE_URL}{path}", headers=cls.DEFAULT_HEADERS, json=json, timeout=30)
        if r.status_code >= 400:
            # show server’s message to debug (often "order not eligible", "missing field", etc.)
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
        return r.json()
    
    @staticmethod
    def build_listing_ref(listing: Dict[str, Any]) -> Dict[str, str]:
        protocol_address = (
            listing.get("protocol_address")
            or listing.get("seaport_address")
            or "0x0000000000000068F116a894984e2DB1123eB395"
        )
         
        order_hash = listing.get("hash") or listing.get("order_hash")
        if not order_hash:
            raise ValueError("Listing is missing order hash (hash/order_hash). Fetch a fresh listing.")
        chain = listing.get("chain") or (listing.get("nft") or {}).get("chain")
        if not chain:
            raise ValueError("Listing is missing chain.")
        return {"hash": order_hash, "chain": chain, "protocol_address": Web3.to_checksum_address(protocol_address)}
    
    @staticmethod
    def parse_price_current(listing: Dict[str, Any]) -> Tuple[float, str, int]:
        cur = (listing.get("price") or {}).get("current") or {}
        v = cur.get("value")
        decimals = int(cur.get("decimals", 18))
        symbol = cur.get("currency") or "ETH"
        # value may be float-like str or integer-like str
        if v is None:
            return (0.0, symbol, decimals)
        if isinstance(v, (int, float)):
            value = float(v)
        else:
            s = str(v)
            value = float(s) if "." in s else int(s) / (10 ** decimals)
        return (value, symbol, decimals)
    
    @classmethod
    def get_cheapest_listings(cls, collection_slug: str,limit=1) -> Dict[str, Any]:
        data = cls._get(f"/listings/collection/{collection_slug}/best", params={"limit": limit})
        listings = data.get("listings") or data.get("orders") or []
        if not listings:
            raise RuntimeError("No active listings found for this collection.")
        return listings
    
    @classmethod
    def get_fulfillment_data(cls, listing_ref: Dict[str, Any], address: str, quantity: int = 1) -> Dict[str, Any]:
        payload = {
            "listing": listing_ref,
            "fulfiller": {"address": Web3.to_checksum_address(address)},
            "quantity": str(quantity),
        }
        return cls._post("/listings/fulfillment_data", json=payload)
    
    @staticmethod
    def extract_nft_from_fulfillment(fd: dict) -> tuple[str, int]:
        """
        Pull the NFT (contract, tokenId) you just bought from OpenSea's fulfillment_data.
        Works for basic orders since OpenSea gives typed params in transaction.input_data.parameters.
        """
        fdata = fd.get("fulfillment_data") or {}
        tx = fdata.get("transaction") or {}
        input_data = tx.get("input_data") or {}
        p = input_data.get("parameters") or {}

        # For a basic order, the asset-to-buy is the *offer* item from the seller:
        # offerToken = NFT contract, offerIdentifier = token id
        contract = Web3.to_checksum_address(p["offerToken"])
        token_id = int(p["offerIdentifier"])
        return contract, token_id
        
    @classmethod
    def create_listing(
        cls,
        *,
        chain: str,                 # e.g. "base"
        protocol_address: str,      # Seaport 1.6
        parameters: Dict[str, Any], # Seaport OrderComponents
        signature: str              # EIP-712 signature over parameters
    ) -> Dict[str, Any]:
        """
        POST /orders/{chain}/{protocol}/listings
        Body: { parameters: <OrderComponents>, signature: "0x..." }
        Returns the created order (with order hash).
        """
        path = f"/orders/{chain}/{protocol_address}/listings"
        body = {"parameters": parameters, "signature": signature}
        return cls._post(path, json=body)