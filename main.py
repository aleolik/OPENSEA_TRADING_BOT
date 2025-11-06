'''
    Public version of Opensea Bot
'''

import os
import time
import random
import threading
import logging
import traceback
import json
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from web3 import Web3

# your existing helpers – adjust paths if needed
from web3_projects.opensea.web3_order_fulfillment import order_fullfillment
from web3_projects.opensea.web3_create_listing import create_listing
from web3_projects.opensea.sale_status import wait_until_sold
from web3_projects.opensea.opensea import Opensea


# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)

def _thread_excepthook(args):
    logging.error("Unhandled thread exception",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
threading.excepthook = _thread_excepthook


# ================= CONFIG =================
DEFAULT_COMMISSION_RATE = 0.01  # +1% over buy price
SALE_TIMEOUT_SINGLE = 60 * 60 * 24     # 24h if per-collection limit == 1
SALE_TIMEOUT_MULTI  = 60 * 60 * 2      # 2h  if per-collection limit > 1

# slow down watcher polls – you were getting 429
POLL_SECONDS = 30

# wipe positions on every boot
CLEAR_STATE_ON_BOOT = True




# Collections data
THE_WARPLETS_FARCASTER_BASE="the-warplets-farcaster"
DX_TERMINAL_BASE="dxterminal"
CANIDAEBASE_BASE="canidaebase"
PUNKISM_5_ABSTRACT="punkism-5"

BASE_CHAIN="base"
ABSTRACT_CHAIN="abstract"


''' TO RUN COLLECTION ADD IT TO COLLECTION_TO_LIMIT AND SET LIMIT of NFTs '''
COLLECTION_TO_LIMIT = {
    # PUNKISM_5_ABSTRACT:1
    THE_WARPLETS_FARCASTER_BASE: 2
    # "canidaebase": 3,
    # "dxterminal": 1,
}

COLLECTION_TO_CHAIN = {
    THE_WARPLETS_FARCASTER_BASE:BASE_CHAIN,
    PUNKISM_5_ABSTRACT:ABSTRACT_CHAIN
    
}

def get_rpc_for_chain(chain: str) -> str:
    if chain == "base":
        return os.environ["BASE_RPC_URL"]
    elif chain == "abstract":
        return os.environ["ABSTRACT_RPC_URL"]
    else:
        raise ValueError(f"Unknown chain: {chain}")

# ERC-1155 flags
COLLECTION_IS_ERC1155 = {
    "dxterminal": False,
    "canidaebase": False,
    "the-warplets-farcaster": False,
    "farworld-creatures": False,
}

# optional per-collection markup
COLLECTION_TO_COMMISSION_RATE: Dict[str, float] = {
    # "canidaebase": 0.012
}

# ================= PERSISTENCE =================
POSITIONS_FILE = Path("opensea_positions.json")
_POSITIONS_LOCK = threading.Lock()


def _load_all_positions() -> Dict[str, Any]:
    with _POSITIONS_LOCK:
        if not POSITIONS_FILE.exists():
            return {}
        try:
            return json.load(POSITIONS_FILE.open("r", encoding="utf-8"))
        except Exception:
            return {}


def _save_all_positions(data: Dict[str, Any]) -> None:
    with _POSITIONS_LOCK:
        POSITIONS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_positions_for_collection(slug: str) -> Dict[str, Any]:
    data = _load_all_positions()
    return data.get(slug, {"live": [], "overflow": []})


def _save_positions_for_collection(slug: str, live: List[Dict[str, Any]], overflow: List[Dict[str, Any]]) -> None:
    data = _load_all_positions()
    data[slug] = {
        "live": live,
        "overflow": overflow,
    }
    _save_all_positions(data)


# ================= small helpers =================
def _jitter(a=2, b=5):
    time.sleep(random.randint(a, b))


# minimal ABIs to confirm ownership
ERC721_OWNEROF_ABI = [{
    "constant": True,
    "inputs": [{"name": "tokenId", "type": "uint256"}],
    "name": "ownerOf",
    "outputs": [{"name": "", "type": "address"}],
    "type": "function"
}]
ERC1155_BAL_ABI = [{
    "constant": True,
    "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "type": "function"
}]


def _owns_asset_now(w3: Web3, nft_contract: str, token_id: int, seller: str, is_erc1155: bool) -> bool:
    caddr = Web3.to_checksum_address(nft_contract)
    saddr = Web3.to_checksum_address(seller)
    if is_erc1155:
        c = w3.eth.contract(address=caddr, abi=ERC1155_BAL_ABI)
        bal = int(c.functions.balanceOf(saddr, int(token_id)).call())
        return bal >= 1
    else:
        c = w3.eth.contract(address=caddr, abi=ERC721_OWNEROF_ABI)
        owner = c.functions.ownerOf(int(token_id)).call()
        return owner.lower() == saddr.lower()


def wait_until_owns_asset(
    w3: Web3,
    nft_contract: str,
    token_id: int,
    seller: str,
    is_erc1155: bool,
    timeout_sec: int = 180,
    poll_sec: int = 6,
) -> bool:
    end = time.time() + timeout_sec
    while time.time() < end:
        try:
            if _owns_asset_now(w3, nft_contract, token_id, seller, is_erc1155):
                return True
        except Exception:
            pass
        time.sleep(poll_sec)
    return False


# ================= “ephemeral” error detector =================
def is_ephemeral_order_error(reason: str) -> bool:
    """
    Returns True if this is the kind of error we get when the order
    was already filled/cancelled/invalid by the time we tried to buy.
    For these we want to restart *fast* (3-5s), not 3-5 mins.
    """
    if not reason:
        return False
    r = reason.lower()
    EPHEMERAL_SUBSTRINGS = [
        "order already filled",
        "orderalreadyfilled",
        "order is cancelled",
        "order is canceled",
        "orderiscancelled",
        "orderiscanceled",
        "seaport order validation failed",
        "does not have enough balance of order asset",
        "not eligible for fulfillment",
        "listing not found",
        "fulfillment not found",
        "invalid listing",
        "considerationnotmet",
        "consideration not met",
        "expired order",
        "order expired",
    ]
    return any(s in r for s in EPHEMERAL_SUBSTRINGS)


# ================= SAFE WAIT (handles 429) =================
def _safe_wait_until_sold_serialized(
    *,
    rpc_url: str,
    nft_contract: str,
    token_id: int,
    seller_address: str,
    is_erc1155: bool,
    poll_seconds: int,
    timeout_seconds: int,
    start_block_hint: Optional[int],
    rpc_lock: threading.Lock,
    max_429: int = 6,
) -> Tuple[bool, Optional[str], Optional[int], Optional[str]]:
    """
    Same as before but:
    - ALL RPC calls for this collection go through rpc_lock (so we don't DDoS 1 endpoint)
    - if we see 429 too many times -> we bail and let supervisor restart later
    """
    deadline = time.time() + timeout_seconds
    consecutive_429 = 0
    while True:
        remaining = int(deadline - time.time())
        if remaining <= 0:
            # timed out normally
            return False, None, None, None
        try:
            with rpc_lock:
                return wait_until_sold(
                    rpc_url=rpc_url,
                    nft_contract=nft_contract,
                    token_id=token_id,
                    seller_address=seller_address,
                    is_erc1155=is_erc1155,
                    poll_seconds=poll_seconds,
                    timeout_seconds=remaining,
                    start_block_hint=start_block_hint,
                )
        except Exception as e:
            msg = str(e)
            if "429" in msg or "too many requests" in msg.lower() or "rate limit" in msg.lower():
                consecutive_429 += 1
                logging.warning("[SAFE WAIT] RPC rate-limited (429). Sleeping 30s and retrying…")
                if consecutive_429 >= max_429:
                    raise RuntimeError("RPC unhealthy (429 x many)")
                time.sleep(30)
                continue
            raise


# ================= BUY + LIST (with retry) =================
def _try_create_listing_with_retries(
    *,
    rpc_url: str,
    api_key: str,
    seller_address: str,
    seller_private_key: str,
    chain_name: str,
    nft_contract: str,
    token_id: int,
    ask_price_eth: float,
    is_erc1155: bool,
    retries: int = 3,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    delay = 20
    for i in range(retries):
        try:
            resp = create_listing(
                rpc_url=rpc_url,
                api_key=api_key,
                seller_address=seller_address,
                seller_private_key=seller_private_key,
                chain_name=chain_name,
                nft_contract=nft_contract,
                token_id=token_id,
                ask_price_eth=ask_price_eth,
                duration_seconds=3 * 24 * 3600,
                is_erc1155=is_erc1155,
                auto_approve_conduit=True,
            )
            return True, resp
        except Exception as e:
            logging.error(f"create_listing attempt {i+1}/{retries} failed: {e}")
            time.sleep(delay)
            delay *= 2
    return False, None


def buy_cheapest_then_list(
    *,
    collection_slug: str,
    taker_address: str,
    private_key: str,
    rpc_url: str,
    chain_name: str,
    api_key: str,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    is_erc1155: bool = False,
) -> Tuple[str, int, float, bool]:
    """
    returns (nft_contract, token_id, ask_price_eth, listed_ok)
    listed_ok = False → we bought, but listing failed now; manager should keep it and retry later
    """
    listings = Opensea.get_cheapest_listings(collection_slug, limit=1)
    if not listings:
        raise RuntimeError(f"No listings for {collection_slug}")
    listing = listings[0]

    buy_price_eth, currency, _ = Opensea.parse_price_current(listing)
    logging.info(f"[{collection_slug}] Cheapest: {buy_price_eth} {currency}")

    ref = Opensea.build_listing_ref(listing)
    fd = Opensea.get_fulfillment_data(listing_ref=ref, address=taker_address)

    # --- BUY ---
    # (this is where you often see: “Seaport order validation failed: [order already filled]”)
    tx_hash, _ = order_fullfillment(
        fulfillment_data=fd,
        taker_address=taker_address,
        buyer_private_key=private_key,
        rpc_url=rpc_url,
        payment_token_address=None,
    )
    logging.info(f"[{collection_slug}] Bought tx: {tx_hash}")

    nft_contract, token_id = Opensea.extract_nft_from_fulfillment(fd)
    logging.info(f"[{collection_slug}] Bought NFT: {nft_contract} {token_id}")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not wait_until_owns_asset(w3, nft_contract, int(token_id), taker_address, is_erc1155, timeout_sec=180):
        logging.warning(f"[{collection_slug}] cannot confirm ownership immediately – will mark as unlisted")
        return nft_contract, int(token_id), buy_price_eth, False

    _jitter(2, 6)

    commission = COLLECTION_TO_COMMISSION_RATE.get(collection_slug, commission_rate)
    ask_price_eth = buy_price_eth * (1.0 + commission)

    ok, resp = _try_create_listing_with_retries(
        rpc_url=rpc_url,
        api_key=api_key,
        seller_address=taker_address,
        seller_private_key=private_key,
        chain_name=chain_name,
        nft_contract=nft_contract,
        token_id=int(token_id),
        ask_price_eth=ask_price_eth,
        is_erc1155=is_erc1155,
    )

    if ok:
        created_hash = (resp.get("order") or {}).get("hash") or resp.get("hash") or resp.get("order_hash")
        logging.info(f"[{collection_slug}] Listed. Order hash: {created_hash}")
        return nft_contract, int(token_id), ask_price_eth, True

    logging.warning(f"[{collection_slug}] Bought but NOT listed (will retry later)")
    return nft_contract, int(token_id), ask_price_eth, False


# ================= COLLECTION MANAGER =================
class CollectionManager:
    def __init__(
        self,
        *,
        collection_slug: str,
        limit: int,
        taker_address: str,
        private_key: str,
        rpc_url: str,
        chain_name: str,
        api_key: str,
        is_erc1155: bool = False,
    ):
        self.collection = collection_slug
        self.limit = int(limit)
        self.addr = taker_address
        self.pk = private_key
        self.rpc = rpc_url
        self.chain = chain_name
        self.api_key = api_key
        self.is_erc1155 = is_erc1155

        self.lock = threading.Lock()
        self.live_positions: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.overflow_positions: Dict[Tuple[str, int], Dict[str, Any]] = {}

        self.stop_flag = False

        # per-collection rpc lock
        self.rpc_lock = threading.Lock()

        # for supervisor
        self.restart_event = threading.Event()
        self.last_restart_reason: Optional[str] = None

    def request_restart(self, reason: str):
        self.last_restart_reason = reason
        self.restart_event.set()

    def _timeout_for_this_collection(self) -> int:
        return SALE_TIMEOUT_MULTI if self.limit > 1 else SALE_TIMEOUT_SINGLE

    def _active_count(self) -> int:
        with self.lock:
            return len(self.live_positions) + len(self.overflow_positions)

    def _persist(self):
        with self.lock:
            live = [
                {
                    "contract": c,
                    "token_id": t,
                    "ask_eth": meta.get("ask_eth"),
                    "ts": meta.get("ts"),
                    "listed_ok": meta.get("listed_ok", True),
                }
                for (c, t), meta in self.live_positions.items()
            ]
            overflow = [
                {
                    "contract": c,
                    "token_id": t,
                    "ask_eth": meta.get("ask_eth"),
                    "ts": meta.get("ts"),
                    "listed_ok": meta.get("listed_ok", True),
                }
                for (c, t), meta in self.overflow_positions.items()
            ]
        _save_positions_for_collection(self.collection, live, overflow)

    def _load_and_spawn_existing(self):
        data = _load_positions_for_collection(self.collection)
        live_list = data.get("live", [])
        overflow_list = data.get("overflow", [])

        for item in live_list:
            c = item["contract"]
            t = int(item["token_id"])
            meta = {
                "ask_eth": item.get("ask_eth"),
                "ts": item.get("ts"),
                "listed_ok": item.get("listed_ok", True),
            }
            self.live_positions[(c, t)] = meta
            self._spawn_watcher(c, t, is_overflow=False)

        for item in overflow_list:
            c = item["contract"]
            t = int(item["token_id"])
            meta = {
                "ask_eth": item.get("ask_eth"),
                "ts": item.get("ts"),
                "listed_ok": item.get("listed_ok", True),
            }
            self.overflow_positions[(c, t)] = meta
            self._spawn_watcher(c, t, is_overflow=True)

        logging.info(
            f"[{self.collection}] Restored {len(live_list)} live and {len(overflow_list)} overflow from storage."
        )

    def _spawn_watcher(self, nft_contract: str, token_id: int, *, is_overflow: bool):
        def _runner():
            while not self.stop_flag:
                try:
                    self._watch_and_manage_position(nft_contract, token_id, is_overflow)
                    break
                except Exception as e:
                    logging.error(f"[{self.collection}] watcher error {nft_contract} #{token_id}: {e}")
                    logging.debug("".join(traceback.format_exc()))
                    # pass reason upward
                    self.request_restart(f"watcher crashed: {e}")
                    break
        t = threading.Thread(
            target=_runner,
            name=f"watch-{self.collection}-{token_id}",
            daemon=True,
        )
        t.start()

    def _watch_and_manage_position(self, nft_contract: str, token_id: int, is_overflow: bool):
        timeout_s = self._timeout_for_this_collection()
        logging.info(f"[{self.collection}] Watching {nft_contract} #{token_id} (timeout {timeout_s//3600}h, overflow={is_overflow})")

        # get block hint but through lock to avoid 429
        try:
            with self.rpc_lock:
                bh = Web3(Web3.HTTPProvider(self.rpc)).eth.block_number
            start_block_hint = max(0, bh - 50_000)
        except Exception:
            start_block_hint = None

        sold, txh, blk, new_owner = _safe_wait_until_sold_serialized(
            rpc_url=self.rpc,
            nft_contract=nft_contract,
            token_id=token_id,
            seller_address=self.addr,
            is_erc1155=self.is_erc1155,
            poll_seconds=POLL_SECONDS,
            timeout_seconds=timeout_s,
            start_block_hint=start_block_hint,
            rpc_lock=self.rpc_lock,
        )

        if sold:
            logging.info(f"[{self.collection}] SOLD {nft_contract} #{token_id} tx={txh} blk={blk} new_owner={new_owner}")
            with self.lock:
                self.live_positions.pop((nft_contract, token_id), None)
                self.overflow_positions.pop((nft_contract, token_id), None)
            self._persist()

            if self._active_count() == 0 and not self.stop_flag:
                try:
                    self._open_new_live_position()
                except Exception as e:
                    self.request_restart(f"Refill after sale failed: {e}")
            return

        # ========== TIMEOUT ==========
        logging.info(f"[{self.collection}] TIMEOUT (unsold) {nft_contract} #{token_id}. Keeping it and maybe open new one.")
        with self.lock:
            meta = self.live_positions.pop((nft_contract, token_id), None) or {}
            self.overflow_positions[(nft_contract, token_id)] = meta
        self._persist()

        # if we still have room – open another one
        if (self._active_count() < self.limit) and not self.stop_flag:
            try:
                self._open_new_live_position()
            except Exception as e:
                self.request_restart(f"Open-after-timeout failed: {e}")
                return

        # now continue checking once per hour BUT serialized
        while not self.stop_flag:
            try:
                with self.rpc_lock:
                    bh2 = Web3(Web3.HTTPProvider(self.rpc)).eth.block_number
                start_block_hint2 = max(0, bh2 - 50_000)
            except Exception:
                start_block_hint2 = None

            sold2, txh2, blk2, new_owner2 = _safe_wait_until_sold_serialized(
                rpc_url=self.rpc,
                nft_contract=nft_contract,
                token_id=token_id,
                seller_address=self.addr,
                is_erc1155=self.is_erc1155,
                poll_seconds=POLL_SECONDS,
                timeout_seconds=60 * 60,
                start_block_hint=start_block_hint2,
                rpc_lock=self.rpc_lock,
            )
            if sold2:
                logging.info(f"[{self.collection}] SOLD after timeout {nft_contract} #{token_id}")
                with self.lock:
                    self.live_positions.pop((nft_contract, token_id), None)
                    self.overflow_positions.pop((nft_contract, token_id), None)
                self._persist()

                if self._active_count() == 0 and not self.stop_flag:
                    try:
                        self._open_new_live_position()
                    except Exception as e:
                        self.request_restart(f"Refill after late sale failed: {e}")
                break

    def _open_new_live_position(self):
        try:
            nft_contract, token_id, ask_price, listed_ok = buy_cheapest_then_list(
                collection_slug=self.collection,
                taker_address=self.addr,
                private_key=self.pk,
                rpc_url=self.rpc,
                chain_name=self.chain,
                api_key=self.api_key,
                commission_rate=DEFAULT_COMMISSION_RATE,
                is_erc1155=self.is_erc1155,
            )
        except Exception as e:
            # this is where buy can fail because order was already filled
            reason = str(e)
            logging.error(f"[{self.collection}] buy+list failed: {reason}")
            # tell supervisor to restart, with exact reason
            self.request_restart(f"buy failed: {reason}")
            # and re-raise so runner finishes
            raise

        with self.lock:
            self.live_positions[(nft_contract, token_id)] = {
                "ask_eth": ask_price,
                "ts": time.time(),
                "listed_ok": listed_ok,
            }
        self._persist()
        self._spawn_watcher(nft_contract, token_id, is_overflow=False)

    def run_once_boot(self):
        logging.info(f"[{self.collection}] Boot: limit={self.limit}. restore…")
        self._load_and_spawn_existing()

        current = self._active_count()
        logging.info(f"[{self.collection}] Active after restore: {current}, limit={self.limit}")

        # do NOT buy if we already have enough positions restored
        if current < self.limit:
            self._open_new_live_position()
        else:
            logging.info(f"[{self.collection}] At/above limit on boot – not buying")

    def start(self):
        self.run_once_boot()

    def stop(self):
        self.stop_flag = True


# ================= SUPERVISOR =================
def supervise_collection(factory, name: str):
    """
    Creates a manager, runs it.
    If it dies / requests restart:
      - if error looks EPHEMERAL → restart in 3-5s
      - else → restart in 3-5min
    """
    while True:
        done_event = threading.Event()
        reason_holder = {"reason": ""}

        def _runner():
            try:
                mgr = factory()
                mgr.run_once_boot()

                # main manager loop – wake up every 30s and see if it wants restart
                while not mgr.stop_flag:
                    if mgr.restart_event.wait(timeout=30):
                        reason = getattr(mgr, "last_restart_reason", "no reason")
                        raise RuntimeError(reason)
            except Exception as e:
                reason_holder["reason"] = str(e)
                logging.error(f"[{name}] Manager crashed: {e}")
                logging.debug("".join(traceback.format_exc()))
            finally:
                done_event.set()

        threading.Thread(target=_runner, name=f"mgr-{name}", daemon=True).start()

        # wait until manager finishes
        done_event.wait()

        # decide delay based on reason
        reason = reason_holder.get("reason", "") or ""
        if is_ephemeral_order_error(reason):
            delay = random.randint(3, 5)
            logging.warning(f"[{name}] Ephemeral order error → quick restart in {delay}s …")
        else:
            delay = random.randint(60 * 3, 60 * 5)
            logging.warning(f"[{name}] Restarting manager in {delay//60} min …")
        time.sleep(delay)


# ================= MAIN =================
def main():
    address = "YOUR_ADDRESS"
    private_key = "YOUR_PRIVATE_KEY"


    api_key = os.environ["API_KEY"]

    # wipe old state on full process start
    if CLEAR_STATE_ON_BOOT:
        _save_all_positions({})
        logging.info("Cleared opensea_positions.json on boot")

    for slug, cap in COLLECTION_TO_LIMIT.items():
        chain = COLLECTION_TO_CHAIN.get(slug,"")
        if chain == "": raise ValueError("Chain for collection is not specified")
        rpc = get_rpc_for_chain(chain=chain)

        def make_factory(s=slug, c=cap,chain_name=chain,rpc_url=rpc):
            def _factory():
                return CollectionManager(
                    collection_slug=s,
                    limit=c,
                    taker_address=address,
                    private_key=private_key,
                    rpc_url=rpc_url,
                    chain_name=chain_name,
                    api_key=api_key,
                    is_erc1155=COLLECTION_IS_ERC1155.get(s, False),
                )
            return _factory

        threading.Thread(
            target=supervise_collection,
            args=(make_factory(), slug),
            name=f"sup-{slug}",
            daemon=True,
        ).start()
        _jitter(2, 6)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("Stopping…")


if __name__ == "__main__":
    main()
