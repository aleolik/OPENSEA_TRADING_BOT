# order_fulfillment.py
from typing import Optional

from web3 import Web3

from data import SEAPORT_16, SEAPORT_BASIC_ABI

# ---- (optional) ERC-20 allowance if you ever need it for token-based buys) ----
def _erc20_ensure_allowance(w3: Web3, owner: str, token: str, spender: str, amount_wei: int, pk: str):
    ERC20_ABI = [
        {"constant": True,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
         "name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
        {"constant": False,"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
         "name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"},
    ]
    token_c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    owner = Web3.to_checksum_address(owner)
    spender = Web3.to_checksum_address(spender)
    current = token_c.functions.allowance(owner, spender).call()
    if current >= amount_wei:
        return None
    nonce = w3.eth.get_transaction_count(owner)
    tx = token_c.functions.approve(spender, amount_wei).build_transaction({
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
    signed = w3.eth.account.sign_transaction(tx, private_key=pk)
    return w3.eth.send_raw_transaction(signed.rawTransaction).hex()

def _encode_basic_order_from_input_data(w3: Web3, to_addr: str, fn_name: str, input_data: dict) -> str:
    """
    Encode Seaport basic order from OpenSea typed input_data (fulfillment_data.transaction.input_data).
    Accepts {"parameters": {...}} or just {...}.
    """
    if not isinstance(input_data, dict):
        raise RuntimeError("input_data must be a dict.")
    p = input_data.get("parameters", input_data)

    def A(x): return Web3.to_checksum_address(x) if isinstance(x, str) else x
    def U(x): return int(x)
    def B32(x):
        s = x or "0x" + "00"*32
        if not s.startswith("0x"): s = "0x" + s
        return bytes.fromhex(s[2:].zfill(64))
    def BYTES(x):
        s = x
        if isinstance(s, str) and s.startswith("0x"): return bytes.fromhex(s[2:])
        if isinstance(s, (bytes, bytearray)): return bytes(s)
        raise RuntimeError("signature must be hex or bytes")

    additional = [{"amount": U(r["amount"]), "recipient": A(r["recipient"])}
                  for r in (p.get("additionalRecipients") or [])]

    params_tuple = (
        A(p["considerationToken"]),
        U(p["considerationIdentifier"]),
        U(p["considerationAmount"]),
        A(p["offerer"]),
        A(p["zone"]),
        A(p["offerToken"]),
        U(p["offerIdentifier"]),
        U(p["offerAmount"]),
        int(p["basicOrderType"]),
        U(p["startTime"]),
        U(p["endTime"]),
        B32(p["zoneHash"]),
        U(p["salt"]),
        B32(p["offererConduitKey"]),
        B32(p["fulfillerConduitKey"]),
        U(p["totalOriginalAdditionalRecipients"]),
        additional,
        BYTES(p["signature"]),
    )

    seaport = w3.eth.contract(address=Web3.to_checksum_address(to_addr), abi=SEAPORT_BASIC_ABI)
    if "(" in fn_name:  # strip "(parameters)" if present
        fn_name = fn_name.split("(")[0]
    if fn_name not in ("fulfillBasicOrder_efficient_6GL6yc", "fulfillBasicOrder"):
        raise RuntimeError(f"Unsupported function: {fn_name}")

    data_hex = seaport.encodeABI(fn_name=fn_name, args=[params_tuple])
    if not data_hex or not data_hex.startswith("0x"):
        raise RuntimeError("Failed to encode Seaport calldata.")
    return data_hex

# ---------------- PUBLIC API ----------------
def order_fullfillment(  # (kept your spelling)
    *,
    fulfillment_data: dict,        # body from POST /listings/fulfillment_data
    taker_address: str,
    buyer_private_key: str,
    rpc_url: str,
    payment_token_address: Optional[str] = None,  # only if ERC-20 payment & value==0
    price_value: Optional[float] = None,          # human units, for ERC-20 allowance calc
    price_decimals: Optional[int] = None,
    spender_hint: Optional[str] = None,
    wait_for_receipt: bool = True,
):
    """
    Submit a Seaport tx from OpenSea fulfillment_data (basic order).
    If raw calldata absent, ABI-encodes from typed parameters.
    """
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    from_addr = Web3.to_checksum_address(taker_address)

    fd = fulfillment_data.get("fulfillment_data") or {}
    txf = fd.get("transaction") or {}
    if not txf:
        raise RuntimeError(f"Missing fulfillment_data.transaction; keys: {list(fd.keys())}")

    to_addr = txf.get("to") or txf.get("to_address") or SEAPORT_16
    native_value = int(txf.get("value") or 0)

    # optional ERC-20 allowance path
    spender = spender_hint or fd.get("protocol_address") or fd.get("seaport_address") or SEAPORT_16
    if native_value == 0 and payment_token_address and price_value is not None:
        dec = 18 if price_decimals is None else int(price_decimals)
        amount_wei = int(price_value * (10 ** dec))
        _erc20_ensure_allowance(w3, from_addr, payment_token_address, spender, amount_wei, buyer_private_key)

    # try raw hex first
    data_field = None
    for k in ("data", "input", "input_data", "calldata"):
        v = txf.get(k)
        if isinstance(v, str):
            s = v.strip()
            if not s.startswith("0x"):
                try:
                    bytes.fromhex(s)
                    s = "0x" + s
                except Exception:
                    s = ""
            if s.startswith("0x") and len(s) > 2:
                data_field = s
                break

    # otherwise encode from typed params
    if not data_field and isinstance(txf.get("input_data"), dict):
        fn = (txf.get("function") or "fulfillBasicOrder_efficient_6GL6yc")
        data_field = _encode_basic_order_from_input_data(w3, to_addr, fn, txf["input_data"])

    if not data_field:
        raise RuntimeError(f"Could not derive calldata; tx keys: {list(txf.keys())}")

    # build/send
    built = {
        "from": from_addr,
        "to": Web3.to_checksum_address(to_addr),
        "data": data_field,
        "value": native_value,
        "nonce": w3.eth.get_transaction_count(from_addr),
        "chainId": txf.get("chainId") or w3.eth.chain_id,
    }

    # gas
    try:
        base = w3.eth.get_block("latest").baseFeePerGas
        prio = w3.eth.max_priority_fee
        built.setdefault("maxPriorityFeePerGas", prio)
        built.setdefault("maxFeePerGas", base * 2 + prio)
    except Exception:
        built.setdefault("gasPrice", w3.eth.gas_price)

    # estimate
    try:
        built["gas"] = int(txf.get("gas") or 0) or w3.eth.estimate_gas(built)
    except Exception:
        built["gas"] = 350_000

    signed = w3.eth.account.sign_transaction(built, private_key=buyer_private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction).hex()

    if wait_for_receipt:
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if rcpt.status != 1:
            raise RuntimeError(f"Tx mined but failed: {tx_hash}")
        return tx_hash, rcpt
    return tx_hash, None

