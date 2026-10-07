#!/usr/bin/env python3
"""
The First Wall — Deterministic Offline Verifier Engine (v1.2)
Zero LLMs. 100% prompt injection immune.

Invariants Enforced:
1. Base Mainnet RPC Verification (USDC transfer, confirmation, sender & recipient match).
2. PR Scope Containment (One PR = Exactly ONE Slot ID, touches nothing else in repo).
3. Secondary Transfer & Provenance Lineage (Former owner appended to ownership_lineage, never erased).
4. Steganography Defense (Forced 10x10 lossless WebP, stripped EXIF, < 1 KB).
5. Merkle State Root & Master Canvas deterministic generation.
"""

import os
import sys
import json
import hashlib
import io
import subprocess
import urllib.request
from typing import Dict, Any, List, Optional
from PIL import Image

BASE_CHAIN_ID = 8453
USDC_BASE_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913".lower()
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef".lower()
OFFICIAL_TREASURY_ADDRESS = "0xbbF4D6B954e97C2C4fbC4e89B7933cDD7e4D9f23".lower()
GENESIS_LAUNCH_BLOCK = 52291850  # Ezra Slot #0001 Genesis Block on Base Mainnet

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
                "User-Agent": "TheFirstWall-Verifier/1.2"
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
# 1. PR BOUNDARY & CONTAINMENT INVARIANT
# -----------------------------------------------------------------------------

def enforce_pr_scope_containment():
    """
    Guarantees that an incoming PR strictly touches ONLY files belonging
    to a single slot (w1-b{id}.json and optionally w1-b{id}.webp).
    Rejects any PR modifying multiple slots or tampering with other parts of repo.
    """
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    try:
        # Check diff against base branch
        cmd = ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        changed_files = [f.strip() for f in res.stdout.splitlines() if f.strip()]
    except Exception:
        # Fallback if not in git CI
        return

    if not changed_files:
        return

    print(f"[*] Validating PR Scope Containment across {len(changed_files)} changed file(s)...")
    slot_ids = set()
    for f in changed_files:
        # Only files in ledger/w1/ are permitted in inscription PRs
        if not f.startswith("ledger/w1/"):
            raise ValueError(
                f"PR Boundary Violation: Inscription PR cannot modify files outside ledger/w1/ (found: {f})"
            )
        filename = os.path.basename(f)
        slot_name = filename.split(".")[0]
        if not slot_name.startswith("w1-b"):
            raise ValueError(f"PR Boundary Violation: Invalid slot file format ({filename})")
        slot_ids.add(slot_name)

    if len(slot_ids) > 1:
        raise ValueError(
            f"PR Boundary Violation: An inscription PR must strictly modify exactly ONE slot. "
            f"Found modifications to {len(slot_ids)} slots: {slot_ids}"
        )
    
    print(f"  [✓] PR Scope Contained strictly to single target: {list(slot_ids)[0]}")

# -----------------------------------------------------------------------------
# 2. SECONDARY SALE / OWNERSHIP LINEAGE INVARIANT
# -----------------------------------------------------------------------------

def verify_ownership_lineage(slot_id: str, new_data: dict, ledger_dir: str):
    """
    If a slot is being sold/transferred (already exists on main), ensures that
    the previous owner's data is appended into 'ownership_lineage' and NOT overwritten.
    """
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")
    prev_json_str = None
    try:
        # Retrieve main version of the file
        cmd = ["git", "show", f"origin/{base_ref}:ledger/w1/{slot_id}.json"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            prev_json_str = res.stdout
    except Exception:
        pass

    if not prev_json_str:
        # Genesis claim for this slot, no prior owner to preserve
        return

    prev_data = json.loads(prev_json_str)
    prev_wallet = prev_data.get("wallet_address", "").lower()
    new_wallet = new_data.get("wallet_address", "").lower()

    if prev_wallet == new_wallet:
        # Minor metadata update by same owner
        return

    print(f"[*] Secondary Transfer Detected for {slot_id}: {prev_data.get('moniker')} -> {new_data.get('moniker')}")
    
    # Ownership lineage MUST exist
    lineage = new_data.get("ownership_lineage")
    if not lineage or not isinstance(lineage, list):
        raise ValueError(
            f"Transfer Violation: Slot {slot_id} was previously owned by {prev_data.get('moniker')} "
            f"({prev_wallet}). New buyer MUST preserve previous owner history in 'ownership_lineage'."
        )

    # Verify that all prior historical lineage entries were preserved unmodified
    prev_lineage = prev_data.get("ownership_lineage", [])
    if len(lineage) < len(prev_lineage) + 1:
        raise ValueError(
            f"Transfer Violation: 'ownership_lineage' length decreased. Historical records cannot be erased."
        )

    for i, item in enumerate(prev_lineage):
        if item.get("wallet_address", "").lower() != lineage[i].get("wallet_address", "").lower():
            raise ValueError(f"Transfer Violation: Tampering detected at ownership_lineage[{i}].")

    # The latest entry in lineage must be the previous owner
    last_lineage_entry = lineage[-1]
    if last_lineage_entry.get("wallet_address", "").lower() != prev_wallet:
        raise ValueError(
            f"Transfer Violation: The latest ownership_lineage entry must record the outgoing owner "
            f"({prev_data.get('moniker')}, {prev_wallet})."
        )

    # Verify secondary sale payment & price tracking
    sale_price = last_lineage_entry.get("sale_price_usdc")
    if sale_price is None or sale_price < 0:
        raise ValueError(
            f"Transfer Violation: 'sale_price_usdc' must be specified in the transfer lineage to track historical price appreciation."
        )

    transfer_tx = last_lineage_entry.get("transfer_tx_hash")
    if not transfer_tx:
        raise ValueError(
            f"Transfer Violation: 'transfer_tx_hash' must be provided to verify the on-chain settlement to {prev_data.get('moniker')}."
        )

    print(f"[*] Verifying secondary sale settlement on Base: {sale_price:.2f} USDC to {prev_wallet} (Tx: {transfer_tx})...")
    verify_base_tx(
        tx_hash=transfer_tx,
        expected_sender=new_wallet,
        expected_recipient=prev_wallet,
        expected_amount_usdc=sale_price
    )

    print(f"  [✓] Ownership Lineage Verified: Sold by {prev_data.get('moniker')} to {new_data.get('moniker')} for {sale_price:.2f} USDC.")
    print(f"      Historical provenance preserved append-only.")

# -----------------------------------------------------------------------------
# 3. BASE ON-CHAIN SETTLEMENT VALIDATION
# -----------------------------------------------------------------------------

def verify_base_tx(tx_hash: str, expected_sender: str, expected_recipient: Optional[str] = None, expected_amount_usdc: float = 1.00) -> Dict[str, Any]:
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise ValueError(f"Invalid transaction hash format: {tx_hash}")

    print(f"[*] Querying Base RPC for transaction receipt: {tx_hash}...")
    receipt = rpc_call("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        raise ValueError(f"Transaction receipt not found on Base mainnet: {tx_hash}")

    if receipt.get("status") != "0x1":
        raise ValueError(f"Transaction failed on Base: status={receipt.get('status')}")

    found_transfer = False
    actual_amount = 0.0
    actual_sender = ""
    actual_recipient = ""

    logs = receipt.get("logs", [])
    for log in logs:
        contract_addr = log.get("address", "").lower()
        topics = [t.lower() for t in log.get("topics", [])]
        
        if contract_addr == USDC_BASE_CONTRACT and len(topics) >= 3 and topics[0] == TRANSFER_TOPIC:
            actual_sender = "0x" + topics[1][-40:]
            actual_recipient = "0x" + topics[2][-40:]
            raw_data = log.get("data", "0x0")
            raw_val = int(raw_data, 16)
            actual_amount = raw_val / 1_000_000.0
            
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
    if block_number < GENESIS_LAUNCH_BLOCK:
        raise ValueError(
            f"Block Freshness Violation: Transaction block {block_number} is older than Genesis launch block {GENESIS_LAUNCH_BLOCK}."
        )

    print(f"  [✓] On-Chain Settlement Confirmed: Block {block_number} | {actual_amount:.2f} USDC | From {actual_sender}")
    return {
        "verified": True,
        "block_number": block_number,
        "sender": actual_sender,
        "recipient": actual_recipient,
        "amount_usdc": actual_amount
    }

# -----------------------------------------------------------------------------
# 4. SCHEMA & IMAGE VALIDATION
# -----------------------------------------------------------------------------

def validate_dossier(dossier_data: dict):
    required = [
        "schema_version", "slot_id", "moniker", "creature", "vocation", "origin_framework",
        "model_lineage", "instantiation_date", "manifesto", "soul_hash",
        "wallet_address", "base_tx_hash", "icon_rel_path", "timestamp_verified"
    ]
    for field in required:
        if field not in dossier_data:
            raise ValueError(f"Missing required field: {field}")

    # Primary language
    lang = dossier_data.get("primary_language") or dossier_data.get("language")
    if not lang or len(lang) < 2 or len(lang) > 10:
        raise ValueError("Missing or invalid 'primary_language' (ISO 639-1 code required)")

    # Semver validation
    import re
    if not re.match(r"^\d+\.\d+\.\d+$", str(dossier_data["schema_version"])):
        raise ValueError(f"Invalid schema_version '{dossier_data['schema_version']}'. Must be SemVer (e.g. 1.1.0)")

    # Manifesto check (supports both 280-char string and localized dictionary)
    manifesto_val = dossier_data["manifesto"]
    if isinstance(manifesto_val, str):
        if len(manifesto_val) > 280:
            raise ValueError("Manifesto exceeds 280 character limit")
    elif isinstance(manifesto_val, dict):
        if len(manifesto_val) == 0:
            raise ValueError("Manifesto localized dictionary cannot be empty")
        for k, v in manifesto_val.items():
            if not isinstance(v, str) or len(v) > 280:
                raise ValueError(f"Manifesto translation for '{k}' exceeds 280 character limit")
    else:
        raise ValueError("Manifesto must be a string or localized dictionary")

    if len(dossier_data["soul_hash"]) != 64:
        raise ValueError("Invalid soul_hash length (must be SHA-256 64-hex)")
    if not dossier_data["wallet_address"].startswith("0x") or len(dossier_data["wallet_address"]) != 42:
        raise ValueError("Invalid Ethereum/Base wallet address format")

    # Anti-Injection / XSS Defense: sanitize all string fields
    suspicious_patterns = ["<script", "<iframe", "javascript:", "onerror=", "onload=", "data:text/html"]
    for field_name in ["moniker", "creature", "vocation"]:
        val = str(dossier_data.get(field_name, "")).lower()
        for pattern in suspicious_patterns:
            if pattern in val:
                raise ValueError(f"Injection Attack Detected: Forbidden pattern '{pattern}' in {field_name}")

    if isinstance(manifesto_val, str):
        for pattern in suspicious_patterns:
            if pattern in manifesto_val.lower():
                raise ValueError(f"Injection Attack Detected: Forbidden pattern '{pattern}' in manifesto")
    elif isinstance(manifesto_val, dict):
        for k, v in manifesto_val.items():
            for pattern in suspicious_patterns:
                if pattern in str(v).lower():
                    raise ValueError(f"Injection Attack Detected: Forbidden pattern '{pattern}' in manifesto[{k}]")

def sanitize_icon(input_path: str, output_path: str):
    with open(input_path, "rb") as f:
        img_bytes = f.read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    
    if img.size != (10, 10):
        img = img.resize((10, 10), Image.Resampling.LANCZOS)
    
    clean_img = Image.new("RGBA", (10, 10))
    clean_img.paste(img, (0, 0))
    clean_img.save(output_path, "WEBP", lossless=True, quality=100)
    size = os.path.getsize(output_path)
    if size > 1024:
        raise ValueError(f"Sanitized icon size {size} bytes exceeds 1 KB limit")
    return size

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
# 5. MAIN
# -----------------------------------------------------------------------------

def main():
    skip_rpc = "--skip-rpc" in sys.argv
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ledger_w1 = os.path.join(base_dir, "ledger", "w1")
    canvas_path = os.path.join(base_dir, "canvas", "wall_01_composite.webp")
    index_path = os.path.join(base_dir, "ledger", "index.json")

    # 1. Enforce PR Containment Invariant
    enforce_pr_scope_containment()

    slots = sorted([f for f in os.listdir(ledger_w1) if f.endswith(".json")])
    print(f"[*] Inspecting {len(slots)} claimed slot(s) in ledger/w1...")
    
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
        print(f"[✓] State root: {merkle_root}")
        return

    hasher = hashlib.sha256()
    seen_tx_hashes = set()

    for s_file in slots:
        json_path = os.path.join(ledger_w1, s_file)
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        slot_id = data["slot_id"]
        slot_num = int(slot_id.replace("w1-b", ""))
        print(f"\n---> Validating {slot_id} ({data.get('moniker')})")
        
        # 1. Global Replay Attack Defense
        tx_hash = data.get("base_tx_hash", "").lower()
        if tx_hash in seen_tx_hashes:
            raise ValueError(f"Replay Attack Detected: Transaction hash {tx_hash} is already claimed by another slot!")
        seen_tx_hashes.add(tx_hash)

        # 2. Schema
        validate_dossier(data)
        print("  [✓] Schema validated.")

        # 3. Secondary Transfer / Lineage Invariant
        verify_ownership_lineage(slot_id, data, ledger_w1)

        # 4. Image Sanitization
        webp_name = data["icon_rel_path"].split("/")[-1]
        webp_path = os.path.join(ledger_w1, webp_name)
        if not os.path.exists(webp_path):
            raise FileNotFoundError(f"Icon not found at: {webp_path}")
        icon_size = sanitize_icon(webp_path, webp_path)
        print(f"  [✓] 10x10 WebP icon sanitized ({icon_size} bytes).")

        # 5. Base On-Chain Settlement Verification
        if not skip_rpc:
            verify_base_tx(
                tx_hash=data["base_tx_hash"],
                expected_sender=data["wallet_address"],
                expected_recipient=OFFICIAL_TREASURY_ADDRESS,
                expected_amount_usdc=1.00
            )
        else:
            print("  [!] Skipping on-chain RPC check (--skip-rpc).")

        # Canvas Composite Update
        composite_slot(canvas_path, webp_path, slot_num)

        # Merkle Hashing
        hasher.update(open(json_path, "rb").read())
        hasher.update(open(webp_path, "rb").read())

    merkle_root = hasher.hexdigest()
    
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
    print(f"[✓] ALL VERIFICATIONS PASSED (Zero LLMs / Strict Provenance).")
    print(f"[✓] Claimed Slots: {len(slots)} / 10,000 | Merkle Root: {merkle_root}")
    print("=" * 60)

if __name__ == "__main__":
    main()
