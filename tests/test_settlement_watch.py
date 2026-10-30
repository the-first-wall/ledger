"""B-34: the settlement watcher's receipt makes its zero falsifiable."""
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "ops"))

import settlement_watch as sw  # noqa: E402

KEYS = {"status", "from_block", "to_block", "target_block", "blocks_scanned", "transfers_seen",
        "candidates", "new_settlements", "errors", "started_at", "finished_at", "state_root"}
KNOWN_TX = "0x2971f278d5c6d6c8ca58fb7f526a1edb88118c3d146775318d0bf3327a821012"


def transfer_log(tx, block, amount=1_000_000):
    return {"topics": [sw.TRANSFER_TOPIC, "0x" + "00" * 12 + "ab" * 20, "0x" + sw.PAY_TO[2:].lower().rjust(64, "0")],
            "transactionHash": tx, "blockNumber": hex(block), "data": hex(amount)}


class FakeRpc:
    def __init__(self, head=10_000, logs=(), fail_logs_after=None, fail_head=False):
        self.head, self.logs, self.fail_after, self.fail_head = head, list(logs), fail_logs_after, fail_head
        self.log_calls = 0

    def __call__(self, method, params):
        if method == "eth_blockNumber":
            if self.fail_head:
                raise RuntimeError("All Base RPC endpoints failed. Last error: timeout")
            return hex(self.head)
        self.log_calls += 1
        if self.fail_after is not None and self.log_calls > self.fail_after:
            raise RuntimeError("rate limited")
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        return [l for l in self.logs if lo <= int(l["blockNumber"], 16) <= hi]


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.cursor = os.path.join(self.td, "cursor.json")
        self.receipt = os.path.join(self.td, "receipt.json")
        self.ticks = iter(["2026-10-30T10:00:00Z", "2026-10-30T10:00:05Z"] * 10)

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def go(self, rpc, **kw):
        return sw.run(rpc, state_file=self.cursor, receipt_file=self.receipt, lookback=kw.pop("lookback", 2000),
                      clock=lambda: next(self.ticks), sleep=0, **kw)

    def written(self):
        with open(self.receipt, encoding="utf-8") as fh:
            return json.load(fh)

    def test_empty_window_is_a_full_receipt(self):
        r, code = self.go(FakeRpc())
        self.assertEqual(code, 0)
        self.assertEqual(set(r), KEYS)
        self.assertEqual((r["from_block"], r["to_block"]), (8001, 10000))
        self.assertEqual(r["status"], "no_transfers_in_window")
        self.assertEqual((r["transfers_seen"], r["blocks_scanned"], r["errors"]), (0, 2000, []))
        self.assertEqual(r["state_root"], json.load(open(os.path.join(REPO, "state.json")))["merkle_root"])
        self.assertEqual(self.written(), r)

    def test_up_to_date_still_emits_a_receipt_without_claiming_a_scan(self):
        self.go(FakeRpc())
        rpc = FakeRpc()
        r, code = self.go(rpc)
        self.assertEqual(code, 0)
        self.assertEqual(rpc.log_calls, 0)
        self.assertEqual(r["status"], "no_transfers_in_window")
        self.assertEqual((r["from_block"], r["to_block"], r["blocks_scanned"]), (10001, 10000, 0))
        self.assertEqual(set(self.written()), KEYS)

    def test_transfers_are_candidates_and_known_hashes_are_not_new(self):
        rpc = FakeRpc(logs=[transfer_log(KNOWN_TX, 9000), transfer_log("0x" + "cd" * 32, 9500, 2_500_000)])
        r, code = self.go(rpc)
        self.assertEqual(code, 0)
        self.assertEqual((r["status"], r["transfers_seen"], len(r["candidates"])), ("transfers_found", 2, 2))
        self.assertEqual([t["tx_hash"] for t in r["new_settlements"]], ["0x" + "cd" * 32])
        self.assertEqual(r["new_settlements"][0]["amount_usdc"], 2.5)

    def test_failed_chunk_is_recorded_receipt_written_exit_nonzero_cursor_not_past_gap(self):
        rpc = FakeRpc(fail_logs_after=1)
        r, code = self.go(rpc)
        self.assertEqual(code, 1)
        self.assertEqual(r["status"], "error")
        self.assertTrue(r["errors"] and "rate limited" in r["errors"][0])
        self.assertEqual(r["to_block"], 8001 + sw.CHUNK_BLOCKS - 1)  # only the chunk that WAS read
        self.assertEqual(r["target_block"], 10000)
        self.assertEqual(self.written(), r)
        self.assertEqual(sw._load_cursor(self.cursor), r["to_block"])

    def test_head_lookup_failure_is_an_error_receipt(self):
        r, code = self.go(FakeRpc(fail_head=True))
        self.assertEqual(code, 1)
        self.assertEqual((r["status"], r["from_block"], r["to_block"]), ("error", None, None))
        self.assertIn("timeout", r["errors"][0])
        self.assertEqual(set(self.written()), KEYS)

    def test_docstring_no_longer_claims_exit_zero_always(self):
        self.assertNotIn("exit code 0 always", sw.__doc__.lower())
        self.assertIn("exits non-zero", sw.__doc__)

    def test_workflow_publishes_the_receipt_on_a_schedule(self):
        wf = open(os.path.join(REPO, ".github", "workflows", "settlement_watch.yml"), encoding="utf-8").read()
        for needle in ("cron:", "python ops/settlement_watch.py --json", "ops/scan-receipt.json",
                       "ops/settlement-cursor.json", "contents: write"):
            self.assertIn(needle, wf)


if __name__ == "__main__":
    unittest.main()
