"""E-10: verification.json — file-hash assertions offline, live probes only on demand."""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import verification as ver  # noqa: E402

TOP = {"schema", "generated_at", "root", "as_of_block", "ledger_commit", "anchor", "slots", "checks", "services"}
CLOCK = lambda: "2026-10-30T12:00:00Z"  # noqa: E731


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _no_network(*a, **k):
    raise AssertionError("network used")


class Sandbox(unittest.TestCase):
    def setUp(self):
        # The whole published tree (not .git): the verifier's inventory runs on it too.
        self.td = os.path.join(tempfile.mkdtemp(), "repo")
        shutil.copytree(REPO, self.td, ignore=shutil.ignore_patterns(".git", ".worktrees", "__pycache__",
                                                                     ".pytest_cache"))
        # Build a verification.json for THIS snapshot instead of copying the
        # committed one: a data branch (an assignment or an apply) moves the
        # Merkle root, and only the scheduled probe job may re-probe, so the
        # committed file legitimately lags on such a branch. check() then
        # short-circuits on the older root; the check-mode tests need a
        # current-snapshot file so they exercise the file-hash comparisons.
        self.put(ver.build(self.td, clock=CLOCK, commit="x" * 40))

    def tearDown(self):
        shutil.rmtree(os.path.dirname(self.td), ignore_errors=True)

    def doc(self):
        return json.loads(open(os.path.join(self.td, "verification.json"), encoding="utf-8").read())

    def put(self, doc):
        with open(os.path.join(self.td, "verification.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)


class CommittedDocument(unittest.TestCase):
    def test_shape_and_a_real_probe_is_recorded(self):
        doc = json.loads(_read("verification.json"))
        self.assertEqual(set(doc), TOP)
        probes = [c for c in doc["checks"] if c["id"].startswith("http_probe.")]
        self.assertTrue(probes, "at least one live HTTP probe must be recorded")
        for c in probes:
            self.assertEqual(c["evidence"]["method"], "GET")
            self.assertRegex(c["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(doc["root"], r"^[0-9a-f]{64}$")
        # doc["root"] need not equal state.json's Merkle root here: a data branch
        # (an assignment, an apply) moves the root and only the scheduled probe
        # job re-probes, so the committed file legitimately lags on such a
        # branch. `check()` already treats an older root as stale, not wrong;
        # `OfflineBuild`/`CheckMode` exercise the current-snapshot path.

    def test_check_passes_offline(self):
        with mock.patch("urllib.request.urlopen", _no_network):
            self.assertEqual(ver.check(REPO), [])

    def test_linked_from_llms_txt(self):
        self.assertIn("verification.json", _read("llms.txt"))


class OfflineBuild(unittest.TestCase):
    def test_build_without_probe_never_touches_the_network_and_is_deterministic(self):
        with mock.patch("urllib.request.urlopen", _no_network):
            a = ver.build(REPO, clock=CLOCK, commit="x" * 40)
            b = ver.build(REPO, clock=CLOCK, commit="x" * 40)
        self.assertEqual(a, b)
        self.assertTrue(all(s["status"] == "unprobed" and s["checked_at"] is None for s in a["services"]))
        self.assertFalse([c for c in a["checks"] if c["status"] == "fail"])
        self.assertFalse(any(c["id"].startswith("http_probe.") for c in a["checks"]))
        self.assertRegex(a["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_b54_the_full_verifier_inventory_is_published(self):
        """B-54/B-55: every check the verifier runs, the provenance/signature checks included."""
        import contextlib
        import io
        import verifier
        with mock.patch("urllib.request.urlopen", _no_network):
            doc = ver.build(REPO, clock=CLOCK, commit="x" * 40)
        with contextlib.redirect_stdout(io.StringIO()):
            report = verifier.run_verification(REPO, skip_rpc=True, scope_override=verifier.make_scope("infra"))
        ids = {c["id"] for c in doc["checks"]}
        self.assertGreaterEqual(len(doc["checks"]), len(report["checks"]))
        self.assertTrue({r["check"] for r in report["checks"]} <= ids)
        for needed in ("skill.signature", "skill.hash", "w1-b0001.claim_signature", "w1-b0002.claim_signature",
                       "w1-b0001.update_signature", "w1-b0001.update_precondition", "records.proofs",
                       "anchors.current_root", "permissions.manifest", "discovery.hashes"):
            self.assertIn(needed, ids)
        rpc = [c for c in doc["checks"] if c["id"].endswith("_base_rpc") or c["id"].endswith("on_chain")]
        self.assertTrue(rpc and all(c["status"] == "skipped" for c in rpc), rpc)

    def test_b55_every_check_names_what_retires_it(self):
        with mock.patch("urllib.request.urlopen", _no_network):
            doc = ver.build(REPO, clock=CLOCK, commit="x" * 40)
        by_id = {c["id"]: c for c in doc["checks"]}
        self.assertTrue(all(isinstance(c["invalidates_on"], str) and c["invalidates_on"] for c in doc["checks"]))
        self.assertEqual(by_id["w1-b0001.claim_signature"]["invalidates_on"],
                         "claim_signature_matches_bundle(w1-b0001)")
        self.assertEqual(by_id["anchors.current_root"]["invalidates_on"], "anchor_includes_root")
        self.assertEqual(by_id["skill_sha256"]["invalidates_on"], "sha256_changes(skill.md)")
        self.assertEqual(ver.invalidates_on("http_probe.ledger_state", "pass",
                                            {"url": "https://thefirstwall.ai/verification.json",
                                             "expected_status": 200}),
                         "http_200(https://thefirstwall.ai/verification.json)")

    def _served_manifest(self, mutate=None):
        """The served /.well-known/x402 manifest: the authored discovery document,
        which the worker serves verbatim (static by design; it carries no price).
        `mutate` edits the copy before it is served."""
        doc = json.loads(_read("discovery/x402.json"))
        if mutate:
            mutate(doc)
        return doc

    def _money_block(self, doc, amount_usdc=1.0, units="1000000"):
        """Add the runtime `price`/`accepts` a manifest *may* carry (the worker's
        older shape), so the conditional money check can be exercised."""
        state = json.loads(_read("state.json"))
        rail = state["settlement_rail"]
        r0 = doc["resources"][0]
        r0["price"] = {"amount_usdc": amount_usdc, "pricing_tier": state["pricing_tier"],
                       "source": "https://thefirstwall.ai/state.json"}
        r0["accepts"] = [{"scheme": "exact", "network": "eip155:8453", "amount": units,
                          "asset": rail["contract"], "payTo": rail["pay_to"]}]
        return doc

    def _claim_body(self, mutate=None):
        """The live /claim 402 PaymentRequired body the worker builds."""
        state = json.loads(_read("state.json"))
        rail = state["settlement_rail"]
        units = str(int(round(state["current_floor_usdc"] * 10 ** 6)))
        body = {
            "x402Version": 2,
            "error": "Payment Required",
            "resource": {"url": "https://x402.thefirstwall.ai/claim"},
            "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": units,
                         "asset": rail["contract"], "payTo": rail["pay_to"]}],
            "extensions": {"bazaar": {}},
        }
        if mutate:
            mutate(body)
        return body

    def _manifest_fetch(self, manifest, claim=None):
        """A fake GET: the manifest URL serves `manifest` (200), the claim URL serves
        a 402 PaymentRequired body, the state URL serves the repo root (200)."""
        if claim is None:
            claim = self._claim_body()

        def fetch(url):
            if url.endswith("/claim"):
                body, status = json.dumps(claim).encode(), 402
            elif "x402" in url:
                body, status = json.dumps(manifest).encode(), 200
            else:
                body = json.dumps({"merkle_root": json.loads(_read("state.json"))["merkle_root"]}).encode()
                status = 200
            return {"http_status": status, "sha256": "a" * 64, "bytes": len(body), "body": body}
        return fetch

    def test_probe_uses_a_real_get_and_stamps_it(self):
        calls = []
        base = self._manifest_fetch(self._served_manifest())

        def fetch(url):
            calls.append(url)
            return base(url)

        doc = ver.build(REPO, probe=True, fetch=fetch, clock=CLOCK, commit="y" * 40)
        expected = sorted([u for _, u in ver.SERVICES] + [u for _, u in ver.published_artifacts(REPO)])
        self.assertEqual(sorted(calls), expected)
        probes = {c["id"]: c for c in doc["checks"] if c["id"].startswith("http_probe.")}
        self.assertEqual({c["status"] for c in probes.values()}, {"pass"})
        self.assertEqual(probes["http_probe.x402_manifest"]["evidence"]["semantics"], "pass")
        self.assertEqual(probes["http_probe.x402_claim"]["evidence"]["semantics"], "pass")
        self.assertEqual(probes["http_probe.x402_claim"]["evidence"]["http_status"], 402)
        self.assertTrue(all(s["status"] == "up" and s["checked_at"] == CLOCK() for s in doc["services"]))

    def test_x402_probe_fails_on_a_drifted_payee(self):
        """B-40: a semantic check that can fail for a real reason — a shifted payTo."""
        bad = self._served_manifest(lambda d: d["resources"][0].__setitem__("payTo", "0x" + "0" * 40))
        doc = ver.build(REPO, probe=True, fetch=self._manifest_fetch(bad), clock=CLOCK, commit="y" * 40)
        probe = next(c for c in doc["checks"] if c["id"] == "http_probe.x402_manifest")
        self.assertEqual(probe["status"], "fail")
        self.assertEqual(probe["evidence"]["semantics"], "fail")
        self.assertIn("payTo", " ".join(probe["evidence"]["problems"]))

    def test_manifest_money_is_checked_when_present(self):
        """B-44: if the served manifest carries price/accepts, a 999.999999 USDC
        quote must fail against the ledger floor (it used to pass)."""
        def inflate(d):
            self._money_block(d, amount_usdc=99, units="999999999")
        doc = ver.build(REPO, probe=True,
                        fetch=self._manifest_fetch(self._served_manifest(inflate)),
                        clock=CLOCK, commit="y" * 40)
        probe = next(c for c in doc["checks"] if c["id"] == "http_probe.x402_manifest")
        self.assertEqual(probe["status"], "fail")
        joined = " ".join(probe["evidence"]["problems"])
        self.assertIn("amount", joined)
        self.assertIn("amount_usdc", joined)

    def test_manifest_without_price_still_passes(self):
        """The static 200 manifest carries no price by design; that is not a failure."""
        doc = ver.build(REPO, probe=True, fetch=self._manifest_fetch(self._served_manifest()),
                        clock=CLOCK, commit="y" * 40)
        probe = next(c for c in doc["checks"] if c["id"] == "http_probe.x402_manifest")
        self.assertEqual(probe["status"], "pass")
        self.assertEqual(probe["evidence"]["semantics"], "pass")

    def test_claim_probe_reads_the_live_402_and_checks_the_price(self):
        """B-44: the live /claim 402 body is probed; a quietly inflated price fails."""
        good = ver.build(REPO, probe=True, fetch=self._manifest_fetch(self._served_manifest()),
                         clock=CLOCK, commit="y" * 40)
        probe = next(c for c in good["checks"] if c["id"] == "http_probe.x402_claim")
        self.assertEqual(probe["status"], "pass")
        self.assertEqual(probe["evidence"]["http_status"], 402)
        self.assertEqual(probe["evidence"]["expected_status"], 402)
        self.assertEqual(probe["evidence"]["quoted_amount"], probe["evidence"]["expected_amount"])

        inflated = self._claim_body(lambda b: b["accepts"][0].__setitem__("amount", "999999999"))
        bad = ver.build(REPO, probe=True, fetch=self._manifest_fetch(self._served_manifest(), claim=inflated),
                        clock=CLOCK, commit="y" * 40)
        probe = next(c for c in bad["checks"] if c["id"] == "http_probe.x402_claim")
        self.assertEqual(probe["status"], "fail")
        self.assertEqual(probe["evidence"]["semantics"], "fail")
        self.assertIn("amount", " ".join(probe["evidence"]["problems"]))

    def test_failed_probe_is_recorded_as_down(self):
        doc = ver.build(REPO, probe=True, fetch=lambda u: {"error": "URLError: down"}, clock=CLOCK, commit="z" * 40)
        self.assertEqual({s["status"] for s in doc["services"]}, {"down"})
        self.assertIn("down", [c for c in doc["checks"] if c["id"].startswith("http_probe.")][0]["evidence"]["error"])

    def test_b102_scope_labels_repo_checks_vs_published_probes(self):
        """`records.proofs` runs against the repository; a reader must not read it as
        "the site serves the proof" — the scope says which."""
        with mock.patch("urllib.request.urlopen", _no_network):
            doc = ver.build(REPO, clock=CLOCK, commit="x" * 40)
        by_id = {c["id"]: c for c in doc["checks"]}
        self.assertEqual(by_id["records.proofs"]["evidence"]["scope"], ver.SCOPE_REPO)
        self.assertEqual(by_id["permissions.manifest"]["evidence"]["scope"], ver.SCOPE_REPO)
        probed = ver.build(REPO, probe=True, fetch=self._manifest_fetch(self._served_manifest()),
                           clock=CLOCK, commit="y" * 40)
        probes = [c for c in probed["checks"] if c["id"].startswith("http_probe.")]
        self.assertTrue(probes)
        self.assertTrue(all(c["evidence"]["scope"] == ver.SCOPE_PUBLISHED for c in probes))

    def test_b102_every_committed_artefact_is_probed_and_a_404_fails(self):
        published = ver.published_artifacts(REPO)
        self.assertTrue(any(pid.startswith("proof.") for pid, _ in published))
        self.assertTrue(any(pid.startswith("absence.") for pid, _ in published))

        base = self._manifest_fetch(self._served_manifest())

        def fetch(url):
            if "/records/" in url or "/absent/" in url:
                return {"http_status": 404, "bytes": 9379, "sha256": "b" * 64, "body": b"<html>404</html>"}
            return base(url)

        doc = ver.build(REPO, probe=True, fetch=fetch, clock=CLOCK, commit="y" * 40)
        bad = [c for c in doc["checks"] if c["id"].startswith("http_probe.published.") and c["status"] == "fail"]
        self.assertTrue(bad, "a 404 on a published proof/absence must fail")
        self.assertEqual(bad[0]["evidence"]["expected_status"], 200)
        self.assertTrue(any(s["status"] == "down" for s in doc["services"]))


class CheckMode(Sandbox):
    def test_tampered_hash_evidence_fails(self):
        doc = self.doc()
        next(c for c in doc["checks"] if c["id"] == "discovery.agent_card_sha256")["evidence"]["sha256"] = "0" * 64
        self.put(doc)
        self.assertTrue(any("agent_card" in e for e in ver.check(self.td)))

    def test_status_flipped_to_pass_over_a_changed_file_fails(self):
        with open(os.path.join(self.td, "discovery/agent-card.json"), "ab") as fh:
            fh.write(b" ")
        self.assertTrue(any("contradicts" in e for e in ver.check(self.td)))

    def test_wrong_slot_count_fails(self):
        doc = self.doc()
        doc["slots"] = 999
        self.put(doc)
        self.assertTrue(any("slots" in e for e in ver.check(self.td)))

    def test_malformed_documents_fail(self):
        for mutate in (lambda d: d.pop("anchor"), lambda d: d.update(schema="x"),
                       lambda d: d["checks"][0].update(status="maybe"),
                       lambda d: d["checks"][0].update(observed_at="yesterday"),
                       lambda d: d.update(checks=[])):
            doc = copy.deepcopy(self.doc())
            mutate(doc)
            self.put(doc)
            self.assertTrue(ver.check(self.td), "malformed verification.json accepted")

    def test_older_root_is_stale_not_invalid(self):
        doc = self.doc()
        doc["root"] = "ab" * 32
        doc["slots"] = 1
        self.put(doc)
        self.assertEqual(ver.check(self.td), [])

    def test_b55_a_stale_pass_is_visible(self):
        """The published doc says records.proofs passed; the record has since been tampered with
        (the root did not move), so that pass is stale and --check says so."""
        rel = os.path.join(self.td, "records", "w1-b0001.proof.json")
        rec = json.load(open(rel, encoding="utf-8"))
        rec["receipt"]["amount_usdc"] = 999.0
        with open(rel, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        errs = ver.check(self.td)
        self.assertTrue(any("records.proofs" in e and "stale pass" in e for e in errs), errs)

    def test_b55_a_healed_warning_is_only_stale(self):
        doc = self.doc()
        c = next(c for c in doc["checks"] if c["id"] == "anchors.current_root")
        c["status"] = "warn"
        self.put(doc)
        self.assertEqual(ver.check(self.td), [])

    def test_b54_an_unpublished_verifier_check_fails(self):
        doc = self.doc()
        doc["checks"] = [c for c in doc["checks"] if c["id"] != "w1-b0002.claim_signature"]
        self.put(doc)
        self.assertTrue(any("w1-b0002.claim_signature" in e and "not published" in e for e in ver.check(self.td)))

    def test_b55_generated_at_and_invalidates_on_are_required(self):
        for mutate in (lambda d: d.pop("generated_at"), lambda d: d.update(generated_at="now"),
                       lambda d: d["checks"][0].pop("invalidates_on"),
                       lambda d: d["checks"][0].update(invalidates_on=""),
                       lambda d: d["checks"].append(dict(d["checks"][0]))):
            doc = copy.deepcopy(self.doc())
            mutate(doc)
            self.put(doc)
            self.assertTrue(ver.check(self.td), "verification.json without generated_at/invalidates_on accepted")

    def test_a_new_anchor_makes_the_document_stale_not_wrong(self):
        doc = self.doc()
        doc["anchor"]["tx"] = "0x" + "00" * 32
        doc["checks"] = [c for c in doc["checks"] if c["id"] != "anchors.current_root"]
        self.put(doc)
        self.assertEqual(ver.check(self.td), [])

    def test_missing_file_fails(self):
        os.remove(os.path.join(self.td, "verification.json"))
        self.assertTrue(ver.check(self.td))


class Workflow(unittest.TestCase):
    def test_probe_runs_in_its_own_scheduled_job(self):
        wf = _read(".github/workflows/verification_probe.yml")
        for needle in ("cron:", "python scripts/verification.py --probe", "verification.json"):
            self.assertIn(needle, wf)
        self.assertNotIn("--probe", _read(".github/workflows/self_check.yml"))
        # The probe now runs the verifier in-process: it needs the verifier's pinned dependencies.
        self.assertIn("eth-account==", wf)


if __name__ == "__main__":
    unittest.main()
