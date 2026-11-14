"""验证方向距离前沿的业务语义与可核对性。

这些测试使用合成小数据，不对应真实企业。重点覆盖：
弱可处置下坏产出不能自由处置、最优面多解时“可能/必然”参照的区分、
留一参照池的显式语义、数据无效与不可行的分离，以及单位与归一化保留。
"""
import copy
import json
from fractions import Fraction as F
from pathlib import Path
import unittest

from app.catalog import load
from app.frontier import MODE_EXCLUDED, MODE_INCLUDED, SolveCache, analyze
from app.service import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
FRONTIER = json.loads(
    (ROOT / "fixtures" / "frontier.json").read_text(encoding="utf-8"))


def snapshot(firms, direction, dimensions=None, **metadata):
    payload = {
        "schema_version": 1,
        "metadata": {
            "data_version": "test-1",
            "dimensions": dimensions or {"inputs": 1, "good": 1, "bad": 1},
            "direction": direction,
            **metadata,
        },
        "collections": {"firms": firms},
    }
    return load(payload, SCHEMAS)


def firm(firm_id, inputs, good, bad, revision=1, year=None):
    row = {"id": firm_id, "revision": revision,
           "inputs": list(inputs), "good": list(good), "bad": list(bad)}
    if year is not None:
        row["year"] = year
    return row


def by_firm(result, mode=MODE_INCLUDED):
    return {entry["firm_id"]: entry for entry in result["firms"]
            if entry.get("mode") == mode}


class WeakDisposabilityTests(unittest.TestCase):
    def test_bad_output_contracts_with_good_output(self):
        # A=(1,2,1) 是标杆。B=(1,1,2) 若把坏产出当普通投入自由处置，
        # 只需把坏产出降到 1 即可；弱可处置要求好坏产出同比例收缩，
        # 因此只能沿 (1,2,1) 方向按 1/2 收缩，beta=1。
        snap = snapshot(
            [firm("A", [1], [2], [1]), firm("B", [1], [1], [2])],
            {"inputs": [0], "good": [1], "bad": [1]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["B"]
        self.assertEqual(entry["status"], "optimal")
        self.assertEqual(entry["beta"], "1")
        self.assertEqual(entry["projection"]["good"], ["2"])
        self.assertEqual(entry["projection"]["bad"], ["1"])
        self.assertTrue(entry["residual"]["checks"]["bad_output_balanced"])
        self.assertTrue(entry["residual"]["passed"])

    def test_bad_output_equality_is_exact(self):
        snap = snapshot(
            [firm("A", [1], [1], [1]), firm("B", [3], [3], [3])],
            {"inputs": [1], "good": [1], "bad": [1]})
        result = analyze(snap, modes=(MODE_INCLUDED,))
        for entry in result["firms"]:
            self.assertEqual(entry["status"], "optimal")
            self.assertEqual(F(entry["residual"]["bad_mismatch"][0]), 0)

    def test_direction_signs_follow_contract(self):
        snap = snapshot(
            [firm("A", [1], [1], [1]), firm("B", [1], [3], [3])],
            {"inputs": [0], "good": [1], "bad": [1]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["B"]
        self.assertEqual(entry["beta"], "0")
        self.assertEqual(entry["projection"], {"inputs": ["1"],
                                               "good": ["3"],
                                               "bad": ["3"]})

    def test_input_direction_reduces_inputs(self):
        snap = snapshot(
            [firm("A", [2], [1], [1]), firm("B", [3], [1], [1])],
            {"inputs": [1], "good": [0], "bad": [0]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["B"]
        self.assertEqual(entry["beta"], "1")
        self.assertEqual(entry["projection"]["inputs"], ["2"])


class OptimalFaceTests(unittest.TestCase):
    def test_possible_reference_is_not_reported_as_unique(self):
        # A、C 完全相同的观测，B 是更差的缩放副本；A 的最优投影可由
        # A 或 C 单独支撑，两者都只是“可能参照”，都不能称为唯一标杆。
        snap = snapshot(
            [firm("A", [1], [1], [1]),
             firm("B", [2], [2], [2]),
             firm("C", [1], [1], [1])],
            {"inputs": [0], "good": [1], "bad": [1]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["A"]
        roles = {peer["firm_id"]: peer["role"]
                 for peer in entry["peer_certainty"]}
        self.assertEqual(roles["A"], "possible")
        self.assertEqual(roles["C"], "possible")
        self.assertEqual(roles["B"], "absent")
        for peer in entry["peer_certainty"]:
            if peer["role"] == "possible":
                self.assertEqual(peer["z_min"], "0")
                self.assertGreater(F(peer["z_max"]), 0)

    def test_certain_reference_has_positive_minimum_and_witness(self):
        snap = snapshot(
            [firm("A", [1], [1], [1]), firm("B", [3], [1], [1])],
            {"inputs": [1], "good": [0], "bad": [0]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["B"]
        a_peer = next(p for p in entry["peer_certainty"]
                      if p["firm_id"] == "A")
        self.assertEqual(a_peer["role"], "certain")
        self.assertGreater(F(a_peer["z_min"]), 0)
        # 见证本身就是最优解，必须通过全部约束
        self.assertTrue(a_peer["witness"]["z_min"]["checks"]["all"])
        self.assertEqual(a_peer["witness"]["z_min"]["attained"],
                         a_peer["z_min"])

    def test_witness_beta_equals_reported_beta(self):
        result = analyze(load(copy.deepcopy(FRONTIER), SCHEMAS),
                         modes=(MODE_INCLUDED,))
        for entry in result["firms"]:
            if entry["status"] != "optimal":
                continue
            for peer in entry["peer_certainty"]:
                for witness in peer["witness"].values():
                    self.assertEqual(witness["beta"], entry["beta"])
                    self.assertTrue(witness["checks"]["all"])

    def test_lambda_and_z_are_separate(self):
        # 容量权重 lambda 与有效活动 z 分别报告；z=0 的容量占用不算标杆。
        result = analyze(load(copy.deepcopy(FRONTIER), SCHEMAS),
                         modes=(MODE_INCLUDED,))
        entry = by_firm(result)["clean"]
        top = next(p for p in entry["peer_certainty"]
                   if p["firm_id"] == "top")
        self.assertEqual(top["role"], "certain")
        self.assertGreater(F(top["z_min"]), 0)
        for peer in entry["peer_certainty"]:
            self.assertIn("lambda_min", peer)
            self.assertIn("z_min", peer)
            if peer["z_max"] == "0":
                self.assertFalse(peer["role"] == "certain")


class ReferenceModeTests(unittest.TestCase):
    def test_self_excluded_removes_evaluated_firm(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(snap, modes=(MODE_INCLUDED, MODE_EXCLUDED))
        for entry in result["firms"]:
            if entry["status"] != "optimal":
                continue
            for peer in entry["peer_certainty"]:
                if peer["is_evaluated_firm"]:
                    self.assertNotEqual(entry["mode"], MODE_EXCLUDED)
            if entry["mode"] == MODE_EXCLUDED:
                self.assertNotIn(entry["firm_id"],
                                 [p["firm_id"] for p in entry["peer_certainty"]
                                  if p["lambda_max"] != "0"])

    def test_left_out_infeasibility_is_not_data_invalidity(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(snap, modes=(MODE_INCLUDED, MODE_EXCLUDED))
        diagnostics = {item["firm_id"]: item
                       for item in result["diagnostics"]["excluded_infeasible"]}
        self.assertIn("top", diagnostics)
        self.assertEqual(diagnostics["top"]["classification"],
                         "super_efficiency_infeasible")
        entry = by_firm(result, MODE_EXCLUDED).get("top")
        self.assertEqual(entry["status"], "infeasible")
        self.assertEqual(entry["reason"], "reference_projection_infeasible")

    def test_excluded_only_run_does_not_claim_super_efficiency(self):
        rows = [firm("A", [1], [1], [1]), firm("B", [2], [1], [1])]
        snap = snapshot(rows, {"inputs": [1], "good": [0], "bad": [0]})
        result = analyze(snap, modes=(MODE_EXCLUDED,))
        item = result["diagnostics"]["excluded_infeasible"][0]
        self.assertEqual(item["classification"],
                         "infeasible_with_excluded_pool")
        self.assertIsNone(item["self_included_status"])

    def test_invalid_data_stays_invalid_in_both_modes(self):
        rows = [firm("A", [1], [1], [1]), firm("B", [-1], [1], [1])]
        snap = snapshot(rows, {"inputs": [0], "good": [1], "bad": [1]})
        result = analyze(snap)
        b_entries = [e for e in result["firms"] if e["firm_id"] == "B"]
        self.assertEqual(len(b_entries), 1)
        self.assertEqual(b_entries[0]["status"], "invalid")
        self.assertEqual(b_entries[0]["reason"], "invalid_data")
        self.assertEqual(result["reference"]["rejected_firms"][0]["id"], "B")

    def test_empty_reference_pool_is_explicit(self):
        rows = [firm("solo", [1], [1], [1], year=2022)]
        snap = snapshot(rows, {"inputs": [0], "good": [1], "bad": [1]},
                        year_key="year")
        result = analyze(snap, year=2022, modes=(MODE_EXCLUDED,))
        solo = by_firm(result, MODE_EXCLUDED)["solo"]
        self.assertEqual(solo["status"], "no_reference")
        self.assertEqual(solo["reason"], "empty_reference_pool")


class DirectionAndDataTests(unittest.TestCase):
    def test_zero_direction_rejected(self):
        snap = snapshot([firm("A", [1], [1], [1])],
                        {"inputs": [0], "good": [0], "bad": [0]})
        result = analyze(snap)
        self.assertFalse(result["reference"]["direction_valid"])
        self.assertEqual(result["reference"]["direction_issues"][0]["code"],
                         "direction_zero")
        self.assertEqual(result["firms"][0]["status"], "invalid")
        self.assertEqual(result["firms"][0]["reason"], "invalid_direction")

    def test_missing_direction_does_not_invent_one(self):
        payload = {
            "schema_version": 1,
            "metadata": {
                "data_version": "t",
                "dimensions": {"inputs": 1, "good": 1, "bad": 1},
            },
            "collections": {"firms": [firm("A", [1], [1], [1])]},
        }
        result = analyze(load(payload, SCHEMAS))
        self.assertFalse(result["reference"]["direction_valid"])
        codes = [item["code"]
                 for item in result["reference"]["direction_issues"]]
        self.assertIn("direction_missing", codes)

    def test_negative_direction_rejected(self):
        snap = snapshot([firm("A", [1], [1], [1])],
                        {"inputs": [-1], "good": [1], "bad": [1]})
        result = analyze(snap)
        codes = [item["code"]
                 for item in result["reference"]["direction_issues"]]
        self.assertIn("direction_negative", codes)

    def test_units_are_preserved(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(snap)
        self.assertEqual(result["reference"]["units"],
                         {"inputs": ["万kWh", "吨标煤"],
                          "good": ["万元"], "bad": ["吨CO2"]})
        self.assertEqual(result["data_version"], "frontier-sample-1")
        self.assertEqual(len(result["input_digest"]), 64)

    def test_normalization_does_not_change_beta(self):
        rows = [firm("A", [100, 10], [50], [5]),
                firm("B", [120, 12], [60], [6]),
                firm("C", [80, 8], [40], [4])]
        direction = {"inputs": [1, 1], "good": [1], "bad": [1]}
        base = snapshot(rows, direction,
                        {"inputs": 2, "good": 1, "bad": 1})
        scaled_rows = [firm(row["id"],
                            [row["inputs"][0] * 1000, row["inputs"][1] * 1000],
                            [row["good"][0] * 1000], [row["bad"][0] * 1000])
                       for row in rows]
        scaled_direction = {"inputs": [1000, 1000], "good": [1000],
                            "bad": [1000]}
        scaled = snapshot(scaled_rows, scaled_direction,
                          {"inputs": 2, "good": 1, "bad": 1})
        first = analyze(base, modes=(MODE_INCLUDED,))
        second = analyze(scaled, modes=(MODE_INCLUDED,))
        self.assertEqual([e["beta"] for e in first["firms"]],
                         [e["beta"] for e in second["firms"]])

    def test_extreme_scale_is_flagged(self):
        snap = snapshot(
            [firm("A", [0.000001, 2000000], [10], [1]),
             firm("B", [0.000002, 4000000], [20], [2])],
            {"inputs": [1, 1], "good": [1], "bad": [1]},
            {"inputs": 2, "good": 1, "bad": 1})
        result = analyze(snap)
        diagnostics = result["reference"]["normalization"]["diagnostics"]
        self.assertTrue(diagnostics["extreme"])
        self.assertGreater(F(diagnostics["max_ratio"]), 10 ** 6)


class ScenarioTests(unittest.TestCase):
    def test_direction_change_is_attributed(self):
        snap = snapshot(
            [firm("A", [1], [3], [1]), firm("B", [1], [1], [1])],
            {"inputs": [0], "good": [1], "bad": [0]})
        result = analyze(
            snap, modes=(MODE_INCLUDED,),
            scenarios=[("加入投入方向",
                        {"direction": {"inputs": [1], "good": [1],
                                       "bad": [0]}})])
        scenario = result["scenarios"][0]
        self.assertEqual(scenario["changed"], ["direction"])
        row = next(item for item in scenario["firms"]
                   if item["firm_id"] == "B")
        self.assertEqual(row["before_beta"], "2")
        self.assertEqual(row["after_beta"], "0")
        self.assertEqual(row["delta_beta"], "-2")

    def test_year_change_is_attributed(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(
            snap, year=2022, modes=(MODE_INCLUDED,),
            scenarios=[("放开年度", {"year": None})])
        scenario = result["scenarios"][0]
        self.assertEqual(scenario["changed"], ["year"])
        newer = next(item for item in scenario["firms"]
                     if item["firm_id"] == "newer"
                     and item["mode"] == MODE_INCLUDED)
        self.assertEqual(newer["before_status"], "excluded")
        self.assertEqual(newer["after_status"], "optimal")

    def test_mode_change_is_attributed(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(
            snap, modes=(MODE_INCLUDED,),
            scenarios=[("改口径", {"modes": (MODE_EXCLUDED,)})])
        self.assertEqual(result["scenarios"][0]["changed"], ["modes"])


class ReproducibilityTests(unittest.TestCase):
    def test_input_order_does_not_change_result(self):
        payload = load(copy.deepcopy(FRONTIER), SCHEMAS)
        reversed_payload = copy.deepcopy(FRONTIER)
        reversed_payload["collections"]["firms"].reverse()
        first = analyze(payload)
        second = analyze(load(reversed_payload, SCHEMAS))
        self.assertEqual(first["firms"], second["firms"])
        self.assertEqual(first["summary"], second["summary"])

    def test_shared_cache_returns_identical_answer(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        cache = SolveCache()
        first = analyze(snap, cache=cache)
        second = analyze(snap, cache=cache)
        self.assertEqual(first["firms"], second["firms"])
        self.assertGreater(second["diagnostics"]["cache"]["hits"], 0)

    def test_no_float_in_output(self):
        snap = load(copy.deepcopy(FRONTIER), SCHEMAS)
        result = analyze(snap)

        def walk(value):
            if isinstance(value, float):
                self.fail("输出不应含浮点数")
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(result)


if __name__ == "__main__":
    unittest.main()


class CapacityVersusActivityTests(unittest.TestCase):
    def test_capacity_weight_alone_is_not_a_benchmark(self):
        # D 的投影需要 A 的投入容量，但不需要 A 的有效产出活动；
        # 因此 A 的 lambda 在最优面上可正，z 却恒为零，只能列为非参照。
        rows = [firm("A", [8], [19], [4]), firm("B", [5], [12], [4]),
                firm("C", [16], [19], [0]), firm("D", [20], [1], [3])]
        snap = snapshot(rows, {"inputs": [1], "good": [1], "bad": [1]})
        entry = by_firm(analyze(snap, modes=(MODE_INCLUDED,)))["D"]
        self.assertEqual(entry["beta"], "3")
        peer = next(p for p in entry["peer_certainty"]
                    if p["firm_id"] == "A")
        self.assertEqual(peer["role"], "absent")
        self.assertEqual(peer["z_max"], "0")
        self.assertGreater(F(peer["lambda_max"]), 0)
        self.assertTrue(peer["lambda_positive_without_activity"])
        self.assertTrue(peer["witness"]["lambda_max"]["checks"]["all"])
