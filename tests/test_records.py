"""P-55: records/<slot>.proof.json — a per-slot inclusion proof + ONE receipt, generated, verifiable from bytes."""
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import merkle_proof  # noqa: E402
import records  # noqa: E402
import verifier  # noqa: E402

TX1 = "0x" + "a1" * 32


def _load(rel, base=REPO):
    with open(os.path.join(base, rel), encoding="utf-8") as fh:
        return json.load(fh)


class CommittedRecords(unittest.TestCase):
    def setUp(self):
        self.state = _load("state.json")
        self.slots = [f[:-5] for f in core.list_slot_files(core.ledger_w1_dir(REPO))]

    def test_every_claimed_slot_has_a_record_and_nothing_else_does(self):
        self.assertEqual(records.committed_record_files(REPO),
                         sorted(records.proof_rel(s) for s in self.slots))
        self.assertEqual(records.drift(REPO), [])

    def test_merkle_commit_proof_resolves_to_real_bytes(self):
        """The proof verifies from the slot's two committed files to the published, anchored root."""
        anchors = _load("ledger/anchors.json")
        for slot_id in self.slots:
            rec = _load(records.proof_rel(slot_id))
            root = merkle_proof.verify_proof(rec, os.path.join(REPO, rec["files"]["dossier"]),
                                             os.path.join(REPO, rec["files"]["icon"]), self.state["merkle_root"])
            self.assertEqual(root, self.state["merkle_root"])
            self.assertEqual(rec["tree_size"], self.state["tree_size"])
            if anchors[-1]["root"] == root:
                self.assertEqual(rec["receipt"]["anchor_tx"], anchors[-1]["tx_hash"])

    def test_exactly_one_receipt_with_the_published_fields(self):
        for slot_id in self.slots:
            rel = records.proof_rel(slot_id)
            rec = _load(rel)
            r = rec["receipt"]
            self.assertIsInstance(r, dict)
            self.assertEqual(tuple(r), records.RECEIPT_KEYS)
            self.assertEqual(r["proof_path"], rel)
            self.assertTrue(os.path.exists(os.path.join(REPO, r["proof_path"])))
            d = core.load_json_strict(os.path.join(REPO, "ledger", "w1", f"{slot_id}.json"))
            with open(os.path.join(REPO, "ledger", "w1", f"{slot_id}.webp"), "rb") as fh:
                icon = fh.read()
            self.assertEqual(r["tx_hash"], d["base_tx_hash"])
            self.assertEqual(r["payer"], claims.expected_signer(d))
            self.assertEqual(r["bundle_sha256"], claims.bundle_sha256(d, icon))
            self.assertEqual(r["amount_usdc"], d["settlement"]["amount_usdc"])
            self.assertEqual(r["block_number"], d["settlement"]["block_number"])
            if r["anchor_tx"]:
                anchor = next(a for a in _load("ledger/anchors.json") if a["tx_hash"] == r["anchor_tx"])
                self.assertEqual(r["confirmations"], anchor["block_number"] - r["block_number"] + 1)
                self.assertGreater(r["confirmations"], 0)

    def test_the_verifier_checks_them(self):
        self.assertIn("verify to the current root", records.verify_records(REPO))


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        for rel in ("ledger", "records"):
            shutil.copytree(os.path.join(REPO, rel), os.path.join(self.td, rel))
        self.rel = records.committed_record_files(self.td)[0]

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def rewrite(self, mutate):
        path = os.path.join(self.td, self.rel)
        rec = _load(self.rel, self.td)
        mutate(rec)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(core.render(rec))

    def test_a_tampered_path_is_caught(self):
        self.rewrite(lambda r: r["path"].__setitem__(0, "00" * 32))
        self.assertTrue(records.drift(self.td))
        with self.assertRaisesRegex(ValueError, "Records Violation"):
            records.verify_records(self.td)

    def test_a_tampered_receipt_is_caught(self):
        self.rewrite(lambda r: r["receipt"].update(amount_usdc=0.01))
        with self.assertRaisesRegex(ValueError, "does not match"):
            records.verify_records(self.td)

    def test_missing_and_stray_records_are_caught(self):
        os.remove(os.path.join(self.td, self.rel))
        shutil.copy(os.path.join(REPO, records.committed_record_files(REPO)[-1]),
                    os.path.join(self.td, "records", "w1-b9999.proof.json"))
        problems = " ".join(records.drift(self.td))
        self.assertIn("missing", problems)
        self.assertIn("stray", problems)
        records.write_records(self.td)
        self.assertEqual(records.drift(self.td), [])

    def test_an_unanchored_root_names_no_anchor(self):
        with open(os.path.join(self.td, "ledger", "anchors.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render([]))
        for rec in records.build_records(self.td).values():
            self.assertIsNone(rec["receipt"]["anchor_tx"])
            self.assertIsNone(rec["receipt"]["confirmations"])

    def test_a_ledger_without_records_is_not_judged(self):
        shutil.rmtree(os.path.join(self.td, "records"))
        self.assertIn("no records/", records.verify_records(self.td))


class Pipeline(unittest.TestCase):
    def _wf(self, name):
        with open(os.path.join(REPO, ".github", "workflows", name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_bots_publish_the_records(self):
        self.assertIn("git add ledger/anchors.json records", self._wf("anchor_root.yml"))
        for wf in ("assign_slots.yml", "apply_updates.yml"):
            self.assertIn("git add -A ledger state.json canvas records", self._wf(wf))
        self.assertIn("records/", self._wf("verify_inscription.yml"))

    def test_an_assignment_may_move_every_record_and_an_owner_none(self):
        tx_files = [("D", f"ledger/claims/{TX1}.json"), ("D", f"ledger/claims/{TX1}.webp"),
                    ("D", f"ledger/claims/{TX1}.soul.json")]
        slot = [("A", "ledger/w1/w1-b0011.json"), ("A", "ledger/w1/w1-b0011.webp"),
                ("A", "ledger/souls/w1-b0011.soul.json")]
        recs = [("M", "records/w1-b0001.proof.json"), ("A", "records/w1-b0011.proof.json")]
        gen = [("M", p) for p in sorted(verifier.GENERATED_PATHS)]
        self.assertEqual(verifier.scope_of(tx_files + slot + gen + recs)["kind"], "assignment")
        claim = [(s.replace("D", "A"), p) for s, p in tx_files]
        with self.assertRaises(ValueError):
            verifier.scope_of(claim + [("M", "records/w1-b0001.proof.json")])

    def test_records_are_never_written_by_the_skill(self):
        self.assertIn("records/", _load("permissions.json")["files"]["never_write"])


if __name__ == "__main__":
    unittest.main()
