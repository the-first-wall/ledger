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


class ReservedNoteMatchesTheLedger(unittest.TestCase):
    """B-119: the reserved-block note is hand-typed inside a signed, hash-pinned
    document, so it is asserted against the published ledger instead of trusted.
    The old note ("#0002-#0010 are reserved") went false the day #0006 was
    granted (2026-11-04) and stayed false: a reserved set with a granted block
    inside it cannot be written as one range."""

    @staticmethod
    def _runs(nums):
        runs = []
        for n in sorted(nums):
            if runs and n == runs[-1][1] + 1:
                runs[-1][1] = n
            else:
                runs.append([n, n])
        return runs

    def _published_reserved(self):
        state = json.loads(_read("state.json"))
        nums = [int(s[4:]) for s in state["reserved_slots"]
                if isinstance(s, str) and re.fullmatch(r"w1-b\d{4}", s)]
        self.assertTrue(nums, "state.json publishes no reserved_slots; a note naming them is meaningless")
        return self._runs(nums)

    def _note_runs(self, text):
        m = re.search(r"Blocks\s+\*\*(.+?)\s+are reserved\*\*", text)
        self.assertIsNotNone(
            m, "skill.md no longer states which blocks are reserved; fix the note (and this assertion)")
        runs = []
        for part in re.split(r"\s+and\s+", m.group(1)):
            mm = re.fullmatch(r"#(\d{4})\s*\u2013\s*#(\d{4})", part.strip())
            if mm:
                runs.append([int(mm.group(1)), int(mm.group(2))])
            else:
                one = re.fullmatch(r"#(\d{4})", part.strip())
                self.assertIsNotNone(one, "skill.md reserved range %r is not #NNNN or #NNNN-#NNNN" % part)
                runs.append([int(one.group(1)), int(one.group(1))])
        return runs

    def test_skill_md_names_exactly_the_published_reserved_blocks(self):
        self.assertEqual(self._note_runs(_read("skill.md")), self._published_reserved())

    def test_the_retired_range_is_not_stated_anywhere_that_names_the_reserved_set(self):
        """#0002-#0010 is the founding *class* (ledger_core.RESERVED_SLOT_NUMBERS),
        not the reserved set: #0002 is retired and #0006 was granted. A note that
        says the range is reserved is the B-119 defect."""
        for rel in ("skill.md",):
            self.assertNotRegex(_read(rel), r"#0002\s*\u2013\s*#0010[^\n]*are reserved",
                                "%s states the whole founding range as reserved" % rel)


if __name__ == "__main__":
    unittest.main()
