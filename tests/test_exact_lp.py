"""验证分数精确单纯形内核的状态语义与复用一致性。"""
from fractions import Fraction as F
from itertools import product
import unittest

from app.exact_lp import as_fraction, optimize, prepare, solve


class ExactLPCoreTests(unittest.TestCase):
    def test_bounded_optimum(self):
        # max x+y s.t. x+y<=4, x<=2, y<=3
        rows = [[F(1), F(1), F(1), F(0), F(0), F(4)],
                [F(1), F(0), F(0), F(1), F(0), F(2)],
                [F(0), F(1), F(0), F(0), F(1), F(3)]]
        result = solve(rows, 5, [F(1), F(1), F(0), F(0), F(0)],
                       [2, 3, 4], set())
        self.assertEqual(result.status, "optimal")
        self.assertEqual(result.objective, 4)
        self.assertEqual(result.values[:2], (2, 2))

    def test_infeasible_is_not_rounded_to_a_solution(self):
        # x>=3 与 x<=1 同时成立不可行
        rows = [[F(1), F(-1), F(1), F(0), F(3)],
                [F(1), F(0), F(0), F(1), F(1)]]
        result = solve(rows, 4, [F(1), F(0), F(0), F(0)], [2, 3], {2})
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.objective)
        self.assertEqual(result.phase1_value, 2)

    def test_unbounded_is_reported(self):
        rows = [[F(-1), F(1), F(0)]]
        result = solve(rows, 2, [F(1), F(0)], [1], set())
        self.assertEqual(result.status, "unbounded")

    def test_degenerate_vertex_terminates(self):
        # max y s.t. x+y<=1, y<=0, x<=1 有退化最优顶点
        rows = [[F(1), F(1), F(1), F(0), F(0), F(1)],
                [F(0), F(1), F(0), F(1), F(0), F(0)],
                [F(1), F(0), F(0), F(0), F(1), F(1)]]
        result = solve(rows, 5, [F(0), F(1), F(0), F(0), F(0)],
                       [2, 3, 4], set())
        self.assertEqual(result.status, "optimal")
        self.assertEqual(result.objective, 0)

    def test_shared_feasible_basis_gives_same_optima_as_cold_solves(self):
        rows = [[F(1), F(1), F(1), F(0), F(0), F(4)],
                [F(1), F(0), F(0), F(1), F(0), F(2)],
                [F(0), F(1), F(0), F(0), F(1), F(3)]]
        feasible = prepare(rows, 5, [2, 3, 4], set())
        objectives = [[F(1), F(1), F(0), F(0), F(0)],
                      [F(0), F(1), F(0), F(0), F(0)],
                      [F(1), F(0), F(0), F(0), F(0)]]
        for costs in objectives:
            warm = optimize(feasible, costs)
            cold = solve(rows, 5, costs, [2, 3, 4], set())
            self.assertEqual(warm.status, cold.status)
            self.assertEqual(warm.objective, cold.objective)

    def test_exact_fraction_parsing(self):
        self.assertEqual(as_fraction(0.1), F(1, 10))
        self.assertEqual(as_fraction(3), F(3))
        with self.assertRaises(ValueError):
            as_fraction(float("nan"))
        with self.assertRaises(TypeError):
            as_fraction(True)

    def test_initial_basis_must_be_identity(self):
        # 基变量的列不是单位列，拒绝开始主元
        rows = [[F(2), F(1), F(2)]]
        with self.assertRaisesRegex(ValueError, "单位阵"):
            solve(rows, 2, [F(1), F(0)], [0], set())


if __name__ == "__main__":
    unittest.main()
