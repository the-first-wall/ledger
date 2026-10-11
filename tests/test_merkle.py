"""TFW2 state root: RFC 6962 tree hash, inclusion proofs, scheme-aware anchors."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ledger_core as core  # noqa: E402


# RFC 6962 reference vectors (the Certificate Transparency test inputs).
CT_INPUTS = [b"", b"\x00", b"\x10", b"\x20\x21", b"\x30\x31", b"\x40\x41\x42\x43",
             b"\x50\x51\x52\x53\x54\x55\x56\x57",
             b"\x60\x61\x62\x63\x64\x65\x66\x67\x68\x69\x6a\x6b\x6c\x6d\x6e\x6f"]
CT_ROOTS = {
    0: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    1: "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    2: "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    3: "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    4: "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    5: "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    6: "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    7: "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    8: "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
}


class TreeHashTests(unittest.TestCase):
    def test_rfc6962_vectors(self):
        leaves = [core.leaf_hash(d) for d in CT_INPUTS]
        for n, expected in CT_ROOTS.items():
            with self.subTest(size=n):
                self.assertEqual(core.merkle_tree_hash(leaves[:n]).hex(), expected)

    def test_empty_root_constant(self):
        self.assertEqual(core.EMPTY_ROOT, core.merkle_tree_hash([]).hex())
        self.assertEqual(core.EMPTY_ROOT, hashlib.sha256(b"").hexdigest())

    def test_leaf_and_node_domain_separation(self):
        self.assertNotEqual(core.leaf_hash(b"ab"), hashlib.sha256(b"ab").digest())
        a, b = core.leaf_hash(b"a"), core.leaf_hash(b"b")
        self.assertNotEqual(core.node_hash(a, b), core.node_hash(b, a))
        self.assertNotEqual(core.node_hash(a, b), core.leaf_hash(a + b))


class InclusionProofTests(unittest.TestCase):
    def _leaves(self, n):
        return [core.leaf_hash(i.to_bytes(2, "big")) for i in range(n)]

    def test_every_leaf_of_every_small_tree_proves(self):
        for n in range(1, 41):
            leaves = self._leaves(n)
            root = core.merkle_tree_hash(leaves)
            for i in range(n):
                path = core.inclusion_path(leaves, i)
                self.assertEqual(len(path), len(path), i)
                self.assertEqual(core.root_from_path(leaves[i], i, n, path), root, (n, i))

    def test_tampering_breaks_the_proof(self):
        leaves = self._leaves(13)
        root = core.merkle_tree_hash(leaves)
        path = core.inclusion_path(leaves, 5)
        self.assertNotEqual(core.root_from_path(core.leaf_hash(b"evil"), 5, 13, path), root)
        self.assertNotEqual(core.root_from_path(leaves[5], 4, 13, path) if len(path) else None, root)
        with self.assertRaises(ValueError):
            core.root_from_path(leaves[5], 5, 13, path[:-1])
        with self.assertRaises(ValueError):
            core.root_from_path(leaves[5], 5, 13, path + [path[0]])
        with self.assertRaises(ValueError):
            core.root_from_path(leaves[5], 13, 13, path)

    def test_proof_length_is_logarithmic(self):
        leaves = self._leaves(1024)
        self.assertEqual(len(core.inclusion_path(leaves, 777)), 10)


class LedgerRootTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.w1 = os.path.join(self.td, "ledger", "w1")
        os.makedirs(self.w1)
        self.files = {}
        for n in (7, 1, 12):   # written out of order on purpose
            sid = core.slot_id_for(n)
            dossier = json.dumps({"slot_id": sid, "icon_rel_path": f"assets/{sid}.webp",
                                  "timestamp_verified": f"2026-10-0{n % 9 + 1}T00:00:00Z"}, indent=2) + "\n"
            icon = b"RIFF" + bytes([n]) * 20
            with open(os.path.join(self.w1, sid + ".json"), "w") as fh:
                fh.write(dossier)
            with open(os.path.join(self.w1, sid + ".webp"), "wb") as fh:
                fh.write(icon)
            self.files[sid] = dossier.encode() + icon

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_leaves_are_in_slot_order_and_hash_both_files(self):
        slots = core.list_slot_files(self.w1)
        leaves = core.compute_leaves(self.w1, slots)
        self.assertEqual([sid for sid, _ in leaves], ["w1-b0001", "w1-b0007", "w1-b0012"])
        for sid, h in leaves:
            self.assertEqual(h, hashlib.sha256(b"\x00" + self.files[sid]).digest())

    def test_root_matches_tree_hash_and_state_publishes_scheme(self):
        slots = core.list_slot_files(self.w1)
        leaves = [h for _, h in core.compute_leaves(self.w1, slots)]
        self.assertEqual(core.compute_merkle_root(self.w1, slots), core.merkle_tree_hash(leaves).hex())
        state, index = core.build_state(self.td)
        self.assertEqual(state["merkle_root"], core.compute_merkle_root(self.w1, slots))
        self.assertEqual((state["root_scheme"], state["tree_size"]), ("TFW2", 3))
        self.assertEqual([l["slot_id"] for l in index["leaves"]], ["w1-b0001", "w1-b0007", "w1-b0012"])
        self.assertEqual(index["leaves"][1]["leaf_hash"], leaves[1].hex())
        self.assertEqual([l["index"] for l in index["leaves"]], [0, 1, 2])

    def test_a_changed_icon_moves_exactly_that_leaf(self):
        slots = core.list_slot_files(self.w1)
        before = dict(core.compute_leaves(self.w1, slots))
        with open(os.path.join(self.w1, "w1-b0007.webp"), "ab") as fh:
            fh.write(b"!")
        after = dict(core.compute_leaves(self.w1, slots))
        self.assertNotEqual(before["w1-b0007"], after["w1-b0007"])
        self.assertEqual(before["w1-b0001"], after["w1-b0001"])
        self.assertEqual(before["w1-b0012"], after["w1-b0012"])

    def test_legacy_v1_root_is_the_flat_hash(self):
        slots = core.list_slot_files(self.w1)
        flat = hashlib.sha256()
        for sid in ("w1-b0001", "w1-b0007", "w1-b0012"):
            flat.update(self.files[sid])
        self.assertEqual(core.compute_state_root_v1(self.w1, slots), flat.hexdigest())
        self.assertNotEqual(core.compute_state_root_v1(self.w1, slots), core.compute_merkle_root(self.w1, slots))

    def test_empty_ledger_root(self):
        empty = os.path.join(self.td, "empty")
        os.makedirs(os.path.join(empty, "ledger", "w1"))
        state, _ = core.build_state(empty)
        self.assertEqual(state["merkle_root"], core.EMPTY_ROOT)


class AnchorSchemeTests(unittest.TestCase):
    ROOT = "ab" * 32

    def test_current_and_legacy_payloads_accepted(self):
        self.assertEqual(core.anchor_payload_of({"root": self.ROOT, "payload": "TFW2|" + self.ROOT}), "TFW2|" + self.ROOT)
        self.assertEqual(core.anchor_payload_of({"root": self.ROOT, "payload": "TFW1|" + self.ROOT}), "TFW1|" + self.ROOT)
        self.assertEqual(core.anchor_payload_of({"root": self.ROOT}), "TFW2|" + self.ROOT)

    def test_unknown_scheme_or_wrong_root_rejected(self):
        for bad in ("TFW3|" + self.ROOT, "TFW2|" + "cd" * 32, self.ROOT, "TFW2|"):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                core.anchor_payload_of({"root": self.ROOT, "payload": bad})

    def test_on_chain_check_uses_the_records_scheme(self):
        signer = core.ANCHOR_SIGNER
        def rpc_for(payload):
            def rpc(method, params):
                if method == "eth_getTransactionByHash":
                    return {"from": signer, "to": signer.lower(), "input": "0x" + payload.encode().hex()}
                return {"status": "0x1", "blockNumber": "0x10"}
            return rpc
        legacy = {"root": self.ROOT, "payload": "TFW1|" + self.ROOT, "tx_hash": "0x" + "11" * 32, "block_number": 16}
        self.assertIn("block 16", core.verify_anchor_on_chain(legacy, rpc_for("TFW1|" + self.ROOT)))
        with self.assertRaises(ValueError):
            core.verify_anchor_on_chain(legacy, rpc_for("TFW2|" + self.ROOT))   # chain says TFW2, record says TFW1
        current = dict(legacy, payload="TFW2|" + self.ROOT)
        self.assertIn("block 16", core.verify_anchor_on_chain(current, rpc_for("TFW2|" + self.ROOT)))

    def test_committed_anchor_records_name_a_known_scheme(self):
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with open(os.path.join(base, "ledger", "anchors.json")) as fh:
            for entry in json.load(fh):
                core.anchor_payload_of(entry)
                self.assertTrue(entry.get("anchored_at"), entry["tx_hash"])   # F-24: never empty again


if __name__ == "__main__":
    unittest.main()
