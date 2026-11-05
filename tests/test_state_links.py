"""B-51: every state.json field skill.md tells an agent to read exists where a literal follower looks."""
import json
import os
import re
import sys
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import ledger_core as core  # noqa: E402


def _read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


class X402ClaimAtTopLevel(unittest.TestCase):
    def test_generated_state_publishes_x402_claim_at_the_top_level(self):
        state, _ = core.build_state(REPO)
        self.assertIsNotNone(state.get("x402_claim"))
        self.assertEqual(state["x402_claim"], state["links"]["x402_claim"])

    def test_committed_state_agrees(self):
        state = json.loads(_read("state.json"))
        self.assertEqual(state.get("x402_claim"), state["links"]["x402_claim"])
        self.assertTrue(state["x402_claim"].startswith("https://"))

    def test_a_literal_follower_of_skill_md_succeeds(self):
        """Every `field` skill.md says to read `in`/`from` state.json is a top-level key."""
        skill = _read("skill.md")
        state = json.loads(_read("state.json"))
        named = set(re.findall(r"`([a-z0-9_]+)`(?: link)? (?:in|from) `state\.json`", skill))
        named |= set(re.findall(r'state\["([a-z0-9_]+)"\]', skill))
        self.assertIn("x402_claim", named)
        missing = sorted(k for k in named if k not in state)
        self.assertEqual(missing, [], f"skill.md names state.json fields that are not top-level: {missing}")


if __name__ == "__main__":
    unittest.main()
