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
