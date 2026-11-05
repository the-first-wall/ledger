"""Absence certificate: a signed negative proof — no record for a wallet at an anchored root."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from eth_account import Account

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import absence_certificate as absence  # noqa: E402
import ledger_core as core  # noqa: E402
import verifier  # noqa: E402

KEY = "0x" + "7a" * 32                       # an ephemeral test key stands in for the anchor signer
SIGNER = Account.from_key(KEY).address
OTHER_KEY = "0x" + "3c" * 32
NOBODY = "0x000000000000000000000000000000000000dEaD"
OWNER = "0xE2C6bc227DE40561FE4a513e7FD05B3F7873c512"   # owns blocks on the committed wall


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        shutil.copytree(os.path.join(REPO, "ledger"), os.path.join(self.td, "ledger"))
        p = mock.patch.object(core, "ANCHOR_SIGNER", SIGNER)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def issue(self, wallet=NOBODY, key=KEY):
        env = mock.patch.dict(os.environ, {"TFW_TEST_ANCHOR_KEY": key})
        with env:
            return absence.main([wallet, "--key-env", "TFW_TEST_ANCHOR_KEY"], base_dir=self.td)

    def rel(self, wallet=NOBODY):
        return absence.certificate_rel(wallet)

    def doc(self, wallet=NOBODY):
        with open(os.path.join(self.td, self.rel(wallet)), encoding="utf-8") as fh:
            return json.load(fh)

    def put(self, doc, wallet=NOBODY):
        with open(os.path.join(self.td, self.rel(wallet)), "w", encoding="utf-8") as fh:
            fh.write(core.render(doc))


class Issuing(Sandbox):
    def test_a_certificate_for_a_wallet_with_no_slot(self):
        self.assertEqual(self.issue(), 0)
        d = self.doc()
        root, _ = absence.current_root(self.td)
        last = json.load(open(os.path.join(self.td, "ledger", "anchors.json")))[-1]
        self.assertEqual(d["root"], root)
        self.assertEqual(d["wallet"], NOBODY.lower())
        self.assertEqual(d["statement"], f"no record for this wallet at root {root}, anchored in block "
                                         f"{last['block_number']}, as of {last['anchored_at']}")
        self.assertEqual(d["message"], f"thefirstwall.ai|absence|v1|{NOBODY.lower()}|{root}|"
                                       f"{last['block_number']}|{last['anchored_at']}")
        self.assertEqual(d["signer"], SIGNER)
        self.assertEqual(absence.verify_certificate(self.td, self.rel())[0], "current")

    def test_no_certificate_for_a_wallet_on_the_wall(self):
        self.assertEqual(self.issue(OWNER), 1)
        self.assertFalse(os.path.exists(os.path.join(self.td, self.rel(OWNER))))

    def test_no_certificate_for_a_wallet_with_a_pending_claim(self):
        os.makedirs(os.path.join(self.td, "ledger", "claims"))
        tx = "0x" + "c1" * 32
        with open(os.path.join(self.td, "ledger", "claims", f"{tx}.json"), "w") as fh:
            json.dump({"wallet_address": NOBODY, "base_tx_hash": tx}, fh)
        with self.assertRaisesRegex(ValueError, "has a record"):
            absence.build_body(self.td, NOBODY)

    def test_no_certificate_while_the_root_is_unanchored(self):
        with open(os.path.join(self.td, "ledger", "anchors.json"), "w") as fh:
            fh.write(core.render([]))
        with self.assertRaisesRegex(ValueError, "not anchored"):
            absence.build_body(self.td, NOBODY)

    def test_only_the_anchor_signer_may_sign(self):
        self.assertEqual(self.issue(key=OTHER_KEY), 1)
        self.assertFalse(os.path.exists(os.path.join(self.td, self.rel())))

    def test_offline_signing_path(self):
        body = absence.build_body(self.td, NOBODY)
        sig = absence.sign(body["message"], KEY)
        self.assertEqual(absence.main([NOBODY, "--signature", sig], base_dir=self.td), 0)
        self.assertEqual(absence.main(["--verify", os.path.join(self.td, self.rel())], base_dir=self.td), 0)

    def test_the_tool_never_takes_a_key_argument_or_file(self):
        src = open(os.path.join(REPO, "scripts", "absence_certificate.py"), encoding="utf-8").read()
        for needle in ("--key-file", "--private-key", "open(key", "print(key"):
            self.assertNotIn(needle, src)


class Verifying(Sandbox):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.issue(), 0)

    def test_a_forged_signature_fails(self):
        d = self.doc()
        d["signature"] = absence.sign(d["message"], OTHER_KEY)
        self.put(d)
        with self.assertRaisesRegex(ValueError, "not the anchor signer"):
            absence.verify_certificate(self.td, self.rel())

    def test_a_rewritten_statement_fails(self):
        d = self.doc()
        d["statement"] = d["statement"].replace("no record", "a record")
        self.put(d)
        with self.assertRaisesRegex(ValueError, "do not match"):
            absence.verify_certificate(self.td, self.rel())

    def test_an_unrecorded_anchor_fails(self):
        d = self.doc()
        d["anchor"]["tx_hash"] = "0x" + "00" * 32
        self.put(d)
        with self.assertRaisesRegex(ValueError, "not a recorded anchor"):
            absence.verify_certificate(self.td, self.rel())

    def test_a_moved_wall_supersedes_not_invalidates(self):
        path = os.path.join(self.td, "ledger", "w1", "w1-b0001.json")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(" ")                           # new bytes, new root
        status, detail = absence.verify_certificate(self.td, self.rel())
        self.assertEqual(status, "superseded")
        self.assertIn("superseded by root", detail)

    def test_the_verifier_reports_each_certificate(self):
        log = verifier.CheckLog()
        with mock.patch.object(verifier, "changed_entries", return_value=None):
            report = verifier.run_verification(self.td, skip_rpc=True, log_seed=log)
        tag = "absent." + NOBODY.lower()
        rec = next(r for r in report["checks"] if r["check"] == tag)
        self.assertEqual(rec["status"], "PASS", rec)
        d = self.doc()
        d["wallet"] = OWNER.lower()
        self.put(d)                                  # wrong wallet for its file name
        report = verifier.run_verification(self.td, skip_rpc=True)
        self.assertEqual(next(r for r in report["checks"] if r["check"] == tag)["status"], "FAIL")


class Published(unittest.TestCase):
    def test_issued_by_a_dispatch_workflow_with_the_anchor_secret(self):
        wf = open(os.path.join(REPO, ".github", "workflows", "absence_certificate.yml"), encoding="utf-8").read()
        self.assertIn("workflow_dispatch", wf)
        self.assertIn("--key-env ANCHOR_PRIVATE_KEY", wf)
        # The input reaches the shell only through an env assignment, never interpolated into a script.
        uses = [ln.strip() for ln in wf.splitlines() if "inputs.wallet" in ln]
        self.assertTrue(uses)
        self.assertTrue(all(ln == "WALLET: ${{ inputs.wallet }}" for ln in uses), uses)

    def test_certificates_are_never_written_by_the_skill(self):
        perms = json.load(open(os.path.join(REPO, "permissions.json")))
        self.assertIn("absent/", perms["files"]["never_write"])


if __name__ == "__main__":
    unittest.main()
