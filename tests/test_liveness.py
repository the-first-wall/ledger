"""P-64: liveness. expected_heartbeat + last_witnessed_at are declared; absent_since is DERIVED by rule."""
import json
import os
import sys
import unittest

import jsonschema

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ledger_core as core  # noqa: E402
import verifier  # noqa: E402
from test_updates import SELLER, TX_FEE1, USDC, FakeRpc, fee, inscribed  # noqa: E402
import record_update  # noqa: E402
import tempfile  # noqa: E402

SCHEMA = jsonschema.Draft202012Validator(verifier.DOSSIER_SCHEMA, format_checker=jsonschema.FormatChecker())


def beating(**over):
    d = inscribed(expected_heartbeat="P30D", last_witnessed_at="2026-10-11T12:00:00Z")
    d.update(over)
    return d


class DerivationRule(unittest.TestCase):
    def test_present_before_the_heartbeat_lapses(self):
        live = core.liveness(beating(), "2026-11-10T11:59:59Z")
        self.assertEqual(live["absent_after"], "2026-11-10T12:00:00Z")
        self.assertIsNone(live["absent_since"])

    def test_absent_from_the_moment_it_lapses_dated_by_rule(self):
        for as_of in ("2026-11-10T12:00:00Z", "2027-01-01T00:00:00Z"):
            live = core.liveness(beating(), as_of)
            # The date of absence is the rule's, not the observer's: the same on any later day.
            self.assertEqual(live["absent_since"], "2026-11-10T12:00:00Z", as_of)

    def test_no_heartbeat_no_liveness(self):
        self.assertIsNone(core.liveness(inscribed(), "2027-01-01T00:00:00Z"))

    def test_heartbeat_format(self):
        self.assertEqual(core.heartbeat_days("P1D"), 1)
        for bad in ("P0D", "30D", "P1W", "PT24H", "P10000D", 30, None):
            with self.assertRaises(ValueError):
                core.heartbeat_days(bad)

    def test_the_ledger_clock_is_deterministic(self):
        a = core.ledger_clock(REPO)
        self.assertEqual(a, core.ledger_clock(REPO))
        anchors = json.load(open(os.path.join(REPO, "ledger", "anchors.json")))
        state = json.load(open(os.path.join(REPO, "state.json")))
        self.assertEqual(a, max(anchors[-1]["anchored_at"], state["last_updated"]))


class VerifierRule(unittest.TestCase):
    def test_a_declared_heartbeat_passes(self):
        self.assertIn("present", verifier.verify_liveness("w1-b0042", beating(), "2026-10-20T00:00:00Z"))
        self.assertIn("ABSENT since 2026-11-10T12:00:00Z",
                      verifier.verify_liveness("w1-b0042", beating(), "2026-12-01T00:00:00Z"))

    def test_absent_since_is_never_set_by_a_claim(self):
        d = beating(absent_since="2026-10-12T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "derived"):
            verifier.verify_liveness("w1-b0042", d, "2026-10-20T00:00:00Z")
        self.assertFalse(SCHEMA.is_valid(d), "the schema must refuse absent_since too")
        with self.assertRaises(ValueError):
            verifier.validate_dossier(d)
        self.assertFalse(jsonschema.Draft202012Validator(verifier.CLAIM_SCHEMA).is_valid(d))

    def test_a_typed_in_witness_is_refused(self):
        d = beating(last_witnessed_at="2026-12-31T00:00:00Z")   # nothing attested happened then
        with self.assertRaisesRegex(ValueError, "attested"):
            verifier.verify_liveness("w1-b0042", d, "2026-10-20T00:00:00Z")

    def test_a_paid_update_is_a_witness(self):
        d = beating(update_settlements=[fee(at="2026-11-01T09:00:00Z")], last_witnessed_at="2026-11-01T09:00:00Z")
        self.assertIn("absent after 2026-12-01T09:00:00Z",
                      verifier.verify_liveness("w1-b0042", d, "2026-11-05T00:00:00Z"))

    def test_the_two_fields_come_as_a_pair(self):
        for d in (inscribed(expected_heartbeat="P30D"), inscribed(last_witnessed_at="2026-10-11T12:00:00Z")):
            self.assertFalse(SCHEMA.is_valid(d))
            with self.assertRaisesRegex(ValueError, "pair"):
                verifier.verify_liveness("w1-b0042", d, "2026-10-20T00:00:00Z")
        self.assertTrue(SCHEMA.is_valid(beating()), list(SCHEMA.iter_errors(beating())))

    def test_the_cadence_and_witness_are_living_fields(self):
        prev = beating()
        new = dict(prev, expected_heartbeat="P7D")
        verifier.verify_append_only("w1-b0042", new, prev)

    def test_committed_ledger_declares_nothing_and_passes(self):
        for name in sorted(os.listdir(os.path.join(REPO, "ledger", "w1"))):
            if name.endswith(".json"):
                d = core.load_json_strict(os.path.join(REPO, "ledger", "w1", name))
                self.assertEqual(verifier.verify_liveness(d["slot_id"], d, core.ledger_clock(REPO)),
                                 "no heartbeat declared")


class RecordUpdateMovesTheWitness(unittest.TestCase):
    def test_a_recorded_fee_witnesses_the_block(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "w1-b0042.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(core.render(beating()))
            rpc = FakeRpc().settle(TX_FEE1, SELLER, USDC // 2)
            self.assertEqual(record_update.main([path, TX_FEE1, "--at", "2026-11-01T09:00:00Z",
                                                 "--prev-bundle", "ab" * 32, "--write"], rpc=rpc), 0)
            d = core.load_json_strict(path)
            self.assertEqual(d["last_witnessed_at"], "2026-11-01T09:00:00Z")
            verifier.validate_dossier(d)
            self.assertIn("present", verifier.verify_liveness("w1-b0042", d, "2026-11-05T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
