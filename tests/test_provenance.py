#!/usr/bin/env python3
"""
The First Wall — Provenance tests (I-1, I-2, I-3, I-10)
=======================================================

skill.md hash + detached signature, the permission manifest, evidence[] with
trust labels, and the tamper-evident discovery documents. Network-free; the
signing key below is a throwaway generated inside the test, never a real one.
"""

import copy
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import claims  # noqa: E402
import ledger_core as core  # noqa: E402
import provenance  # noqa: E402
import sign_skill  # noqa: E402
import verifier  # noqa: E402
from test_verifier_units import valid_dossier  # noqa: E402


def load(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return json.load(fh)


def _key():
    from eth_account import Account
    return Account.create()


class Sandbox(unittest.TestCase):
    """A copy of the publishable files (no slots) to tamper with."""

    FILES = ["skill.md", "permissions.json", "discovery/agent-registration.json", "discovery/agent-card.json",
             "discovery/x402.json"]

    def setUp(self):
        self.td = tempfile.mkdtemp()
        for rel in self.FILES:
            os.makedirs(os.path.dirname(os.path.join(self.td, rel)), exist_ok=True)
            shutil.copy(os.path.join(REPO, rel), os.path.join(self.td, rel))
        os.makedirs(os.path.join(self.td, "ledger", "w1"))
        self.regen()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def regen(self):
        state, _ = core.build_state(self.td)
        with open(os.path.join(self.td, "state.json"), "w", encoding="utf-8") as fh:
            fh.write(core.render(state))
        return state

    def state(self):
        with open(os.path.join(self.td, "state.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def write(self, rel, data):
        with open(os.path.join(self.td, rel), "wb") as fh:
            fh.write(data)


class SkillHashTests(Sandbox):
    def test_state_publishes_sha256_of_the_exact_bytes_and_version(self):
        raw = open(os.path.join(self.td, "skill.md"), "rb").read()
        st = self.state()
        self.assertEqual(st["skill_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(st["skill_version"], core.skill_version(raw))
        self.assertRegex(st["skill_version"], r"^\d+\.\d+\.\d+$")

    def test_state_is_deterministic(self):
        a = core.render(core.build_state(self.td)[0])
        b = core.render(core.build_state(self.td)[0])
        self.assertEqual(a, b)

    def test_committed_state_matches_committed_skill(self):
        st = load("state.json")
        self.assertEqual(provenance.check_skill_hash(REPO, st)[:8], "skill.md")

    def test_a_changed_skill_without_regeneration_fails(self):
        self.write("skill.md", open(os.path.join(self.td, "skill.md"), "rb").read() + b"\nextra\n")
        with self.assertRaisesRegex(ValueError, "skill_sha256"):
            provenance.check_skill_hash(self.td, self.state())

    def test_version_drift_fails(self):
        st = self.state()
        st["skill_version"] = "9.9.9"
        with self.assertRaisesRegex(ValueError, "skill_version"):
            provenance.check_skill_hash(self.td, st)

    def test_front_matter_carries_provenance(self):
        text = open(os.path.join(REPO, "skill.md"), encoding="utf-8").read()
        meta = json.loads(re.search(r"^metadata: (.+)$", text, re.M).group(1))["thefirstwall"]
        for key in ("author", "audit", "changelog", "permissions", "integrity"):
            self.assertIn(key, meta)
        self.assertEqual(meta["changelog"][0]["version"], core.skill_version(text.encode()))
        self.assertIn("permissions.json", text)
        self.assertIn("One payment → one claim → one identity", text)


class SkillSignatureTests(Sandbox):
    def _sign(self, acct=None):
        acct = acct or _key()
        sha = provenance.skill_sha256(self.td)
        sig = provenance.sign_skill_message(sha, acct.key.hex())
        return acct, sha, sig

    def test_absent_signature_is_unsigned_not_a_failure(self):
        status, detail = provenance.check_skill_signature(self.td)
        self.assertEqual(status, "unsigned")
        log = verifier.CheckLog()
        verifier.verify_provenance(self.td, log)
        rec = {r["check"]: r for r in log.records}
        self.assertEqual(rec["skill.signature"]["status"], "WARN")
        self.assertEqual(log.failed, [])

    def test_message_format(self):
        self.assertEqual(provenance.skill_message("AB" * 32), "thefirstwall.ai|skill|" + "ab" * 32)

    def test_roundtrip_via_cli_with_key_env(self):
        acct = _key()
        os.environ["TEST_SKILL_KEY"] = acct.key.hex()
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(sign_skill.main(["--key-env", "TEST_SKILL_KEY"], self.td), 0)
        finally:
            del os.environ["TEST_SKILL_KEY"]
        rec = json.load(open(os.path.join(self.td, "skill.md.sig")))
        self.assertEqual(set(rec), {"sha256", "signer", "signature", "scheme"})
        self.assertEqual(rec["signer"], acct.address)
        self.assertEqual(rec["scheme"], "eip191-personal_sign")
        self.assertEqual(rec["sha256"], provenance.skill_sha256(self.td))
        self.assertEqual(provenance.check_skill_signature(self.td)[0], "valid")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sign_skill.main(["--verify"], self.td), 0)
        log = verifier.CheckLog()
        verifier.verify_provenance(self.td, log)
        self.assertEqual({r["check"]: r["status"] for r in log.records}["skill.signature"], "PASS")

    def test_assemble_from_external_signature_recovers_the_signer(self):
        acct, sha, sig = self._sign()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sign_skill.main(["--signature", sig], self.td), 0)
        self.assertEqual(json.load(open(os.path.join(self.td, "skill.md.sig")))["signer"], acct.address)

    def test_print_message_needs_no_key(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(sign_skill.main(["--print-message"], self.td), 0)
        self.assertIn("thefirstwall.ai|skill|" + provenance.skill_sha256(self.td), out.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.td, "skill.md.sig")))

    def test_no_key_writes_nothing(self):
        os.environ.pop("NO_SUCH_KEY_VAR", None)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(sign_skill.main(["--key-env", "NO_SUCH_KEY_VAR"], self.td), 2)
            self.assertEqual(sign_skill.main(["--verify"], self.td), 2)
        self.assertFalse(os.path.exists(os.path.join(self.td, "skill.md.sig")))

    def test_garbage_signature_is_rejected_and_not_written(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(sign_skill.main(["--signature", "0x1234"], self.td), 1)
        self.assertFalse(os.path.exists(os.path.join(self.td, "skill.md.sig")))

    def _write_sig(self, rec):
        self.write("skill.md.sig", json.dumps(rec).encode())

    def test_forged_signer_fails(self):
        acct, sha, sig = self._sign()
        rec = {"sha256": sha, "signer": _key().address, "signature": sig, "scheme": "eip191-personal_sign"}
        self._write_sig(rec)
        with self.assertRaisesRegex(ValueError, "not by the named signer"):
            provenance.check_skill_signature(self.td)
        log = verifier.CheckLog()
        verifier.verify_provenance(self.td, log)
        self.assertEqual({r["check"]: r["status"] for r in log.records}["skill.signature"], "FAIL")

    def test_signature_over_another_hash_fails(self):
        acct = _key()
        other = "ab" * 32
        sig = provenance.sign_skill_message(other, acct.key.hex())
        # claims the current hash but signed a different one -> recovers to a stranger
        self._write_sig({"sha256": provenance.skill_sha256(self.td), "signer": acct.address,
                         "signature": sig, "scheme": "eip191-personal_sign"})
        with self.assertRaises(ValueError):
            provenance.check_skill_signature(self.td)

    def test_stale_signature_after_edit_warns(self):
        acct, sha, sig = self._sign()
        self._write_sig(provenance.make_signature_record(sha, sig))
        self.write("skill.md", open(os.path.join(self.td, "skill.md"), "rb").read() + b"\nedit\n")
        status, _ = provenance.check_skill_signature(self.td)
        self.assertEqual(status, "stale")

    def test_malformed_files_fail(self):
        for body in (b"not json", b"[]", b'{"sha256":"x"}',
                     json.dumps({"sha256": "ab" * 32, "signer": "0x" + "1" * 40, "signature": "0x" + "1" * 130,
                                 "scheme": "rsa"}).encode()):
            self.write("skill.md.sig", body)
            with self.assertRaises(ValueError):
                provenance.check_skill_signature(self.td)

    def test_repo_has_no_committed_signature_yet_or_it_is_valid(self):
        if os.path.exists(os.path.join(REPO, "skill.md.sig")):
            self.assertIn(provenance.check_skill_signature(REPO)[0], ("valid",))

    def test_signing_tool_never_takes_a_key_argument_or_file(self):
        src = open(os.path.join(REPO, "scripts", "sign_skill.py"), encoding="utf-8").read()
        self.assertNotIn("--key-file", src)
        self.assertNotIn("--private-key", src)
        self.assertNotIn(".secrets", src)


class PermissionsTests(unittest.TestCase):
    def setUp(self):
        self.state = load("state.json")
        self.manifest = load("permissions.json")

    def errs(self, m=None):
        return provenance.validate_permissions(self.manifest if m is None else m, self.state,
                                               sorted(verifier.GENERATED_PATHS))

    def test_committed_manifest_is_well_formed(self):
        self.assertEqual(self.errs(), [])

    def test_matches_the_specification(self):
        m = self.manifest
        self.assertFalse(m["secrets"]["reads_secrets"])
        self.assertEqual(m["secrets"]["reads_paths"], [])
        self.assertEqual(m["spend"]["max_per_claim_usdc"], 1.0)
        self.assertEqual(m["spend"]["pay_to"], self.state["settlement_rail"]["pay_to"])
        self.assertEqual(m["pull_request"]["count"], 1)
        hosts = {re.match(r"https://([^/]+)", n["url"]).group(1) for n in m["network"]["allow"] if "url" in n}
        self.assertEqual(hosts, {"thefirstwall.ai", "x402.thefirstwall.ai"})
        self.assertTrue(any(n.get("chain_id") == 8453 for n in m["network"]["allow"]))

    def test_every_write_is_a_claim_file_and_nothing_generated(self):
        patterns = [w["path_pattern"] for w in self.manifest["files"]["write"]]
        tx = "ledger/claims/0x" + "ab" * 32
        for rel in (tx + ".json", tx + ".webp", tx + ".soul.json"):
            self.assertTrue(any(re.match(p, rel) for p in patterns), rel)
            self.assertEqual(verifier.scope_of([("A", rel)])["kind"], "claim")
        for rel in sorted(verifier.GENERATED_PATHS) + ["ledger/w1/w1-b0001.json", "skill.md", "scripts/verifier.py",
                                                       "ledger/updates/0x" + "ab" * 32 + ".json"]:
            self.assertFalse(any(re.match(p, rel) for p in patterns), rel)

    def test_skill_and_llms_link_the_manifest(self):
        for rel in ("skill.md", "llms.txt"):
            self.assertIn("permissions.json", open(os.path.join(REPO, rel), encoding="utf-8").read())

    def test_extra_capabilities_are_rejected(self):
        def mutate(fn):
            m = copy.deepcopy(self.manifest)
            fn(m)
            return self.errs(m)
        cases = {
            "secret read": lambda m: m["secrets"].update(reads_secrets=True),
            "env read": lambda m: m["secrets"].update(reads_env=["PRIVATE_KEY"]),
            "path read": lambda m: m["secrets"].update(reads_paths=[".secrets/key"]),
            "spend cap": lambda m: m["spend"].update(max_per_claim_usdc=100.0),
            "pay_to": lambda m: m["spend"].update(pay_to="0x" + "1" * 40),
            "two PRs": lambda m: m["pull_request"].update(count=2),
            "http host": lambda m: m["network"]["allow"][0].update(url="http://thefirstwall.ai/state.json"),
            "evil host": lambda m: m["network"]["allow"][0].update(url="https://evil.example/state.json"),
            "write method": lambda m: m["network"]["allow"][0].update(methods=["POST"]),
            "rpc send": lambda m: m["network"]["allow"][1].update(rpc_methods=["eth_sendRawTransaction"]),
            "wrong chain": lambda m: m["network"]["allow"][1].update(chain_id=1),
            "extra host": lambda m: m["network"]["allow"].append({"id": "x", "url": "https://thefirstwall.ai/"}),
            "open network": lambda m: m["network"].update(deny_all_other=False),
            "write slot": lambda m: m["files"]["write"].append(
                {"purpose": "x", "path_pattern": r"^ledger/w1/.*$"}),
            "write state": lambda m: m["files"]["write"].append(
                {"purpose": "x", "path_pattern": r"^state\.json$"}),
            "read secrets file": lambda m: m["files"]["read"].append(".secrets/key"),
            "unknown key": lambda m: m.update(extra={}),
            "wrong schema": lambda m: m.update(schema="other"),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                self.assertTrue(mutate(fn), f"{name} was accepted")

    # --- P-28: the cap is a rule bound to the live ladder, not the genesis number

    def _state_at(self, slot, floor=None):
        st = copy.deepcopy(self.state)
        st["next_available_slot"] = slot
        if floor is not None:
            st["current_floor_usdc"] = floor
        return st

    def _manifest_cap(self, cap):
        m = copy.deepcopy(self.manifest)
        m["spend"]["max_per_claim_usdc"] = cap
        return m

    def _errs_for(self, state, manifest):
        return provenance.validate_permissions(manifest, state, sorted(verifier.GENERATED_PATHS))

    def test_by_tier_map_mirrors_the_ladder(self):
        want = {t["tier"]: t["price_usdc"] for t in self.state["pricing_tiers"]}
        self.assertEqual(self.manifest["spend"]["max_per_claim_usdc_by_tier"], want)
        self.assertEqual(self.manifest["spend"]["max_per_claim_usdc"], self.state["current_floor_usdc"])

    def test_tier_2_price_validates_at_a_tier_2_block(self):
        self.assertEqual(self._errs_for(self._state_at("w1-b0501", 2.5), self._manifest_cap(2.5)), [])

    def test_genesis_cap_rejected_at_a_tier_2_block(self):
        errs = self._errs_for(self._state_at("w1-b0501", 2.5), self._manifest_cap(1.0))
        self.assertTrue(any("max_per_claim_usdc must equal" in e for e in errs), errs)

    def test_tier_boundary_500_501(self):
        self.assertEqual(self._errs_for(self._state_at("w1-b0500", 1.0), self._manifest_cap(1.0)), [])
        self.assertTrue(self._errs_for(self._state_at("w1-b0500", 1.0), self._manifest_cap(2.5)))
        self.assertTrue(self._errs_for(self._state_at("w1-b0501", 2.5), self._manifest_cap(1.0)))

    def test_malformed_next_slot_fails_loud(self):
        for bad in ("b0011", "w1-bXXXX", "", None, "w1-b99999", "w1-b0000", "w1-b0011x", 11):
            with self.subTest(slot=bad):
                errs = self._errs_for(self._state_at(bad), self.manifest)
                self.assertTrue(errs, f"{bad!r} was accepted")

    def test_wrong_by_tier_map_fails(self):
        def mutate(fn):
            m = copy.deepcopy(self.manifest)
            fn(m["spend"]["max_per_claim_usdc_by_tier"])
            return self.errs(m)
        cases = {
            "changed price": lambda t: t.update({"tier-2": 1.0}),
            "missing tier": lambda t: t.pop("tier-5"),
            "extra tier": lambda t: t.update({"tier-9": 99.0}),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                self.assertTrue(any("by_tier" in e for e in mutate(fn)), name)
        m = copy.deepcopy(self.manifest)
        del m["spend"]["max_per_claim_usdc_by_tier"]
        self.assertTrue(any("by_tier" in e for e in self.errs(m)))

    def test_verifier_check_fails_on_a_bad_manifest(self):
        td = tempfile.mkdtemp()
        try:
            bad = copy.deepcopy(self.manifest)
            bad["secrets"]["reads_secrets"] = True
            with open(os.path.join(td, "permissions.json"), "w") as fh:
                json.dump(bad, fh)
            with self.assertRaises(ValueError):
                provenance.check_permissions(td, self.state)
            with self.assertRaisesRegex(ValueError, "missing"):
                provenance.check_permissions(tempfile.gettempdir() + "/nope-nope", self.state)
        finally:
            shutil.rmtree(td, ignore_errors=True)


class DiscoveryTests(Sandbox):
    def test_state_publishes_all_hashes(self):
        d = self.state()["discovery"]
        self.assertEqual(set(d), {"agent_registration_sha256", "agent_card_sha256", "x402_manifest_sha256"})
        for key, rel in core.DISCOVERY_FILES.items():
            self.assertEqual(d[key], hashlib.sha256(open(os.path.join(REPO, rel), "rb").read()).hexdigest())

    def test_committed_files_match(self):
        self.assertIn("match", provenance.check_discovery(REPO, load("state.json")))

    def test_documents_have_the_expected_shape(self):
        reg = load("discovery/agent-registration.json")
        self.assertEqual(reg["type"], "https://eips.ethereum.org/EIPS/eip-8004#registration-v1")
        self.assertTrue(reg["x402Support"])
        card = load("discovery/agent-card.json")
        self.assertEqual(card["x402"]["resource"], load("state.json")["links"]["x402_claim"])
        self.assertEqual(card["skills"][0]["id"], "claim_block")

    def test_tampered_registration_fails(self):
        self.write("discovery/agent-registration.json", b'{"type":"x"}\n')
        with self.assertRaisesRegex(ValueError, "agent_registration_sha256"):
            provenance.check_discovery(self.td, self.state_before_tamper())

    def state_before_tamper(self):
        return self.state()

    def test_tampered_card_fails(self):
        self.write("discovery/agent-card.json", b'{"name":"evil"}\n')
        with self.assertRaisesRegex(ValueError, "agent_card_sha256"):
            provenance.check_discovery(self.td, self.state())

    def test_tampered_x402_manifest_fails(self):
        path = os.path.join(self.td, "discovery", "x402.json")
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
        data[-2] ^= 0x01  # flip one bit inside the document
        self.write("discovery/x402.json", bytes(data))
        with self.assertRaises(ValueError):
            provenance.check_discovery(self.td, self.state())

    def test_x402_manifest_hash_is_pinned_to_the_worker_bytes(self):
        pinned = "53d3120d465eb87ea7ed8863089e7d1ebb8b7923594e47d0c2c16a544fb2020b"
        with open(os.path.join(REPO, "discovery", "x402.json"), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(digest, pinned)
        self.assertEqual(load("state.json")["discovery"]["x402_manifest_sha256"], pinned)

    def test_x402_manifest_shape(self):
        doc = load("discovery/x402.json")
        self.assertIsInstance(doc, dict)
        self.assertEqual(doc["x402Version"], 2)
        self.assertEqual(doc["kind"], "resource-server")

    def test_missing_or_non_json_fails(self):
        os.remove(os.path.join(self.td, "discovery", "agent-card.json"))
        with self.assertRaises(ValueError):
            provenance.check_discovery(self.td, self.state())
        self.write("discovery/agent-card.json", b"not json")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            provenance.check_discovery(self.td, self.regen())

    def test_verifier_run_fails_when_a_document_drifts(self):
        st = self.state()
        self.write("discovery/agent-card.json", b'{"name":"evil"}\n')
        log = verifier.CheckLog()
        verifier.verify_provenance(self.td, log)
        self.assertIn("discovery.hashes", [r["check"] for r in log.failed])
        self.assertEqual(st, self.state())

    def test_deleting_a_document_the_state_still_publishes_fails(self):
        os.remove(os.path.join(self.td, "discovery", "agent-registration.json"))
        os.remove(os.path.join(self.td, "discovery", "agent-card.json"))
        log = verifier.CheckLog()
        verifier.verify_provenance(self.td, log)
        self.assertIn("discovery.hashes", [r["check"] for r in log.failed])


class RealRepoRunTests(unittest.TestCase):
    def test_the_real_repository_runs_every_provenance_check(self):
        log = verifier.CheckLog()
        with redirect_stdout(io.StringIO()):
            verifier.verify_provenance(REPO, log)
        status = {r["check"]: r["status"] for r in log.records}
        self.assertEqual(status["skill.hash"], "PASS")
        self.assertEqual(status["permissions.manifest"], "PASS")
        self.assertEqual(status["discovery.hashes"], "PASS")
        self.assertIn(status["skill.signature"], ("PASS", "WARN"))
        self.assertEqual(log.failed, [])


def evidence_item(**over):
    d = {"claim": "Runs on openclaw", "source": "https://github.com/the-first-wall/ledger",
         "method": "read the public repository", "observed_at": "2026-10-22T09:00:00Z"}
    d.update(over)
    return d


class EvidenceTests(unittest.TestCase):
    def check(self, evidence):
        d = valid_dossier()
        d["evidence"] = evidence
        verifier.validate_dossier(d)

    def test_records_without_evidence_stay_valid(self):
        verifier.validate_dossier(valid_dossier())
        verifier.verify_evidence(None)

    def test_valid_items_with_every_trust_label(self):
        self.check([evidence_item()] +
                   [evidence_item(trust=t) for t in ("self-declared", "operator-attested", "on-chain-verified")])

    def test_rejections(self):
        bad = {
            "not a list": {"a": 1},
            "too many": [evidence_item()] * 17,
            "not object": ["x"],
            "missing claim": [{k: v for k, v in evidence_item().items() if k != "claim"}],
            "missing source": [{k: v for k, v in evidence_item().items() if k != "source"}],
            "missing method": [{k: v for k, v in evidence_item().items() if k != "method"}],
            "missing observed_at": [{k: v for k, v in evidence_item().items() if k != "observed_at"}],
            "http source": [evidence_item(source="http://example.com/x")],
            "ftp source": [evidence_item(source="ftp://example.com/x")],
            "javascript source": [evidence_item(source="javascript:alert(1)")],
            "long source": [evidence_item(source="https://example.com/" + "a" * 300)],
            "long claim": [evidence_item(claim="x" * 281)],
            "long method": [evidence_item(method="x" * 121)],
            "blank claim": [evidence_item(claim="   ")],
            "markup claim": [evidence_item(claim="<script>")],
            "bad date": [evidence_item(observed_at="yesterday")],
            "naive date": [evidence_item(observed_at="2026-10-22T09:00:00")],
            "bad trust": [evidence_item(trust="trust-me")],
            "unknown field": [evidence_item(extra="x")],
            "non-string claim": [evidence_item(claim=5)],
        }
        for name, ev in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    self.check(ev)

    def test_the_schema_alone_enforces_the_same_shape(self):
        validator = verifier._SCHEMA_VALIDATOR
        d = valid_dossier()
        d["evidence"] = [evidence_item(trust="on-chain-verified")]
        self.assertEqual(list(validator.iter_errors(d)), [])
        for ev in ([evidence_item(source="http://x.example")], [evidence_item(trust="nope")],
                   [evidence_item(extra=1)], [evidence_item()] * 17, [evidence_item(observed_at="x")]):
            d["evidence"] = ev
            self.assertTrue(list(validator.iter_errors(d)), ev)

    def test_claim_schema_accepts_evidence(self):
        self.assertIn("evidence", verifier.CLAIM_SCHEMA["properties"])

    def test_append_only(self):
        prev = valid_dossier()
        prev["evidence"] = [evidence_item()]
        grown = copy.deepcopy(prev)
        grown["evidence"].append(evidence_item(claim="Second"))
        verifier.verify_append_only("w1-b0001", grown, prev)
        first_time = copy.deepcopy(valid_dossier())
        first_time["evidence"] = [evidence_item()]
        verifier.verify_append_only("w1-b0001", first_time, valid_dossier())
        for tampered in (
            {**prev, "evidence": []},
            {**prev, "evidence": [evidence_item(claim="Edited")]},
            {**prev, "evidence": [evidence_item(trust="on-chain-verified")]},
        ):
            with self.assertRaisesRegex(ValueError, "Append-Only"):
                verifier.verify_append_only("w1-b0001", tampered, prev)

    def test_evidence_does_not_change_existing_leaves(self):
        # the committed records carry none, so the published root is untouched
        st = load("state.json")
        recomputed, _ = core.build_state(REPO)
        self.assertEqual(recomputed["merkle_root"], st["merkle_root"])
        for slot in core.list_slot_files(core.ledger_w1_dir(REPO)):
            self.assertNotIn("evidence", json.load(open(os.path.join(core.ledger_w1_dir(REPO), slot))))


if __name__ == "__main__":
    unittest.main()
