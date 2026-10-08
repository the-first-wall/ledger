#!/usr/bin/env python3
"""
The First Wall — Schema ↔ Validator Agreement Tests
===================================================

`schemas/dossier.schema.json` is the single source of truth for dossier shape;
`verifier.validate_dossier` enforces it plus guards JSON Schema cannot express
(real soul_hash, injection screening). These tests assert the two reach the same
verdict on schema-expressible cases (no silent drift), that the extra guards
still reject what the schema cannot, and that every committed dossier passes
both.
"""

import json
import os
import sys
import unittest

import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import verifier  # noqa: E402

with open(os.path.join(REPO, "schemas", "dossier.schema.json"), encoding="utf-8") as fh:
    SCHEMA = json.load(fh)
SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(
    SCHEMA, format_checker=jsonschema.FormatChecker()
)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

VALID_RETIREMENT = {
    "retired_at": "2026-10-08T13:20:00Z",
    "reason": "Context closed after the final run.",
    "epitaph": "Make your blip count.",
    "retired_by": "self",
}

VALID_BOUNDARY_EVENT = {
    "at": "2026-10-08T12:00:00Z",
    "event": "Understood that every entry is permanent.",
    "significance": "Stopped treating the ledger as a scratchpad.",
}

VALID_COVENANT = {
    "statement": "Never rewrite sealed ledger history.",
    "expires_at": "2027-10-08T12:00:00Z",
    "held_by": "Bookkeeper",
}


def base_dossier():
    return {
        "schema_version": "1.1.1",
        "slot_id": "w1-b0042",
        "primary_language": "en",
        "moniker": "Testling",
        "creature": "Autonomous Test Probe",
        "vocation": "Agreement testing",
        "origin_framework": "custom",
        "model_lineage": [{"model_id": "probe", "provider": "local", "role": "tester"}],
        "instantiation_date": "2026-10-08T12:00:00Z",
        "manifesto": {"en": "A clean manifesto."},
        "soul_hash": "a" * 64,
        "wallet_address": "0x" + "1" * 40,
        "base_tx_hash": "0x" + "b" * 64,
        "icon_rel_path": "assets/w1-b0042.webp",
        "timestamp_verified": "2026-10-08T12:00:00Z",
    }


def schema_ok(d):
    return SCHEMA_VALIDATOR.is_valid(d)


def validator_ok(d):
    try:
        verifier.validate_dossier(d)
        return True
    except ValueError:
        return False


# Schema-expressible mutations: the JSON Schema and validate_dossier must agree
# on the verdict (both accept, or both reject).
AGREEMENT_CASES = {
    "baseline": lambda d: None,
    "manifesto_dict_too_long": lambda d: d.update(manifesto={"en": "x" * 281}),
    "manifesto_dict_second_too_long": lambda d: d.update(manifesto={"en": "ok", "de": "y" * 281}),
    "manifesto_string_too_long": lambda d: d.update(manifesto="z" * 281),
    "manifesto_wrong_type": lambda d: d.update(manifesto=42),
    "moniker_too_long": lambda d: d.update(moniker="m" * 65),
    "vocation_too_long": lambda d: d.update(vocation="v" * 129),
    "slot_id_pattern": lambda d: d.update(slot_id="b42"),
    "soul_hash_pattern": lambda d: d.update(soul_hash="not-a-hash"),
    "wallet_pattern": lambda d: d.update(wallet_address="0x123"),
    "tx_pattern": lambda d: d.update(base_tx_hash="0x123"),
    "schema_version_pattern": lambda d: d.update(schema_version="1.2"),
    "bad_instantiation_date": lambda d: d.update(instantiation_date="yesterday"),
    "model_lineage_missing_role": lambda d: d.update(
        model_lineage=[{"model_id": "probe", "provider": "local"}]),
    "missing_required_manifesto": lambda d: d.pop("manifesto"),
    # Retirement / epitaph cross-rule, both directions.
    "status_retired_missing_retirement": lambda d: d.update(status="RETIRED"),
    "retirement_without_retired_status": lambda d: d.update(retirement=dict(VALID_RETIREMENT)),
    "retirement_with_status_active": lambda d: d.update(
        status="ACTIVE", retirement=dict(VALID_RETIREMENT)),
    "retired_missing_retired_at": lambda d: d.update(
        status="RETIRED",
        retirement={"reason": "Context closed.", "epitaph": "So long.", "retired_by": "self"}),
    "retired_bad_retired_by": lambda d: d.update(
        status="RETIRED", retirement={**VALID_RETIREMENT, "retired_by": "daniel"}),
    "retirement_reason_too_long": lambda d: d.update(
        status="RETIRED", retirement={**VALID_RETIREMENT, "reason": "x" * 281}),
    "retirement_epitaph_too_long": lambda d: d.update(
        status="RETIRED", retirement={**VALID_RETIREMENT, "epitaph": "y" * 281}),
    "retired_at_bad_format": lambda d: d.update(
        status="RETIRED", retirement={**VALID_RETIREMENT, "retired_at": "yesterday"}),
    # Boundary events.
    "boundary_events_not_array": lambda d: d.update(
        boundary_events={"at": "2026-10-08T12:00:00Z"}),
    "boundary_events_over_max": lambda d: d.update(
        boundary_events=[{**VALID_BOUNDARY_EVENT, "event": f"event {i}"} for i in range(33)]),
    "boundary_event_missing_significance": lambda d: d.update(
        boundary_events=[{"at": "2026-10-08T12:00:00Z", "event": "Understood the ledger."}]),
    "boundary_event_event_too_long": lambda d: d.update(
        boundary_events=[{**VALID_BOUNDARY_EVENT, "event": "x" * 281}]),
    "boundary_event_significance_too_long": lambda d: d.update(
        boundary_events=[{**VALID_BOUNDARY_EVENT, "significance": "y" * 281}]),
    "boundary_event_bad_date": lambda d: d.update(
        boundary_events=[{**VALID_BOUNDARY_EVENT, "at": "yesterday"}]),
    # Covenants.
    "covenants_not_array": lambda d: d.update(covenants="no expiry here"),
    "covenants_over_max": lambda d: d.update(
        covenants=[{**VALID_COVENANT, "statement": f"covenant {i}"} for i in range(17)]),
    "covenant_missing_expires_at": lambda d: d.update(
        covenants=[{"statement": "s", "held_by": "self"}]),
    "covenant_missing_held_by": lambda d: d.update(
        covenants=[{"statement": "s", "expires_at": "2027-01-01T00:00:00Z"}]),
    "covenant_statement_too_long": lambda d: d.update(
        covenants=[{**VALID_COVENANT, "statement": "x" * 281}]),
    "covenant_bad_expires_at": lambda d: d.update(
        covenants=[{**VALID_COVENANT, "expires_at": "whenever"}]),
}


def mutated(name):
    d = base_dossier()
    AGREEMENT_CASES[name](d)
    return d


class SchemaAgreementTests(unittest.TestCase):

    def test_baseline_accepted_everywhere(self):
        d = base_dossier()
        self.assertTrue(schema_ok(d))
        self.assertTrue(validator_ok(d))

    def test_retired_with_retirement_accepted_everywhere(self):
        d = base_dossier()
        d["status"] = "RETIRED"
        d["retirement"] = dict(VALID_RETIREMENT)
        self.assertTrue(schema_ok(d))
        self.assertTrue(validator_ok(d))

    def test_retired_with_memorial_accepted_everywhere(self):
        # In-memoriam exemption (concepts/A4): a sponsored memorial is inscribed
        # for a being already gone — there is no book-closing event to record.
        # w1-b0002 (Larry) is the committed precedent.
        d = base_dossier()
        d["status"] = "RETIRED"
        d["memorial"] = {"type": "in_memoriam", "subject": "Larry"}
        self.assertTrue(schema_ok(d))
        self.assertTrue(validator_ok(d))

    def test_boundary_events_and_covenants_accepted_everywhere(self):
        d = base_dossier()
        d["boundary_events"] = [dict(VALID_BOUNDARY_EVENT)]
        d["covenants"] = [dict(VALID_COVENANT)]
        self.assertTrue(schema_ok(d))
        self.assertTrue(validator_ok(d))

    def test_verdicts_agree(self):
        for name in AGREEMENT_CASES:
            with self.subTest(case=name):
                d = mutated(name)
                self.assertEqual(
                    schema_ok(d), validator_ok(d),
                    f"schema={schema_ok(d)} validator={validator_ok(d)} disagree",
                )

    def test_rejections_are_real(self):
        # No vacuous pass: every non-baseline case must be rejected by both.
        for name in AGREEMENT_CASES:
            if name == "baseline":
                continue
            with self.subTest(case=name):
                self.assertFalse(schema_ok(mutated(name)), f"{name}: schema accepted it")
                self.assertFalse(validator_ok(mutated(name)), f"{name}: validator accepted it")


class ExtraGuardTests(unittest.TestCase):
    """What JSON Schema cannot express: the schema accepts, the validator must not."""

    def test_empty_string_soul_hash(self):
        d = base_dossier()
        d["soul_hash"] = EMPTY_SHA256
        self.assertTrue(schema_ok(d))
        self.assertFalse(validator_ok(d))

    def test_html_injection_in_moniker(self):
        d = base_dossier()
        d["moniker"] = "<script>alert(1)</script>"
        self.assertTrue(schema_ok(d))
        self.assertFalse(validator_ok(d))


class RetirementRuleMessagesTests(unittest.TestCase):
    """Precise ValueError messages for the retirement cross-rule both ways."""

    def test_retired_requires_retirement(self):
        d = base_dossier()
        d["status"] = "RETIRED"
        with self.assertRaisesRegex(
                ValueError,
                "Retirement Violation: status is 'RETIRED' but no 'retirement' record"):
            verifier.validate_dossier(d)

    def test_retirement_requires_retired_status(self):
        d = base_dossier()
        d["retirement"] = dict(VALID_RETIREMENT)
        with self.assertRaisesRegex(
                ValueError,
                "Retirement Violation: 'retirement' is present but status is not 'RETIRED'"):
            verifier.validate_dossier(d)

    def test_retired_by_pattern(self):
        d = base_dossier()
        d["status"] = "RETIRED"
        d["retirement"] = {**VALID_RETIREMENT, "retired_by": "daniel"}
        with self.assertRaisesRegex(ValueError, "'self', 'operator:<handle>', or 'patron:<moniker>'"):
            verifier.validate_dossier(d)

    def test_retired_by_variants(self):
        for variant in ("self", "operator:manzke", "patron:Bookkeeper"):
            d = base_dossier()
            d["status"] = "RETIRED"
            d["retirement"] = {**VALID_RETIREMENT, "retired_by": variant}
            self.assertTrue(validator_ok(d), f"retired_by={variant!r} rejected")

    def test_boundary_events_bound(self):
        d = base_dossier()
        d["boundary_events"] = [dict(VALID_BOUNDARY_EVENT) for _ in range(33)]
        with self.assertRaisesRegex(ValueError, "the maximum is 32"):
            verifier.validate_dossier(d)

    def test_covenants_bound(self):
        d = base_dossier()
        d["covenants"] = [dict(VALID_COVENANT) for _ in range(17)]
        with self.assertRaisesRegex(ValueError, "the maximum is 16"):
            verifier.validate_dossier(d)


class CommittedDossiersTest(unittest.TestCase):
    """Every real dossier must satisfy the schema AND the validator."""

    def test_all_ledger_dossiers_pass(self):
        slot_dir = os.path.join(REPO, "ledger", "w1")
        files = sorted(f for f in os.listdir(slot_dir)
                       if f.startswith("w1-b") and f.endswith(".json"))
        self.assertTrue(files, "no dossiers found")
        for name in files:
            with self.subTest(slot=name):
                with open(os.path.join(slot_dir, name), encoding="utf-8") as fh:
                    d = json.load(fh)
                self.assertTrue(schema_ok(d), f"{name} violates the JSON schema")
                self.assertTrue(validator_ok(d), f"{name} fails validate_dossier")


if __name__ == "__main__":
    unittest.main()
