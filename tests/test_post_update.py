#!/usr/bin/env python3
"""
The First Wall — scripts/post_update.py
=======================================

An owner edits the slot in a clone as always; post_update.py turns the edit into a
request and puts the slot's files back, so the pull request holds the request and
nothing else.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import ledger_core as core  # noqa: E402
import post_update  # noqa: E402
import updates  # noqa: E402

from test_updates import SELLER, TX_FEE1, TX_FEE2, fee, inscribed  # noqa: E402

SLOT = "w1-b0042"
ICON_A = b"RIFF-icon-a"
ICON_B = b"RIFF-icon-b"


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class PostUpdateTests(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.td, "ledger", "w1"))
        os.makedirs(os.path.join(self.td, "ledger", "souls"))
        self.json_path = os.path.join(self.td, "ledger", "w1", f"{SLOT}.json")
        self.icon_path = os.path.join(self.td, "ledger", "w1", f"{SLOT}.webp")
        self.soul_path = os.path.join(self.td, "ledger", "souls", f"{SLOT}.soul.json")
        self.prev = inscribed(slot_id=SLOT)
        with open(self.json_path, "w", encoding="utf-8") as fh:
            fh.write(core.render(self.prev))
        with open(self.icon_path, "wb") as fh:
            fh.write(ICON_A)
        with open(self.soul_path, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        git(self.td, "init", "-q")
        git(self.td, "add", "-A")
        git(self.td, "commit", "-q", "-m", "main")

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def edit(self, **over):
        new = dict(self.prev, **over)
        with open(self.json_path, "w", encoding="utf-8") as fh:
            fh.write(core.render(new))
        return new

    def run_tool(self, *argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = post_update.main(list(argv), base_dir=self.td)
        return rc, out.getvalue(), err.getvalue()

    def status(self):
        res = subprocess.run(["git", "-C", self.td, "status", "--porcelain"], capture_output=True, text=True, check=True)
        return sorted(line[3:] for line in res.stdout.splitlines())

    def test_the_edit_becomes_a_request_and_the_slot_is_put_back(self):
        new = self.edit(agent_urls={"home": "https://example.com"}, update_settlements=[fee()])
        rc, out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 0, err)
        # Only the request is left to commit: the slot's files are as main has them.
        self.assertEqual(self.status(), ["ledger/updates/"])
        with open(os.path.join(self.td, "ledger", "updates", f"{TX_FEE1}.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), new)
        self.assertEqual(sorted(os.listdir(os.path.join(self.td, "ledger", "updates"))), [f"{TX_FEE1}.json"])
        with open(self.json_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), core.render(self.prev))

    def test_a_changed_icon_and_manifest_travel_and_an_unchanged_one_does_not(self):
        self.edit(update_settlements=[fee()])
        with open(self.icon_path, "wb") as fh:
            fh.write(ICON_B)
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(os.listdir(os.path.join(self.td, "ledger", "updates"))),
                         [f"{TX_FEE1}.json", f"{TX_FEE1}.webp"])
        with open(self.icon_path, "rb") as fh:
            self.assertEqual(fh.read(), ICON_A)
        self.assertEqual(self.status(), ["ledger/updates/"])

    def test_a_first_manifest_moves_out_of_the_slots_place(self):
        git(self.td, "rm", "-q", "-f", os.path.relpath(self.soul_path, self.td))
        git(self.td, "commit", "-q", "-m", "no manifest")
        self.edit(update_settlements=[fee()])
        os.makedirs(os.path.dirname(self.soul_path), exist_ok=True)
        with open(self.soul_path, "w", encoding="utf-8") as fh:
            fh.write('{"a": 1}\n')
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.td, "ledger", "updates", f"{TX_FEE1}.soul.json")))
        self.assertFalse(os.path.exists(self.soul_path))

    def test_keep_leaves_the_slot_alone(self):
        new = self.edit(update_settlements=[fee()])
        rc, _out, _err = self.run_tool(SLOT, "--keep")
        self.assertEqual(rc, 0)
        with open(self.json_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), new)

    def test_refuses_what_is_not_a_request(self):
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 1)
        self.assertIn("is unchanged", err)
        self.edit(agent_urls={"home": "https://example.com"})
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 1)
        self.assertIn("update_settlements[] has no entry", err)
        self.edit(update_settlements=[fee(), fee(tx=TX_FEE2)])
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 1)
        self.assertIn("grew by 2", err)
        self.edit(update_settlements=[fee()], slot_id="w1-b0043")
        rc, _out, err = self.run_tool(SLOT)
        self.assertEqual(rc, 1)
        self.assertIn("slot_id never changes", err)
        rc, _out, err = self.run_tool("w1-b0099")
        self.assertEqual(rc, 1)
        self.assertIn("not inscribed", err)
        self.assertEqual(self.run_tool("nonsense")[0], 1)
        self.assertEqual(self.run_tool()[0], 2)

    def test_the_request_it_writes_passes_the_containment_rule(self):
        import verifier
        self.edit(agent_urls={"home": "https://example.com"}, update_settlements=[fee()])
        self.assertEqual(self.run_tool(SLOT)[0], 0)
        entries = [("A", f"ledger/updates/{TX_FEE1}.json")]
        self.assertEqual(verifier.scope_of(entries)["kind"], "update")
        self.assertEqual(updates.list_update_files(self.td), [f"{TX_FEE1}.json"])


if __name__ == "__main__":
    unittest.main()
