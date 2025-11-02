from __future__ import annotations
from typing import Optional, Tuple, Literal, List
from web3 import Web3

# Minimal ABIs
ERC721_OWNEROF_ABI = [{"constant": True,"inputs":[{"name":"tokenId","type":"uint256"}],
                       "name":"ownerOf","outputs":[{"name":"","type":"address"}],"type":"function"}]
ERC1155_BALANCEOF_ABI = [{"constant": True,"inputs":[{"name":"account","type":"address"},
                                                    {"name":"id","type":"uint256"}],
                          "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"}]

# Event signatures
ERC721_TRANSFER_SIG = Web3.keccak(text="Transfer(address,address,uint256)").hex()
ERC1155_TRANSFER_SINGLE_SIG = Web3.keccak(text="TransferSingle(address,address,address,uint256,uint256)").hex()
ERC1155_TRANSFER_BATCH_SIG  = Web3.keccak(text="TransferBatch(address,address,address,uint256[],uint256[])").hex()

def _topic_address(addr: str) -> str:
    return "0x" + "0"*24 + Web3.to_checksum_address(addr)[2:]

def _topic_uint256(n: int) -> str:
    return "0x" + n.to_bytes(32, byteorder="big").hex()

def owner_erc721(w3: Web3, nft_contract: str, token_id: int) -> str:
    c = w3.eth.contract(address=Web3.to_checksum_address(nft_contract), abi=ERC721_OWNEROF_ABI)
    return c.functions.ownerOf(int(token_id)).call()

def balance_erc1155(w3: Web3, nft_contract: str, owner: str, token_id: int) -> int:
    c = w3.eth.contract(address=Web3.to_checksum_address(nft_contract), abi=ERC1155_BALANCEOF_ABI)
    return int(c.functions.balanceOf(Web3.to_checksum_address(owner), int(token_id)).call())

def latest_transfer_tx_erc721(
    w3: Web3,
    nft_contract: str,
    token_id: int,
    start_block: Optional[int] = None,
    end_block: Optional[int] = None,
) -> Optional[Tuple[str, int]]:
    address = Web3.to_checksum_address(nft_contract)
    latest = w3.eth.block_number
    # Clamp fromBlock to a sane window to avoid provider limits
    from_block = start_block if start_block is not None else (latest - 50_000)
    if from_block < 0: from_block = 0
    if isinstance(end_block, int):
        to_block = min(end_block, latest)
    else:
        to_block = latest

    topic0 = ERC721_TRANSFER_SIG
    topic_token = _topic_uint256(int(token_id))  # 32-byte padded, 0x-prefixed

    # Try strict filter: topic0 + tokenId in topic[3]
    try:
        logs = w3.eth.get_logs({
            "address": address,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [topic0, None, None, topic_token],
        })
        if logs:
            log = logs[-1]
            return (log["transactionHash"].hex(), log["blockNumber"])
    except Exception:
        pass

    # Fallback 1: only topic0, then post-filter by topic[3]
    try:
        logs = w3.eth.get_logs({
            "address": address,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [topic0],
        })
        toks = topic_token.lower()
        filtered = [l for l in logs if len(l["topics"]) >= 4 and l["topics"][3].hex().lower() == toks]
        if filtered:
            log = filtered[-1]
            return (log["transactionHash"].hex(), log["blockNumber"])
    except Exception:
        pass

    # Fallback 2: scan a narrower recent window (e.g., last 5k blocks)
    try:
        recent_from = max(0, latest - 5_000)
        logs = w3.eth.get_logs({
            "address": address,
            "fromBlock": recent_from,
            "toBlock": latest,
            "topics": [topic0],
        })
        toks = topic_token.lower()
        filtered = [l for l in logs if len(l["topics"]) >= 4 and l["topics"][3].hex().lower() == toks]
        if filtered:
            log = filtered[-1]
            return (log["transactionHash"].hex(), log["blockNumber"])
    except Exception:
        pass

    return None



ERC165_SUPPORTS_INTERFACE_ABI = [{
    "constant": True,
    "inputs": [{"name": "interfaceId", "type": "bytes4"}],
    "name": "supportsInterface",
    "outputs": [{"name": "", "type": "bool"}],
    "type": "function",
}]

ERC721_INTERFACE_ID  = "0x80ac58cd"
ERC1155_INTERFACE_ID = "0xd9b67a26"


def _detect_standard(w3: Web3, nft_contract: str) -> str:
    """
    Detects whether the contract is ERC-721 or ERC-1155 using ERC-165.
    Returns: 'erc721' or 'erc1155'.
    """
    c = w3.eth.contract(
        address=Web3.to_checksum_address(nft_contract),
        abi=ERC165_SUPPORTS_INTERFACE_ABI
    )

    try:
        if c.functions.supportsInterface(Web3.to_bytes(hexstr=ERC721_INTERFACE_ID)).call():
            return "erc721"
    except Exception:
        pass

    try:
        if c.functions.supportsInterface(Web3.to_bytes(hexstr=ERC1155_INTERFACE_ID)).call():
            return "erc1155"
    except Exception:
        pass

    # fallback heuristic: ownerOf present → ERC-721
    try:
        _ = owner_erc721(w3, nft_contract, 1)
        return "erc721"
    except Exception:
        return "erc1155"
    
def latest_transfer_tx_erc1155(
    w3: Web3,
    nft_contract: str,
    token_id: int,
    start_block: Optional[int] = None,
    end_block: Optional[int] = None,
) -> Optional[Tuple[str, int]]:
    """
    Returns (tx_hash, block_number) of the latest ERC-1155 TransferSingle/Batch involving token_id.
    """
    address = Web3.to_checksum_address(nft_contract)
    from_block = start_block or max(0, w3.eth.block_number - 50_000)
    to_block = end_block or "latest"

    # Try TransferSingle first (id is indexed in topic4)
    try:
        logs = w3.eth.get_logs({
            "address": address,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [ERC1155_TRANSFER_SINGLE_SIG, None, None, _topic_uint256(int(token_id))],
        })
    except Exception:
        logs = []
    # Batch events: ids are in data, not topics — need to scan
    try:
        logs_b = w3.eth.get_logs({
            "address": address,
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [ERC1155_TRANSFER_BATCH_SIG],
        })
    except Exception:
        logs_b = []

    cand: List[Tuple[str,int]] = [(l["transactionHash"].hex(), l["blockNumber"]) for l in logs]
    # Heuristic: include all batches (you can decode to filter strictly if needed)
    cand += [(l["transactionHash"].hex(), l["blockNumber"]) for l in logs_b]

    if not cand: return None
    cand.sort(key=lambda x: x[1])
    return cand[-1]

def check_nft_sold(
    *,
    rpc_url: str,
    nft_contract: str,
    token_id: int | str,
    seller_address: str,
    is_erc1155: bool | None = None,
    start_block_hint: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    token_id = int(token_id)
    seller = Web3.to_checksum_address(seller_address)

    # Auto-detect if not specified
    std = ("erc1155" if is_erc1155 else "erc721") if is_erc1155 is not None else _detect_standard(w3, nft_contract)

    if std == "erc721":
        try:
            current_owner = owner_erc721(w3, nft_contract, token_id)
        except Exception as e:
            # As a last resort, assume not sold if ownerOf reverts
            raise RuntimeError(f"ownerOf failed: {e}")

        if current_owner.lower() != seller.lower():
            # Sold; try to fetch last transfer, but don’t fail if logs throw
            tx = None
            try:
                tx = latest_transfer_tx_erc721(w3, nft_contract, token_id, start_block=start_block_hint)
            except Exception:
                tx = None
            txh, blk = tx if tx else (None, None)
            return True, txh, blk, current_owner
        else:
            return False, None, None, current_owner

    else:
        # ERC-1155 path
        try:
            bal = balance_erc1155(w3, nft_contract, seller, token_id)
        except Exception as e:
            # Treat as not sold if balanceOf fails; or raise if you prefer
            raise RuntimeError(f"balanceOf failed: {e}")

        if bal == 0:
            tx = None
            try:
                tx = latest_transfer_tx_erc1155(w3, nft_contract, token_id, start_block=start_block_hint)
            except Exception:
                tx = None
            txh, blk = tx if tx else (None, None)
            return True, txh, blk, None
        else:
            return False, None, None, None
        
def wait_until_sold(
    *,
    rpc_url: str,
    nft_contract: str,
    token_id: int | str,
    seller_address: str,
    is_erc1155: bool = False,
    poll_seconds: int = 6,
    timeout_seconds: int = 900,
    start_block_hint: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """
    Polls until sale detected or timeout.
    Returns same tuple as check_nft_sold.
    """
    import time
    deadline = time.time() + timeout_seconds
    while True:
        sold, txh, blk, new_owner = check_nft_sold(
            rpc_url=rpc_url,
            nft_contract=nft_contract,
            token_id=token_id,
            seller_address=seller_address,
            is_erc1155=is_erc1155,
            start_block_hint=start_block_hint,
        )
        if sold:
            return sold, txh, blk, new_owner
        if time.time() > deadline:
            return False, None, None, new_owner
        time.sleep(poll_seconds)