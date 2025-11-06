# Shared Seaport/OpenSea constants & ABIs

# Seaport 1.6 canonical (cross-chain) address
from web3 import Web3


SEAPORT_16 = "0x0000000000000068F116a894984e2DB1123eB395"

OPENSEA_CONDUIT_PER_CHAIN = {
    "base": {
        "address": "0x0000a26b00c1F0DF003000390027140000fAa719",
        "key": "0x0000007b02230091a7ed01230072f7006a004d60a8d4e71d599b8104250f0000"
    },
    "abstract": {
        "address": "0x0000a26b00c1F0DF003000390027140000fAa719",  # Likely same address
        "key": "0x61159fefdfada89302ed55f8b9e89e2d67d8258712b3a3f89aa88525877f1d5e"  # Different key!
    }
}

def get_conduit_address(chain: str) -> str:
    return OPENSEA_CONDUIT_PER_CHAIN.get(chain, OPENSEA_CONDUIT_PER_CHAIN["base"])["address"]

def get_conduit_key(chain: str) -> str:
    return OPENSEA_CONDUIT_PER_CHAIN.get(chain, OPENSEA_CONDUIT_PER_CHAIN["base"])["key"]

# ----- Minimal ABIs -----

# Seaport: read counter
SEAPORT_COUNTER_ABI = [{
    "inputs":[{"internalType":"address","name":"offerer","type":"address"}],
    "name":"getCounter","outputs":[{"internalType":"uint256","name":"counter","type":"uint256"}],
    "stateMutability":"view","type":"function"
}]

# Minimal Seaport basic-order entrypoints (used for fulfillment)
SEAPORT_BASIC_ABI = [
    {
        "type":"function","stateMutability":"payable",
        "outputs":[{"name":"fulfilled","type":"bool"}],
        "name":"fulfillBasicOrder_efficient_6GL6yc",
        "inputs":[{"name":"parameters","type":"tuple","components":[
            {"name":"considerationToken","type":"address"},
            {"name":"considerationIdentifier","type":"uint256"},
            {"name":"considerationAmount","type":"uint256"},
            {"name":"offerer","type":"address"},
            {"name":"zone","type":"address"},
            {"name":"offerToken","type":"address"},
            {"name":"offerIdentifier","type":"uint256"},
            {"name":"offerAmount","type":"uint256"},
            {"name":"basicOrderType","type":"uint8"},
            {"name":"startTime","type":"uint256"},
            {"name":"endTime","type":"uint256"},
            {"name":"zoneHash","type":"bytes32"},
            {"name":"salt","type":"uint256"},
            {"name":"offererConduitKey","type":"bytes32"},
            {"name":"fulfillerConduitKey","type":"bytes32"},
            {"name":"totalOriginalAdditionalRecipients","type":"uint256"},
            {"name":"additionalRecipients","type":"tuple[]","components":[
                {"name":"amount","type":"uint256"},
                {"name":"recipient","type":"address"}
            ]},
            {"name":"signature","type":"bytes"}
        ]}]
    },
    {
        "type":"function","stateMutability":"payable",
        "outputs":[{"name":"fulfilled","type":"bool"}],
        "name":"fulfillBasicOrder",
        "inputs":[{"name":"parameters","type":"tuple","components":[
            {"name":"considerationToken","type":"address"},
            {"name":"considerationIdentifier","type":"uint256"},
            {"name":"considerationAmount","type":"uint256"},
            {"name":"offerer","type":"address"},
            {"name":"zone","type":"address"},
            {"name":"offerToken","type":"address"},
            {"name":"offerIdentifier","type":"uint256"},
            {"name":"offerAmount","type":"uint256"},
            {"name":"basicOrderType","type":"uint8"},
            {"name":"startTime","type":"uint256"},
            {"name":"endTime","type":"uint256"},
            {"name":"zoneHash","type":"bytes32"},
            {"name":"salt","type":"uint256"},
            {"name":"offererConduitKey","type":"bytes32"},
            {"name":"fulfillerConduitKey","type":"bytes32"},
            {"name":"totalOriginalAdditionalRecipients","type":"uint256"},
            {"name":"additionalRecipients","type":"tuple[]","components":[
                {"name":"amount","type":"uint256"},
                {"name":"recipient","type":"address"}
            ]},
            {"name":"signature","type":"bytes"}
        ]}]
    },
]

# ERC-721/1155 for approvals
ERC721_ABI = [
    {"constant": True, "inputs":[{"name":"owner","type":"address"},{"name":"operator","type":"address"}],
     "name":"isApprovedForAll", "outputs":[{"name":"","type":"bool"}], "type":"function"},
    {"constant": False, "inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
     "name":"setApprovalForAll", "outputs":[], "type":"function"},
]

ERC1155_ABI = [
    {"constant": True, "inputs":[{"name":"owner","type":"address"},{"name":"operator","type":"address"}],
     "name":"isApprovedForAll", "outputs":[{"name":"","type":"bool"}], "type":"function"},
    {"constant": False, "inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
     "name":"setApprovalForAll", "outputs":[], "type":"function"},
]

# ERC-20 meta (optional for ERC-20 listings/purchases)
ERC20_ABI = [
    {"constant": True,"inputs": [],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant": True,"inputs": [],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
]

OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1F0DF003000390027140000fAa719"



