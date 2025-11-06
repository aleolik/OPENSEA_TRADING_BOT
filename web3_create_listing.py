# create_listing.py
import time, random
from data import OPENSEA_FEE_RECIPIENT
from typing import Dict, Any, List, Optional, Tuple, Literal

import requests
from web3 import Web3
from eth_account.messages import encode_typed_data

from data import (
    SEAPORT_16,SEAPORT_COUNTER_ABI, ERC721_ABI, ERC1155_ABI
)

# ---------- small helpers ----------
def _now() -> int: return int(time.time())
def _rand_salt(bits: int = 256) -> int: return random.getrandbits(bits)

def _get_seaport_counter(w3: Web3, offerer: str) -> int:
    c = w3.eth.contract(address=Web3.to_checksum_address(SEAPORT_16), abi=SEAPORT_COUNTER_ABI)
    return int(c.functions.getCounter(Web3.to_checksum_address(offerer)).call())

def _eip712_for_order(chain_id: int, order_components: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name":"name","type":"string"},
                {"name":"version","type":"string"},
                {"name":"chainId","type":"uint256"},
                {"name":"verifyingContract","type":"address"},
            ],
            "OfferItem": [
                {"name":"itemType","type":"uint8"},
                {"name":"token","type":"address"},
                {"name":"identifierOrCriteria","type":"uint256"},
                {"name":"startAmount","type":"uint256"},
                {"name":"endAmount","type":"uint256"},
            ],
            "ConsiderationItem": [
                {"name":"itemType","type":"uint8"},
                {"name":"token","type":"address"},
                {"name":"identifierOrCriteria","type":"uint256"},
                {"name":"startAmount","type":"uint256"},
                {"name":"endAmount","type":"uint256"},
                {"name":"recipient","type":"address"},
            ],
            "OrderComponents": [
                {"name":"offerer","type":"address"},
                {"name":"zone","type":"address"},
                {"name":"offer","type":"OfferItem[]"},
                {"name":"consideration","type":"ConsiderationItem[]"},
                {"name":"orderType","type":"uint8"},
                {"name":"startTime","type":"uint256"},
                {"name":"endTime","type":"uint256"},
                {"name":"zoneHash","type":"bytes32"},
                {"name":"salt","type":"uint256"},
                {"name":"conduitKey","type":"bytes32"},
                {"name":"counter","type":"uint256"},
            ],
        },
        "primaryType": "OrderComponents",
        "domain": {
            "name": "Seaport",
            "version": "1.6",
            "chainId": chain_id,
            "verifyingContract": Web3.to_checksum_address(SEAPORT_16),
        },
        "message": order_components,
    }

def _sign_order_components(w3: Web3, order_components: Dict[str, Any], private_key: str) -> str:
    typed = _eip712_for_order(w3.eth.chain_id, order_components)
    msg = encode_typed_data(full_message=typed)
    signed = w3.eth.account.sign_message(msg, private_key=private_key)
    return signed.signature.hex()

def _ensure_conduit_approval(
    w3: Web3,
    owner: str,
    nft_contract: str,
    is_erc1155: bool,
    private_key: str,
    chain:str = "base",
) -> Optional[str]:
    from data import get_conduit_address

    owner = Web3.to_checksum_address(owner)
    operator = Web3.to_checksum_address(get_conduit_address(chain))
    nft = w3.eth.contract(
        address=Web3.to_checksum_address(nft_contract),
        abi=ERC1155_ABI if is_erc1155 else ERC721_ABI,
    )
    if nft.functions.isApprovedForAll(owner, operator).call():
        return None

    nonce = w3.eth.get_transaction_count(owner)
    tx = nft.functions.setApprovalForAll(operator, True).build_transaction({
        "from": owner, "nonce": nonce, "chainId": w3.eth.chain_id,
    })
    try:
        base = w3.eth.get_block("latest").baseFeePerGas
        prio = w3.eth.max_priority_fee
        tx["maxPriorityFeePerGas"] = prio
        tx["maxFeePerGas"] = base * 2 + prio
    except Exception:
        tx["gasPrice"] = w3.eth.gas_price
    tx["gas"] = w3.eth.estimate_gas({**tx, "from": owner})
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)
    return w3.eth.send_raw_transaction(signed.rawTransaction).hex()

def _build_order_components(
    *,
    chain_id: int,
    chain:str,
    offerer: str,
    nft_contract: str,
    token_id: int | str,
    price_wei: int,
    duration_seconds: int,
    is_erc1155: bool,
    erc1155_amount: int,
    pay_with_erc20: bool,
    erc20_token: Optional[str],
    extra_consideration: Optional[List[Tuple[int,str]]],
    opensea_fee_bps: int = 100,   # <-- NEW: default 1% (100 bps)
) -> Dict[str, Any]:
    from data import get_conduit_key

    offerer = Web3.to_checksum_address(offerer)
    start = _now()
    end = start + int(duration_seconds)
    salt = _rand_salt()

    item_type_nft = 3 if is_erc1155 else 2
    item_type_pay = 1 if pay_with_erc20 else 0
    pay_token_addr = (Web3.to_checksum_address(erc20_token) if pay_with_erc20
                      else "0x0000000000000000000000000000000000000000")

    offer_item = {
        "itemType": item_type_nft,
        "token": Web3.to_checksum_address(nft_contract),
        "identifierOrCriteria": int(token_id),
        "startAmount": int(erc1155_amount if is_erc1155 else 1),
        "endAmount": int(erc1155_amount if is_erc1155 else 1),
    }

    # --- fees split ---
    os_fee = (price_wei * opensea_fee_bps) // 10_000
    extra_sum = 0
    extra_items: List[Dict[str, Any]] = []
    if extra_consideration:
        for amt, rcpt in extra_consideration:
            extra_sum += int(amt)
            extra_items.append({
                "itemType": item_type_pay,
                "token": pay_token_addr,
                "identifierOrCriteria": 0,
                "startAmount": int(amt),
                "endAmount": int(amt),
                "recipient": Web3.to_checksum_address(rcpt),
            })

    seller_amount = int(price_wei) - os_fee - extra_sum
    if seller_amount <= 0:
        raise ValueError("Seller amount <= 0; decrease fees or increase price.")

    consideration: List[Dict[str, Any]] = [
        # Seller gets net after fees
        {
            "itemType": item_type_pay,
            "token": pay_token_addr,
            "identifierOrCriteria": 0,
            "startAmount": seller_amount,
            "endAmount": seller_amount,
            "recipient": offerer,
        },
        # OpenSea fee (100 bps by default)
        {
            "itemType": item_type_pay,
            "token": pay_token_addr,
            "identifierOrCriteria": 0,
            "startAmount": os_fee,
            "endAmount": os_fee,
            "recipient": Web3.to_checksum_address(OPENSEA_FEE_RECIPIENT),
        },
        # Any extra fee recipients (e.g., creator earnings)
        *extra_items,
    ]

    return {
        "offerer": offerer,
        "zone": "0x0000000000000000000000000000000000000000",
        "offer": [offer_item],
        "consideration": consideration,
        "orderType": 0,
        "startTime": start,
        "endTime": end,
        "zoneHash": "0x" + "0"*64,
        "salt": int(salt),
        "conduitKey": get_conduit_key(chain),
        "counter": 0,
    }

def _opensea_post_listing(api_key: str, chain: str, parameters: Dict[str, Any], signature: str) -> Dict[str, Any]:
    from data import SEAPORT_16  # 1.6 verifying contract

    PROTOCOL_SLUG = "seaport"  # <-- path must use the slug, not the address
    url = f"https://api.opensea.io/api/v2/orders/{chain}/{PROTOCOL_SLUG}/listings"

    headers = {
        "x-api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json",
    }

    body = {
        "protocol_address": SEAPORT_16,  # <-- address goes in the body
        "parameters": parameters,
        "signature": signature,
    }

    r = requests.post(url, headers=headers, json=body, timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
    return r.json()

# ---------------- PUBLIC API ----------------
def create_listing(
    *,
    rpc_url: str,
    api_key: str,
    seller_address: str,
    seller_private_key: str,
    chain_name: Literal["ethereum","base","polygon","arbitrum","optimism","zora","abstract"] = "base",
    nft_contract: str,
    token_id: int | str,
    ask_price_eth: Optional[float] = None,
    ask_price_wei: Optional[int] = None,
    duration_seconds: int = 72*3600,
    is_erc1155: bool = False,
    erc1155_amount: int = 1,
    pay_with_erc20: bool = False,
    erc20_token: Optional[str] = None,
    extra_consideration: Optional[List[Tuple[int,str]]] = None,
    auto_approve_conduit: bool = True,
) -> Dict[str, Any]:
    if ask_price_wei is None and ask_price_eth is None:
        raise ValueError("Provide ask_price_wei or ask_price_eth.")
    if ask_price_wei is None:
        ask_price_wei = int(ask_price_eth * 10**18)

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    seller = Web3.to_checksum_address(seller_address)

    if auto_approve_conduit:
        _ = _ensure_conduit_approval(w3, seller, nft_contract, is_erc1155, seller_private_key, chain=chain_name)  # Pass chain

    params = _build_order_components(
        chain_id=w3.eth.chain_id,
        chain=chain_name,  # Pass chain
        offerer=seller,
        nft_contract=nft_contract,
        token_id=token_id,
        price_wei=ask_price_wei,
        duration_seconds=duration_seconds,
        is_erc1155=is_erc1155,
        erc1155_amount=erc1155_amount,
        pay_with_erc20=pay_with_erc20,
        erc20_token=erc20_token,
        extra_consideration=extra_consideration,
    )
    params["counter"] = _get_seaport_counter(w3, seller)

    sig = _sign_order_components(w3, params, seller_private_key)
    return _opensea_post_listing(api_key, chain_name, params, sig)