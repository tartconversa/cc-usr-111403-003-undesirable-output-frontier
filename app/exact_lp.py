"""使用分数精确算术的两阶段单纯形。

前沿分析需要区分“某个最优解里出现”与“所有最优解里出现”，因此不能
依赖浮点容差或随机微扰。本模块只依赖标准库，用 Fraction 做精确主元
运算，并以 Bland 规则避免退化循环。所有输入约束均写成

    A x = b,  x >= 0

的典式；不等式由调用方显式加入松弛/剩余变量，等式加入人工变量。
解的状态只有 optimal、infeasible、unbounded 三种，不会把数值困难
悄悄映射成一个可行答案。

求解分为 prepare（第一阶段，求一个可行基）和 optimize（第二阶段，
在可行基上换目标）。同一个约束集上求解多个目标时可复用 prepare 的
结果，避免重复第一阶段；复用只共享可行基，不改变任何目标的精确最优
解，因此不会以预热为由选出一个特定最优面端点。
"""
from dataclasses import dataclass
from fractions import Fraction
import math


def as_fraction(value):
    """把 JSON 数字转成精确分数。

    float 先经 str() 取 JSON 中可见的十进制值，避免把二进制尾数带进
    约束。整数、Fraction、Decimal 直接转换；布尔值和非有限数拒绝。
    """
    if isinstance(value, bool):
        raise TypeError("布尔值不是有效数值")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("数值必须有限")
        return Fraction(str(value))
    if hasattr(value, "as_integer_ratio"):
        numerator, denominator = value.as_integer_ratio()
        return Fraction(numerator, denominator)
    raise TypeError(f"不支持精确转换的数值类型: {type(value).__name__}")


@dataclass(frozen=True)
class LPResult:
    status: str
    objective: Fraction | None
    values: tuple[Fraction, ...]
    basis: tuple[int, ...]
    phase1_value: Fraction | None
    pivots: int
    detail: str

    def value(self, index):
        return self.values[index]


@dataclass(frozen=True)
class Feasible:
    """第一阶段的可行基与对应典式，可用于多次第二阶段优化。"""

    status: str
    tableau: tuple
    basis: tuple
    nvars: int
    artificial: frozenset
    phase1_value: Fraction | None
    pivots: int
    detail: str


def _objective_row(costs, tableau, basis, nvars):
    """构造 reduced-cost 行；末位保存 -目标值（便于主元时一并更新）。"""
    row = list(costs) + [Fraction(0)]
    for i, basic in enumerate(basis):
        cost = costs[basic]
        if cost:
            source = tableau[i]
            for j in range(nvars + 1):
                row[j] -= cost * source[j]
    return row


def _pivot(tableau, objective, basis, pivot_row, entering):
    pivot_value = tableau[pivot_row][entering]
    inverse = Fraction(1, 1) / pivot_value
    tableau[pivot_row] = [v * inverse for v in tableau[pivot_row]]
    pivot_source = tableau[pivot_row]
    for i in range(len(tableau)):
        if i == pivot_row:
            continue
        factor = tableau[i][entering]
        if factor:
            target = tableau[i]
            for j in range(len(target)):
                target[j] -= factor * pivot_source[j]
    factor = objective[entering]
    if factor:
        for j in range(len(objective)):
            objective[j] -= factor * pivot_source[j]
    basis[pivot_row] = entering


def _entering_variable(objective, in_basis, nvars, allowed):
    # Bland 规则：取下标最小的正检验数，保证精确算术下有限步终止。
    for j in range(nvars):
        if j in in_basis:
            continue
        if objective[j] > 0 and (allowed is None or allowed(j)):
            return j
    return None


def _leaving_row(tableau, basis, entering):
    best = None
    best_ratio = None
    for i, row in enumerate(tableau):
        coefficient = row[entering]
        if coefficient <= 0:
            continue
        ratio = row[-1] / coefficient
        if best_ratio is None or ratio < best_ratio:
            best_ratio, best = ratio, i
        elif ratio == best_ratio and basis[i] < basis[best]:
            best = i
    return best


def _run_simplex(tableau, objective, basis, nvars, allowed, max_pivots):
    pivots = 0
    in_basis = set(basis)
    while True:
        entering = _entering_variable(objective, in_basis, nvars, allowed)
        if entering is None:
            return "optimal", pivots
        leaving = _leaving_row(tableau, basis, entering)
        if leaving is None:
            return "unbounded", pivots
        in_basis.discard(basis[leaving])
        _pivot(tableau, objective, basis, leaving, entering)
        in_basis.add(entering)
        pivots += 1
        if pivots > max_pivots:
            raise RuntimeError("单纯形主元次数超过上限，拒绝返回未经验证的结果")


def prepare(rows, nvars, basis, artificial, max_pivots=200000):
    """第一阶段：求一个可行基。

    返回 Feasible。status 为 feasible 时 tableau 已消除全部人工变量
    （或允许其以 0 留在基中），可作为多个目标的热启动基。
    """
    if len(rows) != len(basis):
        raise ValueError("约束行与基变量数量不一致")
    if any(row[-1] < 0 for row in rows):
        raise ValueError("单纯形要求初始右端项非负")
    for i, b in enumerate(basis):
        if rows[i][b] != 1:
            raise ValueError("初始基列不是单位阵")
        if any(rows[k][b] != 0 for k in range(len(rows)) if k != i):
            raise ValueError("初始基列不是单位阵")

    tableau = [list(row) for row in rows]
    basis = list(basis)
    artificial = frozenset(artificial)
    phase1_costs = [Fraction(0)] * nvars
    for j in artificial:
        phase1_costs[j] = Fraction(-1)
    objective = _objective_row(phase1_costs, tableau, basis, nvars)
    status, pivots = _run_simplex(tableau, objective, basis, nvars, None,
                                 max_pivots)
    if status == "unbounded":
        raise RuntimeError("第一阶段不应无界")
    # 目标行末位是 -目标值；第一阶段目标是 max(-sum artificial)，
    # 因此行末位恰为 sum artificial，可行时应为零。
    phase1_value = objective[-1]
    if phase1_value != 0:
        return Feasible("infeasible", tuple(map(tuple, tableau)),
                        tuple(basis), nvars, artificial, phase1_value,
                        pivots, "第一阶段人工变量和不为零")

    # 第二阶段开始前把仍为基变量的人工变量换出。若该行在非人工列上全零，
    # 它只是退化冗余行，允许人工变量以 0 留在基中。
    for i, basic in enumerate(basis):
        if basic not in artificial:
            continue
        if tableau[i][-1] != 0:
            raise RuntimeError("第一阶段为零但人工变量取正值")
        for j in range(nvars):
            if j in artificial:
                continue
            if tableau[i][j] != 0:
                _pivot(tableau, objective, basis, i, j)
                break

    return Feasible("feasible", tuple(map(tuple, tableau)), tuple(basis),
                    nvars, artificial, phase1_value, pivots, "")


def optimize(feasible, costs, max_pivots=200000):
    """第二阶段：在可行基上求解 max costs^T x。"""
    if feasible.status != "feasible":
        raise ValueError("只能在可行基上优化")
    tableau = [list(row) for row in feasible.tableau]
    basis = list(feasible.basis)
    nvars = feasible.nvars
    artificial = feasible.artificial
    objective = _objective_row(costs, tableau, basis, nvars)
    status, pivots = _run_simplex(
        tableau, objective, basis, nvars,
        lambda j: j not in artificial, max_pivots)
    pivots += feasible.pivots
    if status == "unbounded":
        return LPResult("unbounded", None, tuple([Fraction(0)] * nvars),
                        tuple(basis), feasible.phase1_value, pivots,
                        "第二阶段目标无界")
    values = [Fraction(0)] * nvars
    for i, basic in enumerate(basis):
        values[basic] = tableau[i][-1]
    return LPResult("optimal", -objective[-1], tuple(values), tuple(basis),
                    feasible.phase1_value, pivots, "")


def solve(rows, nvars, costs, basis, artificial, max_pivots=200000):
    """一次完成两阶段求解，语义等价于 prepare 后立即 optimize。"""
    feasible = prepare(rows, nvars, basis, artificial, max_pivots)
    if feasible.status != "feasible":
        full = [Fraction(0)] * nvars
        for i, basic in enumerate(feasible.basis):
            full[basic] = feasible.tableau[i][-1]
        return LPResult("infeasible", None, tuple(full), feasible.basis,
                        feasible.phase1_value, feasible.pivots,
                        feasible.detail)
    return optimize(feasible, costs, max_pivots)
