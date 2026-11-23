"""Fixture verification, material replay and CLI behaviour."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen import cli  # noqa: E402
from fixturegen.contract import parse_contract  # noqa: E402
from fixturegen.engine import generate, verify_fixture, verify_report  # noqa: E402
from fixturegen.material import (  # noqa: E402
    replay_block,
    replay_material,
)
from tests.test_engine import billing_doc, small_doc  # noqa: E402
from tests.helpers import contract, slot, table  # noqa: E402


def implied_doc():
    """A is entailed by F: no isolation for A may be fabricated."""
    return contract(
        [
            table("P", "parent",
                  [("id", "int", False), ("total", "int", True)], ["id"]),
            table("C", "child",
                  [("cid", "int", False), ("pref", "int", True),
                   ("amt", "int", True)]),
        ],
        [
            slot("p", "P", [("a", {"id": 1, "total": 0})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 0})]),
        ],
        [
            {"id": "F", "kind": "fk", "child": "C",
             "child_fields": ["pref"], "parent": "P", "parent_key": ["id"]},
            {"id": "A", "kind": "aggregate", "child": "C",
             "child_fields": ["pref"], "child_amount": "amt",
             "parent": "P", "parent_key": ["id"], "parent_amount": "total"},
        ],
    )

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples")


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.contract = parse_contract(small_doc())
        self.report = generate(self.contract, isolate=["A"]).to_json()
        # a report that also contains a proved no-fixture entry with material
        self.implied_contract = parse_contract(implied_doc())
        self.implied_report = generate(
            self.implied_contract, isolate=["A"]).to_json()

    def test_clean_report_verifies(self):
        self.assertTrue(verify_report(self.contract, self.report)["ok"])

    def test_tampered_rule_table_detected(self):
        bad = copy.deepcopy(self.report)
        bad["normal"]["rules"]["A"] = False
        result = verify_report(self.contract, bad)
        self.assertFalse(result["ok"])
        self.assertIn("independent evaluation", " ".join(
            result["fixtures"]["normal"]["errors"]))

    def test_tampered_row_detected(self):
        bad = copy.deepcopy(self.report)
        entry = bad["normal"]["slots"]["p1"]
        entry["row"]["total"] = 12345
        result = verify_fixture(self.contract, bad["normal"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("does not match candidate" in e for e in result["errors"]))

    def test_unknown_candidate_detected(self):
        bad = copy.deepcopy(self.report)
        bad["normal"]["slots"]["p1"]["candidate_id"] = "zzz"
        result = verify_fixture(self.contract, bad["normal"])
        self.assertFalse(result["ok"])

    def test_normal_with_false_rule_detected(self):
        bad = copy.deepcopy(self.report)
        bad["isolations"]["A"]["kind"] = "normal"
        bad["isolations"]["A"]["target"] = None
        result = verify_fixture(self.contract, bad["isolations"]["A"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("false rule" in e for e in result["errors"]))

    def test_no_fixture_claim_is_replayed(self):
        self.assertEqual(
            self.implied_report["isolations"]["A"]["schema"], "no-fixture/1")
        result = verify_report(self.implied_contract, self.implied_report)
        self.assertTrue(result["ok"])
        self.assertTrue(result["fixtures"]["A"]["replayed"])

    def test_tampered_no_fixture_claim_detected(self):
        bad = copy.deepcopy(self.implied_report)
        bad["isolations"]["A"]["material"]["blocks"][0]["claim"]["must_false"] = []
        result = verify_report(self.implied_contract, bad)
        self.assertFalse(result["ok"])
        self.assertIn("no-fixture claim failed", " ".join(result["errors"]))

    def test_proved_no_fixture_without_material_detected(self):
        bad = copy.deepcopy(self.implied_report)
        bad["isolations"]["A"]["material"] = None
        result = verify_report(self.implied_contract, bad)
        self.assertFalse(result["ok"])

    def test_lost_no_fixture_entry_detected(self):
        bad = copy.deepcopy(self.report)
        bad["isolations"]["A"] = {"schema": "no-fixture/1", "kind": "isolated",
                                  "target": "A", "status": "proved",
                                  "reason": "x", "material": None}
        result = verify_report(self.contract, bad)
        self.assertFalse(result["ok"])

    def test_isolation_with_other_false_rule_detected(self):
        bad = copy.deepcopy(self.report)
        bad["isolations"]["A"]["rules"]["F"] = False
        result = verify_fixture(self.contract, bad["isolations"]["A"])
        self.assertFalse(result["ok"])


class MaterialTests(unittest.TestCase):
    def test_block_replay_detects_false_claim(self):
        tiny = small_doc()
        tiny["slots"] = [s for s in tiny["slots"] if s["id"] in ("p1", "c1")]
        c = parse_contract(tiny)
        # claim something false: no model exists for "A true" alone is false
        report = generate(c)
        self.assertTrue(report.normal.rule_results)
        sub = {
            "schema": "fixture-contract/1",
            "contract_id": "tiny",
            "tables": tiny["tables"],
            "slots": tiny["slots"],
            "rules": [r for r in tiny["rules"] if r["id"] == "A"],
        }
        block = {"id": "b", "sub_contract": sub,
                 "claim": {"must_true": ["A"], "must_false": []}}
        result = replay_block(block)
        self.assertFalse(result["ok"])
        self.assertIn("counterexample", " ".join(result["errors"]))

    def test_material_deterministic(self):
        doc = implied_doc()
        c = parse_contract(doc)
        a = generate(c, isolate=["A"]).isolations["A"].to_json()["material"]
        rev = copy.deepcopy(doc)
        rev["slots"].reverse()
        rev["rules"].reverse()
        b = generate(parse_contract(rev), isolate=["A"]).isolations["A"].to_json()["material"]
        self.assertEqual(a, b)

    def test_material_ids_sort_stable(self):
        doc = billing_doc()
        c = parse_contract(doc)
        material = generate(c, isolate=["U-client-id"]).isolations["U-client-id"] \
            .to_json()["material"]
        block = material["blocks"][0]
        self.assertEqual(block["claim"]["must_true"],
                         sorted(block["claim"]["must_true"]))
        self.assertEqual(block["claim"]["must_false"],
                         sorted(block["claim"]["must_false"]))
        self.assertEqual(block["sub_contract"]["slots"],
                         sorted(block["sub_contract"]["slots"],
                                key=lambda s: s["id"]))

    def test_undetermined_material_reports_bound(self):
        doc = implied_doc()
        c = parse_contract(doc)
        material = generate(c, isolate=["A"]).isolations["A"].to_json()["material"]
        result = replay_material(material, max_assignments=0)
        self.assertFalse(result["ok"])
        self.assertTrue(result["undetermined"])


class CliTests(unittest.TestCase):
    def test_cli_writes_fixtures_and_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "run")
            rc = cli.main([os.path.join(EXAMPLES, "billing.json"),
                           "--isolate", "A-client-billed",
                           "--isolate", "U-client-id",
                           "--out", out])
            self.assertEqual(rc, 0)
            for name in ("report.json", "normal.fixture.json",
                         "isolation-A-client-billed.fixture.json",
                         "isolation-U-client-id.no-fixture.json",
                         "isolation-U-client-id.material.json"):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            with open(os.path.join(out, "report.json")) as fh:
                report = json.load(fh)
            self.assertTrue(report["verification"]["ok"])

    def test_cli_replay_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "run")
            cli.main([os.path.join(EXAMPLES, "billing.json"),
                      "--isolate", "U-client-id", "--out", out])
            material = os.path.join(out, "isolation-U-client-id.material.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["--replay-material", material])
            self.assertEqual(rc, 0)
            self.assertTrue(json.loads(buf.getvalue())["ok"])

    def test_cli_malformed_contract_is_error_not_unsat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            doc = billing_doc()
            doc["rules"][0]["key"] = ["ghost"]
            with open(path, "w") as fh:
                json.dump(doc, fh)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main([path])
            self.assertEqual(rc, 2)
            self.assertIn("malformed contract", err.getvalue())
            self.assertNotIn("no fixture", err.getvalue())

    def test_cli_unknown_isolate_rule_is_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main([os.path.join(EXAMPLES, "billing.json"),
                           "--isolate", "NOPE"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown rule", err.getvalue())

    def test_cli_stdout_report(self):
        buf = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            rc = cli.main([os.path.join(EXAMPLES, "billing.json")])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["schema"], "fixture-report/1")


if __name__ == "__main__":
    unittest.main()
