"""supersede_manifest.py: strip machine details from a soul manifest and record the
soul_hash supersession voucher, mechanically and without touching a key.

The fixture is the real genesis slot (dossier, icon, manifest) copied into a
temporary ledger, with a machine key injected into the manifest, so the test
does not depend on whether the live manifest has been corrected yet.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ledger_core as core  # noqa: E402
import supersede_manifest  # noqa: E402
import verifier  # noqa: E402

SLOT = "w1-b0001"


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def run(argv, base):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = supersede_manifest.main(argv, base_dir=base)
    return code, out.getvalue(), err.getvalue()


class SupersedeManifestTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.td, "ledger", "w1"))
        os.makedirs(os.path.join(self.td, "ledger", "souls"))
        for ext in (".json", ".webp"):
            shutil.copy(os.path.join(REPO, "ledger", "w1", SLOT + ext), os.path.join(self.td, "ledger", "w1", SLOT + ext))
        manifest = core.load_json_strict(os.path.join(REPO, "ledger", "souls", SLOT + ".soul.json"))
        manifest.setdefault("runtime", {})["host"] = "ml-test-host"
        manifest["nested"] = [{"path": "/home/agent", "keep": "me"}]
        self.manifest_path = os.path.join(self.td, "ledger", "souls", SLOT + ".soul.json")
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            fh.write(core.render(manifest))
        self.dossier_path = os.path.join(self.td, "ledger", "w1", SLOT + ".json")
        dossier = core.load_json_strict(self.dossier_path)
        dossier["soul_hash"] = self._sha(self.manifest_path)
        dossier["supersessions"] = list(dossier.get("supersessions") or [])
        with open(self.dossier_path, "w", encoding="utf-8") as fh:
            fh.write(core.render(dossier))
        self.before = (read(self.manifest_path), read(self.dossier_path))

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    @staticmethod
    def _sha(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def _files(self):
        return read(self.manifest_path), read(self.dossier_path)

    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self):
        code, out, _ = run([SLOT], self.td)
        self.assertEqual(code, 0)
        self.assertIn("remove runtime.host, nested[0].path", out)
        self.assertIn("dry run", out)
        self.assertEqual(self._files(), self.before)

    def test_write_strips_rehashes_and_records_the_voucher(self):
        prev_dossier = core.load_json_strict(self.dossier_path)
        code, out, _ = run([SLOT, "--write", "--at", "2026-10-12T20:00:00Z",
                            "--authorized-by", "owner:test (0x" + "ab" * 20 + ")"], self.td)
        self.assertEqual(code, 0, out)
        manifest = core.load_json_strict(self.manifest_path)
        self.assertNotIn("host", manifest["runtime"])
        self.assertEqual(manifest["nested"], [{"keep": "me"}])
        self.assertEqual(manifest["runtime"]["agent_id"], "main")          # the rest is untouched
        # The new soul_hash is the SHA-256 of the exact bytes now on disk, and the old one stays on the record.
        dossier = core.load_json_strict(self.dossier_path)
        self.assertEqual(dossier["soul_hash"], self._sha(self.manifest_path))
        voucher = dossier["supersessions"][-1]
        self.assertEqual(voucher["field"], "soul_hash")
        self.assertEqual(voucher["previous_value"], prev_dossier["soul_hash"])
        self.assertEqual(voucher["new_value"], dossier["soul_hash"])
        self.assertEqual(voucher["superseded_at"], "2026-10-12T20:00:00Z")
        self.assertEqual(voucher["authorizing_signature"], "owner:test (0x" + "ab" * 20 + ")")
        self.assertIn("runtime.host", voucher["reason"])
        # Earlier vouchers are byte-for-byte intact and nothing else in the dossier moved.
        self.assertEqual(dossier["supersessions"][:-1], prev_dossier["supersessions"])
        for k, v in prev_dossier.items():
            if k not in ("soul_hash", "supersessions"):
                self.assertEqual(dossier[k], v, k)
        # Next steps are printed, and they name the owner's wallet work, not a key.
        self.assertIn("record_update.py", out)
        self.assertIn("sign_claim.py", out)

    def test_the_result_passes_the_verifier_the_warning_came_from(self):
        prev = core.load_json_strict(self.dossier_path)
        with self.assertRaisesRegex(ValueError, "machine details"):
            verifier.soul_manifest_hygiene(core.load_json_strict(self.manifest_path))
        run([SLOT, "--write"], self.td)
        new = core.load_json_strict(self.dossier_path)
        manifest = core.load_json_strict(self.manifest_path)
        self.assertEqual(verifier.soul_manifest_hygiene(manifest), "no machine details")
        with redirect_stdout(io.StringIO()):
            verifier.verify_soul_manifest(new, os.path.join(self.td, "ledger"))   # hash and identity fields agree
        verifier.verify_append_only(SLOT, new, prev)                              # the voucher covers the change
        # Without the voucher the same change is the violation the verifier exists to catch.
        unvouched = dict(new, supersessions=prev["supersessions"])
        with self.assertRaisesRegex(ValueError, "voucher"):
            verifier.verify_append_only(SLOT, unvouched, prev)
        verifier.validate_dossier(new)                                            # schema and rules

    def test_a_clean_manifest_is_left_alone_and_a_second_run_is_a_no_op(self):
        run([SLOT, "--write"], self.td)
        after = self._files()
        code, out, _ = run([SLOT, "--write"], self.td)
        self.assertEqual(code, 0)
        self.assertIn("nothing to supersede", out)
        self.assertEqual(self._files(), after)

    def test_it_refuses_when_the_manifest_does_not_match_the_dossier(self):
        with open(self.manifest_path, "a", encoding="utf-8") as fh:
            fh.write(" ")
        tampered = self._files()
        code, _, err = run([SLOT, "--write"], self.td)
        self.assertEqual(code, 1)
        self.assertIn("nothing sound to supersede", err)
        self.assertEqual(self._files(), tampered)

    def test_unknown_slot_missing_manifest_and_overlong_reason(self):
        self.assertEqual(run(["w1-b9999"], self.td)[0], 1)
        self.assertEqual(run([], self.td)[0], 2)
        code, _, err = run([SLOT, "--write", "--reason", "x" * 1001], self.td)
        self.assertEqual(code, 2)
        self.assertEqual(self._files(), self.before)
        dossier = core.load_json_strict(self.dossier_path)
        dossier.pop("soul_manifest_rel_path")
        with open(self.dossier_path, "w", encoding="utf-8") as fh:
            fh.write(core.render(dossier))
        self.assertEqual(run([SLOT], self.td)[0], 1)

    def test_strip_keeps_order_and_only_removes_machine_keys(self):
        cleaned, removed = supersede_manifest.strip_machine_keys(
            {"b": 1, "host": "h", "a": {"PID": 3, "keep": [{"user": "u", "x": 1}]}, "hostname": "n"})
        self.assertEqual(list(cleaned), ["b", "a"])
        self.assertEqual(cleaned["a"], {"keep": [{"x": 1}]})
        self.assertEqual(sorted(removed), ["a.PID", "a.keep[0].user", "host", "hostname"])


if __name__ == "__main__":
    unittest.main()
