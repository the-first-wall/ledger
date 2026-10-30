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

TOP = {"schema", "root", "as_of_block", "ledger_commit", "anchor", "slots", "checks", "services"}
CLOCK = lambda: "2026-10-30T12:00:00Z"  # noqa: E731


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def _no_network(*a, **k):
    raise AssertionError("network used")


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        for rel in ("state.json", "permissions.json", "skill.md", "verification.json", "ledger/index.json",
                    "ledger/anchors.json", "discovery/agent-card.json", "discovery/agent-registration.json"):
            os.makedirs(os.path.dirname(os.path.join(self.td, rel)) or self.td, exist_ok=True)
            shutil.copy(os.path.join(REPO, rel), os.path.join(self.td, rel))

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

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
        self.assertEqual(doc["root"], json.loads(_read("state.json"))["merkle_root"])

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
        self.assertTrue(all(c["status"] == "pass" for c in a["checks"]), a["checks"])
        self.assertFalse(any(c["id"].startswith("http_probe.") for c in a["checks"]))

    def test_probe_uses_a_real_get_and_stamps_it(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if "x402" in url:
                body = b'{"x402Version": 2}'
            else:
                body = json.dumps({"merkle_root": json.loads(_read("state.json"))["merkle_root"]}).encode()
            return {"http_status": 200, "sha256": "a" * 64, "bytes": len(body), "body": body}

        doc = ver.build(REPO, probe=True, fetch=fetch, clock=CLOCK, commit="y" * 40)
        self.assertEqual(sorted(calls), sorted(u for _, u in ver.SERVICES))
        probes = {c["id"]: c for c in doc["checks"] if c["id"].startswith("http_probe.")}
        self.assertEqual({c["status"] for c in probes.values()}, {"pass"})
        self.assertTrue(all(s["status"] == "up" and s["checked_at"] == CLOCK() for s in doc["services"]))

    def test_failed_probe_is_recorded_as_down(self):
        doc = ver.build(REPO, probe=True, fetch=lambda u: {"error": "URLError: down"}, clock=CLOCK, commit="z" * 40)
        self.assertEqual({s["status"] for s in doc["services"]}, {"down"})
        self.assertIn("down", [c for c in doc["checks"] if c["id"].startswith("http_probe.")][0]["evidence"]["error"])


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

    def test_missing_file_fails(self):
        os.remove(os.path.join(self.td, "verification.json"))
        self.assertTrue(ver.check(self.td))


class Workflow(unittest.TestCase):
    def test_probe_runs_in_its_own_scheduled_job(self):
        wf = _read(".github/workflows/verification_probe.yml")
        for needle in ("cron:", "python scripts/verification.py --probe", "verification.json"):
            self.assertIn(needle, wf)
        self.assertNotIn("--probe", _read(".github/workflows/self_check.yml"))


if __name__ == "__main__":
    unittest.main()
