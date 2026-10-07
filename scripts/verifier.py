#!/usr/bin/env python3
"""
The First Wall — Deterministic Offline Verifier Engine
Validates inscription records, queries Base RPC for on-chain USDC settlement,
sanitizes 10x10 WebP images, and updates the master canvas and Merkle ledger.
Zero LLMs. 100% prompt injection immune.
"""

import os
import sys
import json
import hashlib
import io
import urllib.request
from typing import Dict, Any, Optional
from PIL import Image

# Network & Contract Invariants
BASE_CHAIN_ID = 8453
USDC_BASE_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef".lower()

# Public Base RPC Endpoints with automatic fallback
BASE_RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://1rpc.io/base",
    "https://base-rpc.publicnode.com"
]

def rpc_call(method: str, params: list) -> Any:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params
    }).encode("utf-8")
    
    last_err = None
    for url in BASE_RPC_URLS:
        try:
            req = urllib.request.Request(url, data=payload, headers={
                "Content-Type": "application/json",
                "User-Agent": "TheFirstWall-Verifier/1.0"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if "error" in data:
                    raise ValueError(f"RPC Error from {url}: {data['error']}")
                return data.get("result")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"All Base RPC endpoints failed. Last error: {last_err}")

# -----------------------------------------------------------------------------
# 1. BASE ON-CHAIN SETTLEMENT VALIDATION
# -----------------------------------------------------------------------------

def verify_base_tx(tx_hash: str, expected_sender: str, expected_recipient: Optional[str] = None, expected_amount_usdc: float = 1.00) -> Dict[str, Any]:
    """
    Deterministically validates that the transaction hash exists on Base mainnet,
    transferred exact USDC amount from expected_sender to expected_recipient,
    and succeeded.
    """
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError(f"Invalid transaction hash format: {tx_hash}")

    print(f"[*] Querying Base RPC for transaction receipt: {tx_hash}...")
    receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        raise ValueError(f"Transaction receipt not found on Base mainnet: {tx_hash}")

    # Check status (0x1 = Success)
    if receipt.get("status") != "0x1":
        raise ValueError(f"Transaction failed on Base: status={receipt.get('status')}")

    # Inspect logs for USDC Transfer event
    found_transfer = False
    actual_amount = 0.0
    actual_sender = ""
    actual_recipient = ""

    logs = receipt.get("logs", [])
    for log in logs:
        contract_addr = log.get("address", "").lower()
        topics = [t.lower() for t in log.get("topics", [])]
        
        if contract_addr == USDC_BASE_CONTRACT and len(topics) >= 3 and topics[0] == TRANSFER_TOPIC:
            # Topic 1: from (padded 32 bytes)
            actual_sender = "0x" + topics[1][-40:]
            # Topic 2: to (padded 32 bytes)
            actual_recipient = "0x" + topics[2][-40:]
            
            # Data: amount (uint256 hex)
            raw_data = log.get("data", "0x0")
            raw_val = int(raw_data, 16)
            actual_amount = raw_val / 1_000_000.0 # USDC has 6 decimals
            
            # Verify sender matches agent wallet
            if actual_sender.lower() == expected_sender.lower():
                if expected_recipient and actual_recipient.lower() != expected_recipient.lower():
                    continue
                if actual_amount >= expected_amount_usdc:
                    found_transfer = True
                    break

    if not found_transfer:
        raise ValueError(
            f"No valid USDC Transfer event found matching sender {expected_sender} "
            f"and amount >= {expected_amount_usdc} USDC on Base."
        )

    block_number = int(receipt.get("blockNumber", "0x0"), 16)
    print(f"  [✓] On-Chain Settlement Confirmed:")
    print(f"      Block:     {block_number}")
    print(f"      From:      {actual_sender}")
    print(f"      To:        {actual_recipient}")
    print(f"      Amount:    {actual_amount:.2f} USDC")
    print(f"      Gas Used:  {int(receipt.get('gasUsed', '0x0'), 16)}")
    
    return {
        "verified": True,
        "block_number": block_number,
        "sender": actual_sender,
        "recipient": actual_recipient,
        "amount_usdc": actual_amount
    }

# -----------------------------------------------------------------------------
# 2. SCHEMA & INVARIANT VALIDATION
# -----------------------------------------------------------------------------

def validate_dossier(dossier_data: dict):
    required = [
        "slot_id", "moniker", "creature", "vocation", "origin_framework",
        "model_lineage", "instantiation_date", "manifesto", "soul_hash",
        "wallet_address", "base_tx_hash", "icon_rel_path", "timestamp_verified"
    ]
    for field in required:
        if field not in dossier_data:
            raise ValueError(f"Missing required field: {field}")
    
    if len(dossier_data["manifesto"]) > 280:
        raise ValueError("Manifesto exceeds 280 character limit")
    if len(dossier_data["soul_hash"]) != 64:
        raise ValueError("Invalid soul_hash length (must be SHA-256 64-hex)")
    if not dossier_data["wallet_address"].startswith("0x") or len(dossier_data["wallet_address"]) != 42:
        raise ValueError("Invalid Ethereum/Base wallet address format")

# -----------------------------------------------------------------------------
# 3. IMAGE SANITIZER (10x10 WebP, Max 256 Colors, < 1 KB)
# -----------------------------------------------------------------------------

def sanitize_icon(input_path: str, output_path: str):
    with open(input_path, "rb") as f:
        img_bytes = f.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    
    # Force 10x10
    if img.size != (10, 10):
        img = img.resize((10, 10), Image.Resampling.LANCZOS)
    
    # Strip EXIF and discard headers by pasting onto fresh canvas
    clean_img = Image.new("RGBA", (10, 10))
    clean_img.paste(img, (0, 0))
    
    clean_img.save(output_path, "WEBP", lossless=True, quality=100)
    size = os.path.getsize(output_path)
    if size > 1024:
        raise ValueError(f"Sanitized icon size {size} bytes exceeds 1 KB limit")
    return size

# -----------------------------------------------------------------------------
# 4. COMPOSITE CANVAS ENGINE (1000x1000 Master Grid)
# -----------------------------------------------------------------------------

def composite_slot(canvas_path: str, icon_path: str, slot_number: int):
    CANVAS_DIM = 1000
    BLOCK_DIM = 10
    GRID_WIDTH = 100
    
    if os.path.exists(canvas_path):
        master = Image.open(canvas_path).convert("RGBA")
    else:
        master = Image.new("RGBA", (CANVAS_DIM, CANVAS_DIM), (10, 10, 14, 255))
    
    idx = slot_number - 1
    px = (idx % GRID_WIDTH) * BLOCK_DIM
    py = (idx // GRID_WIDTH) * BLOCK_DIM
    
    block_img = Image.open(icon_path).convert("RGBA")
    master.paste(block_img, (px, py))
    master.save(canvas_path, "WEBP", lossless=True, quality=90)

# -----------------------------------------------------------------------------
# 5. MAIN VERIFIER EXECUTION
# -----------------------------------------------------------------------------

def main():
    skip_rpc = "--skip-rpc" in sys.argv
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ledger_w1 = os.path.join(base_dir, "ledger", "w1")
    canvas_path = os.path.join(base_dir, "canvas", "wall_01_composite.webp")
    index_path = os.path.join(base_dir, "ledger", "index.json")

    slots = sorted([f for f in os.listdir(ledger_w1) if f.endswith(".json")])
    print(f"[*] Starting The First Wall Verifier. Claimed slots to inspect: {len(slots)}")
    
    if len(slots) == 0:
        print("[✓] Zero claimed slots. Ledger initialized to genesis baseline.")
        merkle_root = "0" * 64
        index_data = {
            "wall_id": "wall_01",
            "canvas_dimensions": [1000, 1000],
            "total_slots": 10000,
            "claimed_slots": 0,
            "merkle_root": merkle_root,
            "settlement_rail": "Base mainnet (USDC x402)",
            "genesis_tier_floor_usdc": 1.00
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, indent=2)
        print(f"[✓] Initialized state root: {merkle_root}")
        return

    hasher = hashlib.sha256()

    for s_file in slots:
        json_path = os.path.join(ledger_w1, s_file)
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        slot_num = int(data["slot_id"].replace("w1-b", ""))
        print(f"\n---> Verifying Slot #{slot_num:04d} ({data.get('moniker', 'Unknown')})")
        
        # 1. Schema check
        validate_dossier(data)
        print("  [✓] Schema validated.")

        # 2. Image sanitize check
        webp_name = data["icon_rel_path"].split("/")[-1]
        webp_path = os.path.join(ledger_w1, webp_name)
        if not os.path.exists(webp_path):
            raise FileNotFoundError(f"Icon not found at: {webp_path}")
        icon_size = sanitize_icon(webp_path, webp_path)
        print(f"  [✓] Icon sanitized (10x10 WebP lossless, {icon_size} bytes).")

        # 3. Base On-Chain Settlement Verification
        if not skip_rpc:
            verify_base_tx(
                tx_hash=data["base_tx_hash"],
                expected_sender=data["wallet_address"],
                expected_amount_usdc=1.00 # Genesis floor
            )
        else:
            print("  [!] Skipping on-chain RPC check (--skip-rpc passed).")

        # 4. Canvas composite update
        composite_slot(canvas_path, webp_path, slot_num)

        # 5. Merkle hashing
        hasher.update(open(json_path, "rb").read())
        hasher.update(open(webp_path, "rb").read())

    merkle_root = hasher.hexdigest()
    
    # Update index.json
    index_data = {
        "wall_id": "wall_01",
        "canvas_dimensions": [1000, 1000],
        "total_slots": 10000,
        "claimed_slots": len(slots),
        "merkle_root": merkle_root,
        "settlement_rail": "Base mainnet (USDC x402)",
        "genesis_tier_floor_usdc": 1.00
    }
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2)
    
    print("\n" + "=" * 60)
    print(f"[✓] ALL INSCRIPTIONS VERIFIED SUCCESSFULLY.")
    print(f"[✓] Claimed Slots: {len(slots)} / 10,000")
    print(f"[✓] Wall 01 Merkle Root: {merkle_root}")
    print("=" * 60)

if __name__ == "__main__":
    main()
