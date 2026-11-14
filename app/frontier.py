"""弱可处置生产技术下的方向距离前沿分析。

模型严格沿用 docs/data-contract.md：

    T = {(x,y,b) | 存在 lambda>=0, sum(lambda)=1, 0<=z<=lambda,
                   X lambda <= x, Y z >= y, B z = b}

对每家企业求 max beta>=0，使
(x - beta*gx, y + beta*gy, b - beta*gb) 属于 T。

本模块的关键设计：

* 全程使用分数精确算术，不引入容差、随机扰动或“先取倒数”的变换；
  坏产出以 Bz=b 的等式进入弱可处置约束，不能像普通投入一样自由处置。
* 最优 beta 唯一，但最优 (lambda,z) 可能很多。这里不选一个解冒充答案：
  对每个参照企业求 z、lambda 在最优面中的最小值和最大值，给出取到
  这些端点的完整权重见证，并区分“必然参照”“可能参照”“非参照”。
* 方向向量、原始单位和归一化因子、资料版本、输入摘要一并保留。
  归一化只缩放约束行，不改变 beta、投影或参照判定。
* lambda 是投入容量权重，z 是有效产出活动权重；参照资格以 z 为准，
  z=0 的活动即使 lambda>0 也只列为“容量占用、非标杆”。
* 两种参照口径分别输出并明确标注：
  self_included  —— 参照池包含被评价者（若其通过年度筛选）；
  self_excluded  —— 留一 / 超效率口径，参照池移除被评价者。
* 数据无效、方向无效、超效率不可行、参照投影不可行是不同状态。
"""
from dataclasses import dataclass
from fractions import Fraction

from .exact_lp import (LPResult, as_fraction, optimize, prepare, solve)

ROLES = ("inputs", "good", "bad")
MODE_INCLUDED = "self_included"
MODE_EXCLUDED = "self_excluded"
MODES = (MODE_INCLUDED, MODE_EXCLUDED)
OUTPUT_SCHEMA = 1

# 量级差异超过该倍数时给出可解释提示；精确算术不受影响，但跨维比较
# 原始单位比归一化单位更可靠。
SCALE_ALERT = Fraction(10 ** 6)


# ---------------------------------------------------------------------------
# 精确数值与输出表示


def rat(value):
    """精确有理数的稳定文本表示，整数写作 '2'，其余写作 'p/q'。"""
    if value is None:
        return None
    value = as_fraction(value) if not isinstance(value, Fraction) else value
    return str(value)


def _encode(value):
    """递归编码：Fraction 变成稳定文本，其余 JSON 值原样保留。"""
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _is_number(value):
    return isinstance(value, (int, float, Fraction)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# 读取与校验


@dataclass
class FirmData:
    id: str
    year: object
    values: dict
    valid: bool
    issues: tuple


@dataclass
class Direction:
    values: dict
    key: tuple
    valid: bool
    all_zero: bool
    issues: tuple


def _dimensions(metadata):
    dims = metadata.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("metadata.dimensions 缺失或不是对象")
    if set(dims) != set(ROLES):
        raise ValueError("dimensions 只能包含 inputs、good、bad")
    out = {}
    for role in ROLES:
        size = dims.get(role)
        if type(size) is not int or size < 1:
            raise ValueError(f"dimensions.{role} 应为正整数")
        out[role] = size
    return out


def _vector(value, size, label, issues):
    if not isinstance(value, list) or len(value) != size:
        issues.append({"code": "shape", "field": label,
                       "detail": f"应为长度 {size} 的数组"})
        return None
    exact = []
    for position, item in enumerate(value):
        if not _is_number(item):
            issues.append({"code": "not_number", "field": label,
                           "position": position, "detail": "不是数值"})
            return None
        try:
            exact.append(as_fraction(item))
        except (TypeError, ValueError) as error:
            issues.append({"code": "invalid_number", "field": label,
                           "position": position, "detail": str(error)})
            return None
    return exact


def read_firms(rows, dimensions, year_key="year"):
    """把已修订快照的 firms 行转成带有效标记的数据。

    无效企业不参与任何参照池，但仍出现在 rejected_firms 里；它的缺失
    不会被当作 0 或均值，也不会悄悄缩小参照集合而不留痕迹。
    """
    firms = []
    seen = set()
    for row in rows:
        issues = []
        firm_id = row.get("id")
        if not isinstance(firm_id, str) or not firm_id:
            issues.append({"code": "identity", "detail": "id 应为非空字符串"})
            firm_id = repr(firm_id)
        elif firm_id in seen:
            raise ValueError(f"firms 出现重复 id: {firm_id}")
        seen.add(firm_id)
        values = {}
        for role in ROLES:
            values[role] = _vector(row.get(role), dimensions[role],
                                   role, issues)
            if values[role] is not None and any(v < 0 for v in values[role]):
                issues.append({"code": "negative", "field": role,
                               "detail": "投入产出不能为负；负值不是可解释的观测"})
        if not issues:
            if not any(v > 0 for role in ROLES for v in values[role]):
                issues.append({"code": "all_zero",
                               "detail": "投入和产出全为零，无法定位生产技术"})
        firms.append(FirmData(firm_id, row.get(year_key), values,
                              not issues, tuple(issues)))
    return firms


def read_direction(metadata, dimensions, override=None):
    """读取方向向量；缺失或全零视为无效，负值单独标记。"""
    raw = override if override is not None else metadata.get("direction")
    values = {}
    issues = []
    if not isinstance(raw, dict):
        issues.append({"code": "direction_shape" if override is not None else
                       "direction_missing",
                       "detail": "方向应为对象；缺失时不能假设调整方向"})
        raw = {}
    for role in ROLES:
        values[role] = _vector(raw.get(role), dimensions[role],
                               f"direction.{role}", issues)
        if values[role] is not None and any(v < 0 for v in values[role]):
            issues.append({"code": "direction_negative", "field": role,
                           "detail": "方向分量应为非负"})
    all_zero = not issues and all(
        v == 0 for role in ROLES for v in values[role])
    if all_zero:
        issues.append({"code": "direction_zero",
                       "detail": "全零方向无法定义方向距离；请提供非零调整方向"})
    key = None if issues else tuple(tuple(values[role]) for role in ROLES)
    return Direction(values, key, not issues, all_zero, tuple(issues))


def read_units(metadata, dimensions):
    """可选的原始单位标签；只做报告，不参与计算。"""
    raw = metadata.get("units")
    if raw is None:
        return None, []
    if not isinstance(raw, dict) or set(raw) != set(ROLES):
        return None, [{"code": "units_shape",
                       "detail": "metadata.units 应包含 inputs、good、bad"}]
    out = {}
    issues = []
    for role in ROLES:
        labels = raw.get(role)
        if (not isinstance(labels, list) or len(labels) != dimensions[role]
                or any(not isinstance(item, str) for item in labels)):
            issues.append({"code": "units_shape", "field": role,
                           "detail": f"应为长度 {dimensions[role]} 的字符串数组"})
            out[role] = None
        else:
            out[role] = list(labels)
    return (None if issues else out), issues


# ---------------------------------------------------------------------------
# 归一化与量纲诊断


def normalize_scales(firms, dimensions):
    """每个角色维度的数据尺度，仅用于数值归一化。

    取全体有效企业该维最大绝对值，全零维取 1。按行缩放是恒等变换，
    因此 beta、投影与参照判定都不受尺度影响。
    """
    scales = {}
    zero_dimensions = []
    for role in ROLES:
        scales[role] = []
        for d in range(dimensions[role]):
            values = [abs(f.values[role][d]) for f in firms if f.valid]
            largest = max(values) if values else Fraction(0)
            if largest > 0:
                scales[role].append(largest)
            else:
                scales[role].append(Fraction(1))
                zero_dimensions.append({"role": role, "position": d})
    return scales, zero_dimensions


def scale_diagnostics(scales, zero_dimensions, dimensions):
    positive = [scales[role][d] for role in ROLES
                for d in range(dimensions[role])
                if {"role": role, "position": d} not in zero_dimensions]
    ratio = (max(positive) / min(positive)) if positive else None
    return {
        "zero_dimensions": zero_dimensions,
        "max_ratio": ratio,
        "alert_threshold": SCALE_ALERT,
        "extreme": bool(ratio is not None and ratio >= SCALE_ALERT),
        "note": ("各维量级差异超过阈值：已按维归一化保证数值稳定；"
                 "跨维比较请使用原始单位调整量，不要把归一化分量直接相加"
                 if ratio is not None and ratio >= SCALE_ALERT else
                 "按维归一化仅缩放约束行，不改变 beta、投影和参照判定"),
    }


# ---------------------------------------------------------------------------
# 线性模型


@dataclass
class _Model:
    pool_firms: list
    peer_ids: list
    nvars: int
    rows: list
    basis: list
    artificial: set
    lam: list
    z: list
    beta: int
    target: dict
    direction: dict
    target_id: str
    signature: tuple


def _build_model(pool, target, direction, scales, target_id):
    n = len(pool)
    dimensions = {role: len(target[role]) for role in ROLES}
    n_in, n_good, n_bad = (dimensions["inputs"], dimensions["good"],
                           dimensions["bad"])

    lam = list(range(n))
    z = list(range(n, 2 * n))
    beta = 2 * n
    sin_base = beta + 1
    sgood_base = sin_base + n_in
    u_base = sgood_base + n_good
    a_sum = u_base + n
    a_good = a_sum + 1
    a_bad = a_good + n_good
    nvars = a_bad + n_bad

    rows, basis, artificial = [], [], set()

    def empty():
        return [Fraction(0)] * (nvars + 1)

    row = empty()
    for i in lam:
        row[i] = Fraction(1)
    row[a_sum] = Fraction(1)
    row[-1] = Fraction(1)
    rows.append(row)
    basis.append(a_sum)
    artificial.add(a_sum)

    # X lambda + beta*gx + s = x，目标投影 x - beta*gx
    for d in range(n_in):
        scale = scales["inputs"][d]
        row = empty()
        for i, firm in enumerate(pool):
            row[lam[i]] = firm.values["inputs"][d] / scale
        row[beta] = direction["inputs"][d] / scale
        row[sin_base + d] = Fraction(1)
        row[-1] = target["inputs"][d] / scale
        rows.append(row)
        basis.append(sin_base + d)

    # Y z - s - beta*gy = y，目标投影 y + beta*gy
    for d in range(n_good):
        scale = scales["good"][d]
        row = empty()
        for i, firm in enumerate(pool):
            row[z[i]] = firm.values["good"][d] / scale
        row[beta] = -direction["good"][d] / scale
        row[sgood_base + d] = Fraction(-1)
        row[a_good + d] = Fraction(1)
        row[-1] = target["good"][d] / scale
        rows.append(row)
        basis.append(a_good + d)
        artificial.add(a_good + d)

    # B z + beta*gb = b，目标投影 b - beta*gb；等式即弱可处置
    for d in range(n_bad):
        scale = scales["bad"][d]
        row = empty()
        for i, firm in enumerate(pool):
            row[z[i]] = firm.values["bad"][d] / scale
        row[beta] = direction["bad"][d] / scale
        row[a_bad + d] = Fraction(1)
        row[-1] = target["bad"][d] / scale
        rows.append(row)
        basis.append(a_bad + d)
        artificial.add(a_bad + d)

    # z_i <= lambda_i：关闭活动强度不释放投入容量。
    for i in range(n):
        row = empty()
        row[z[i]] = Fraction(1)
        row[lam[i]] = Fraction(-1)
        row[u_base + i] = Fraction(1)
        rows.append(row)
        basis.append(u_base + i)

    signature = (
        tuple(firm.id for firm in pool),
        tuple(tuple(firm.values[role]) for role in ROLES for firm in pool),
        tuple(tuple(target[role]) for role in ROLES),
        tuple(tuple(direction[role]) for role in ROLES),
        tuple(tuple(scales[role]) for role in ROLES),
    )
    return _Model(pool, [firm.id for firm in pool], nvars, rows, basis,
                  artificial, lam, z, beta, target, direction, target_id,
                  signature)


class SolveCache:
    """LP 结果复用缓存，可跨企业、跨情景、跨 analyze 调用共享。

    * 每个 (模型, 固定 beta) 组合只做一次第一阶段，得到可行基；
      同一基上不同目标各自做第二阶段，结果按目标缓存。
    * 键包含模型签名、固定 beta 与目标方向，只在问题完全相同时复用。
    * 复用只共享可行基，不改变各目标的精确最优解，也不会以“预热基
      不同”为由偏向某个最优面端点；端点仍由最小/最大化目标本身决定。
    """

    def __init__(self):
        self.results = {}
        self.bases = {}
        self.hits = 0
        self.misses = 0

    def _prepare(self, model, fixed_beta):
        key = (model.signature, fixed_beta)
        if key not in self.bases:
            rows, nvars, basis, artificial = _build_forms(model, fixed_beta)
            self.bases[key] = (prepare(rows, nvars, basis, artificial),
                               nvars)
        return self.bases[key]

    def solve(self, model, fixed_beta=None, objective=None):
        key = (model.signature, fixed_beta, objective)
        if key in self.results:
            self.hits += 1
            return self.results[key]
        self.misses += 1
        feasible, nvars = self._prepare(model, fixed_beta)
        costs = [Fraction(0)] * nvars
        if fixed_beta is None:
            costs[model.beta] = Fraction(1)
        elif objective is not None:
            direction, index = objective
            costs[index] = Fraction(1 if direction == "max" else -1)
        if feasible.status != "feasible":
            values = [Fraction(0)] * nvars
            for i, basic in enumerate(feasible.basis):
                values[basic] = feasible.tableau[i][-1]
            result = LPResult("infeasible", None, tuple(values),
                              feasible.basis, feasible.phase1_value,
                              feasible.pivots, feasible.detail)
        else:
            result = optimize(feasible, costs)
        self.results[key] = result
        return result

    def snapshot(self):
        return {"hits": self.hits, "misses": self.misses,
                "solves": self.hits + self.misses,
                "distinct_bases": len(self.bases)}


def _build_forms(model, fixed_beta):
    """返回 (rows, nvars, initial_basis, artificial)。

    fixed_beta 通过追加一行 beta = 固定值、并给该行一个人工变量来实现，
    而不是清零 beta 列后平移右端项。这样原始约束的右端项保持非负，
    初等基仍然合法，固定值不可行时由第一阶段如实报告 infeasible。
    """
    if fixed_beta is None:
        return ([row[:] for row in model.rows], model.nvars,
                list(model.basis), set(model.artificial))
    rows = [row[:-1] + [Fraction(0), row[-1]] for row in model.rows]
    nvars = model.nvars + 1
    extra = model.nvars
    fixing = [Fraction(0)] * (nvars + 1)
    fixing[model.beta] = Fraction(1)
    fixing[extra] = Fraction(1)
    fixing[-1] = fixed_beta
    rows.append(fixing)
    basis = list(model.basis) + [extra]
    artificial = set(model.artificial) | {extra}
    return rows, nvars, basis, artificial


def _solution_maps(model, values):
    lam = {model.peer_ids[i]: values[index]
           for i, index in enumerate(model.lam)}
    z = {model.peer_ids[i]: values[index]
         for i, index in enumerate(model.z)}
    return lam, z


# ---------------------------------------------------------------------------
# 投影、约束残差与见证校验


def _projection(target, direction, beta_value):
    out = {}
    for role in ROLES:
        sign = 1 if role == "good" else -1
        out[role] = [target[role][d] + sign * beta_value * direction[role][d]
                     for d in range(len(target[role]))]
    return out


def _combine(pool, role, weights):
    width = len(pool[0].values[role])
    return [sum((weight * firm.values[role][d]
                 for weight, firm in zip(weights, pool)), Fraction(0))
            for d in range(width)]


def _check_solution(model, beta_value, values):
    """独立重算投影并逐条核对原始约束，返回可审计残差。"""
    pool = model.pool_firms
    lam = [values[index] for index in model.lam]
    z = [values[index] for index in model.z]
    projection = _projection(model.target, model.direction, beta_value)

    residual = {
        "sum_lambda_minus_1": sum(lam, Fraction(0)) - 1,
        "input_slack": [],
        "good_excess": [],
        "bad_mismatch": [],
        "min_lambda_minus_z": min(
            (lam[i] - z[i] for i in range(len(pool))), default=Fraction(0)),
        "projection_nonnegative": all(
            value >= 0 for role in ROLES for value in projection[role]),
    }
    if pool:
        input_activity = _combine(pool, "inputs", lam)
        good_activity = _combine(pool, "good", z)
        bad_activity = _combine(pool, "bad", z)
        residual["input_slack"] = [projection["inputs"][d] - input_activity[d]
                                   for d in range(len(input_activity))]
        residual["good_excess"] = [good_activity[d] - projection["good"][d]
                                   for d in range(len(good_activity))]
        residual["bad_mismatch"] = [bad_activity[d] - projection["bad"][d]
                                    for d in range(len(bad_activity))]

    checks = {
        "beta_nonnegative": beta_value >= 0,
        "weights_nonnegative": all(v >= 0 for v in lam + z),
        "sum_lambda_is_one": residual["sum_lambda_minus_1"] == 0,
        "inputs_not_exceeded": all(v >= 0 for v in residual["input_slack"]),
        "good_output_met": all(v >= 0 for v in residual["good_excess"]),
        "bad_output_balanced": all(v == 0 for v in residual["bad_mismatch"]),
        "z_within_lambda": residual["min_lambda_minus_z"] >= 0,
        "projection_nonnegative": residual["projection_nonnegative"],
    }
    checks["all"] = all(checks.values())
    residual["checks"] = checks
    residual["passed"] = checks["all"]
    return projection, residual


def _basis_labels(model, basis):
    labels = []
    for index in basis:
        if index in model.lam:
            labels.append("lambda:" + model.peer_ids[index])
        elif index in model.z:
            labels.append("z:" + model.peer_ids[index - len(model.peer_ids)])
        elif index == model.beta:
            labels.append("beta")
        else:
            labels.append(f"aux:{index}")
    return labels


def _witness(model, beta_value, result, index, expected):
    """把端点解包装成可核对见证，并独立校验全部约束。"""
    attained = result.values[index]
    if attained != expected:
        raise RuntimeError("见证变量值与其声称的端点不一致")
    _, residual = _check_solution(model, beta_value, result.values)
    if not residual["passed"]:
        raise RuntimeError(f"见证未通过约束校验: {residual['checks']}")
    lam, z = _solution_maps(model, result.values)
    return {
        "beta": beta_value,
        "lambda": lam,
        "z": z,
        "attained": attained,
        "basis": _basis_labels(model, result.basis),
        "checks": residual["checks"],
    }


# ---------------------------------------------------------------------------
# 最优面：可能参照与必然参照


def _range(model, cache, beta_value, index):
    low = cache.solve(model, beta_value, ("min", index))
    high = cache.solve(model, beta_value, ("max", index))
    if low.status != "optimal" or high.status != "optimal":
        raise RuntimeError("最优面上变量区间求解失败")
    return low, high


def _peer_analysis(model, cache, beta_value):
    """对每家企业给出 z 与 lambda 的最优面区间和端点见证。

    z_i 的最小值为正是“必然参照”的判据；最大值为正而最小值为零，
    只能说存在某个最优解用到它，即“可能参照”。端点解本身就是权重
    见证，不做插值、排序或以任意一个最优解冒充唯一标杆。
    """
    entries = []
    for i, firm_id in enumerate(model.peer_ids):
        z_low, z_high = _range(model, cache, beta_value, model.z[i])
        z_min, z_max = z_low.values[model.z[i]], z_high.values[model.z[i]]
        lam_low, lam_high = _range(model, cache, beta_value, model.lam[i])
        lam_min = lam_low.values[model.lam[i]]
        lam_max = lam_high.values[model.lam[i]]
        entries.append({
            "firm_id": firm_id,
            "role": ("certain" if z_min > 0 else
                     "possible" if z_max > 0 else "absent"),
            "is_evaluated_firm": firm_id == model.target_id,
            "z_min": z_min,
            "z_max": z_max,
            "lambda_min": lam_min,
            "lambda_max": lam_max,
            "lambda_positive_without_activity": lam_max > 0 and z_max == 0,
            "witness": {
                "z_min": _witness(model, beta_value, z_low,
                                  model.z[i], z_min),
                "z_max": _witness(model, beta_value, z_high,
                                  model.z[i], z_max),
                "lambda_min": _witness(model, beta_value, lam_low,
                                       model.lam[i], lam_min),
                "lambda_max": _witness(model, beta_value, lam_high,
                                       model.lam[i], lam_max),
            },
        })
    return entries


# ---------------------------------------------------------------------------
# 单家企业评价


def _invalid_entry(firm, reason, detail, mode=None):
    return {
        "firm_id": firm.id,
        "year": firm.year,
        "status": "invalid",
        "reason": reason,
        "detail": detail,
        "mode": mode,
        "issues": [dict(issue) for issue in firm.issues],
    }


def _evaluate(firm, pool, direction, scales, mode, cache):
    if not direction.valid:
        raise ValueError("方向无效时不应进入单企业评价")
    if mode == MODE_EXCLUDED and any(peer.id == firm.id for peer in pool):
        raise ValueError("self_excluded 参照池不应包含被评价者")
    model = _build_model(pool, firm.values, direction.values, scales, firm.id)
    result = cache.solve(model)
    if result.status == "infeasible":
        # 参照集合无法表达该企业当前点：通常是方向越过可行域边界，
        # 或参照数据不足。它与数据无效不同，单独报告。
        return {
            "firm_id": firm.id,
            "year": firm.year,
            "status": "infeasible",
            "reason": "reference_projection_infeasible",
            "detail": "在该参照池与方向下投影约束不可行；并非数据无效",
            "mode": mode,
            "phase1_residual": result.phase1_value,
        }
    if result.status == "unbounded":
        return {
            "firm_id": firm.id,
            "year": firm.year,
            "status": "unbounded",
            "reason": "unbounded_beta",
            "detail": "方向与弱可处置约束组合使目标无界，拒绝返回分数",
            "mode": mode,
        }
    beta_value = result.values[model.beta]
    projection, residual = _check_solution(model, beta_value, result.values)
    if not residual["passed"]:
        raise RuntimeError(f"投影校验未通过: {firm.id} {residual['checks']}")
    lam, z = _solution_maps(model, result.values)
    return {
        "firm_id": firm.id,
        "year": firm.year,
        "status": "optimal",
        "mode": mode,
        "beta": beta_value,
        "projection": projection,
        "adjustment": {
            role: [firm.values[role][d] - projection[role][d]
                   for d in range(len(projection[role]))]
            for role in ROLES},
        "residual": residual,
        # 这是 Bland 规则给出的一个最优解，仅作可复核示例；
        # 唯一标杆集合以下面的最优面区间与见证为准。
        "one_optimal_weights": {"lambda": lam, "z": z},
        "peer_certainty": _peer_analysis(model, cache, beta_value),
    }


# ---------------------------------------------------------------------------
# 情景对比


def _firm_pool(firms, year):
    return [firm for firm in firms if firm.valid
            and (year is None or firm.year == year)]


def _compare(base_entries, alt_entries, label, changed):
    """逐企业对比 base 与情景分数，只报告可观察到的差异来源。

    不把总差异拆成若干假设的“精确份额”；只标明本次情景改变了方向、
    参照年度、参照口径还是数据，并给出每家企业的分数前后值。
    """
    base = {(e["firm_id"], e.get("mode")): e for e in base_entries}
    alt = {(e["firm_id"], e.get("mode")): e for e in alt_entries}

    def locate(table, firm_id, mode):
        entry = table.get((firm_id, mode))
        if entry is not None:
            return entry
        # 不区分口径的条目（年度排除、数据无效）对所有口径生效。
        return table.get((firm_id, None))

    keys = set(base) | set(alt)
    for firm_id, mode in list(keys):
        if mode is None:
            continue
        # 不区分口径的条目（年度排除、方向无效）展开到本次实际口径，
        # 避免同一企业在同一情景里出现一条“通用”和两条“分口径”的重复行。
        for table in (base, alt):
            if (firm_id, None) in table:
                keys.add((firm_id, mode))
    keys = {key for key in keys
            if key[1] is not None
            or not any(other[0] == key[0] and other[1] is not None
                       for other in keys)}
    rows = []
    for key in sorted(keys, key=lambda item: (item[0], item[1] or "")):
        firm_id, mode = key
        before, after = locate(base, firm_id, mode), locate(alt, firm_id, mode)
        row = {
            "firm_id": firm_id,
            "mode": mode,
            "before_status": None if before is None else before["status"],
            "after_status": None if after is None else after["status"],
            "before_beta": None if before is None else before.get("beta"),
            "after_beta": None if after is None else after.get("beta"),
        }
        if (before is not None and after is not None
                and before.get("beta") is not None
                and after.get("beta") is not None):
            row["delta_beta"] = after["beta"] - before["beta"]
        else:
            row["delta_beta"] = None
        rows.append(row)
    return {"label": label, "changed": list(changed), "firms": rows}


# ---------------------------------------------------------------------------
# 批量入口


def analyze(snapshot, direction=None, year=None, modes=MODES,
            cache=None, scenarios=None):
    """对快照中的企业批量做方向距离分析。

    direction: 覆盖 metadata.direction 的方向；None 使用资料方向。
    year:      参照池年度筛选；None 表示全部有效企业。
    modes:     参照口径；self_included 与 self_excluded 分别独立求解。
    cache:     可选的 SolveCache，多次调用可共享缓存并读取复用统计。
    scenarios: [(label, override)]，override 可含 direction / year / modes，
               用于逐项隔离“方向变化”与“参照年度变化”。

    返回值中所有有理数均编码为稳定文本，原始单位标签、归一化尺度、
    资料版本与输入摘要一并保留。
    """
    metadata = snapshot.metadata
    dimensions = _dimensions(metadata)
    year_key = metadata.get("year_key", "year")
    firms = read_firms(snapshot.rows("firms"), dimensions, year_key)
    units, unit_issues = read_units(metadata, dimensions)
    if cache is None:
        cache = SolveCache()

    def resolve_direction(override):
        if isinstance(override, Direction):
            return override
        return read_direction(metadata, dimensions, override)

    direction_obj = resolve_direction(direction)

    def run(dir_obj, year_filter, mode_list):
        pool_all = _firm_pool(firms, year_filter)
        scales, zero_dimensions = normalize_scales(
            [firm for firm in firms if firm.valid], dimensions)
        entries = []
        for firm in firms:
            if not firm.valid:
                entries.append(_invalid_entry(
                    firm, "invalid_data", "业务数据无效，不进入参照池"))
                continue
            if not dir_obj.valid:
                entries.append(_invalid_entry(
                    firm, "invalid_direction",
                    "调整方向缺失、全零或含负值，不能定义方向距离"))
                continue
            if year_filter is not None and firm.year != year_filter:
                entries.append({
                    "firm_id": firm.id,
                    "year": firm.year,
                    "status": "excluded",
                    "reason": "year_filter",
                    "detail": (f"参照池限定为年度 {year_filter!r}；本企业不在"
                               "该年度，未计算跨年度的外推分数"),
                    "mode": None,
                })
                continue
            for mode in mode_list:
                pool = (pool_all if mode == MODE_INCLUDED else
                        [peer for peer in pool_all if peer.id != firm.id])
                if not pool:
                    entries.append({
                        "firm_id": firm.id,
                        "year": firm.year,
                        "status": "no_reference",
                        "reason": "empty_reference_pool",
                        "detail": "移除被评价者后参照池为空，不产生超效率分数",
                        "mode": mode,
                    })
                    continue
                entries.append(_evaluate(firm, pool, dir_obj, scales, mode,
                                         cache))
        return entries, scales, zero_dimensions

    entries, scales, zero_dimensions = run(direction_obj, year, tuple(modes))

    def excluded_infeasible_diagnostics(entry_list):
        """把超效率不可行与数据无效、常规不可行分开说明。"""
        by_key = {(e["firm_id"], e.get("mode")): e for e in entry_list}
        out = []
        for entry in entry_list:
            if (entry["status"] != "infeasible"
                    or entry.get("mode") != MODE_EXCLUDED):
                continue
            included = by_key.get((entry["firm_id"], MODE_INCLUDED))
            included_status = None if included is None else included["status"]
            if included_status is None:
                classification = "infeasible_with_excluded_pool"
                note = ("本次未计算含自身口径，无法判断是否仅超效率不可行；"
                        "需要时请同时请求 self_included")
            elif included_status == "optimal":
                classification = "super_efficiency_infeasible"
                note = ("移除被评价者后参照集合无法覆盖其观测；这是超效率"
                        "口径的可行性问题，不是数据无效")
            else:
                classification = "infeasible_in_both_reference_modes"
                note = ("两种参照口径均不可行，应检查方向、年度筛选"
                        "或数据支持域")
            out.append({
                "firm_id": entry["firm_id"],
                "year": entry["year"],
                "classification": classification,
                "self_included_status": included_status,
                "note": note,
            })
        return out

    result = {
        "schema_version": OUTPUT_SCHEMA,
        "analysis": "directional_distance_weak_disposability",
        "input_digest": snapshot.digest,
        "data_version": metadata.get("data_version"),
        "model": {
            "technology": ("lambda>=0, sum(lambda)=1, 0<=z<=lambda, "
                           "X lambda <= x, Y z >= y, B z = b"),
            "returns_to_scale": "variable",
            "good_output_disposability": "strong (Y z >= y)",
            "bad_output_disposability": "weak (B z = b, z<=lambda)",
            "direction_sign": ("inputs -beta*gx, good +beta*gy, "
                               "bad -beta*gb"),
            "objective": "max beta >= 0",
            "solution_method": ("两阶段单纯形 + 分数精确算术 + Bland 规则；"
                                "不使用浮点容差或随机微扰"),
        },
        "reference": {
            "year": year,
            "year_key": year_key,
            "modes": list(modes),
            "mode_semantics": {
                MODE_INCLUDED: "参照池包含被评价者（若其通过年度筛选）；"
                               "技术集合含自身观测",
                MODE_EXCLUDED: "参照池移除被评价者；超效率口径，分数不设上限",
            },
            "supplied_direction": _encode(direction_obj.values),
            "supplied_direction_key": _encode(direction_obj.key),
            "direction_valid": direction_obj.valid,
            "direction_issues": [dict(i) for i in direction_obj.issues],
            "units": units,
            "unit_issues": unit_issues,
            "normalization": {
                "scales": _encode(scales),
                "zero_dimensions": zero_dimensions,
                "invariant": ("按维缩放约束行是恒等变换，beta、投影与"
                              "参照判定均不随归一化改变"),
                "diagnostics": _encode(scale_diagnostics(
                scales, zero_dimensions, dimensions)),
            },
            "pool": [firm.id for firm in _firm_pool(firms, year)],
            "valid_firms": [firm.id for firm in firms if firm.valid],
            "rejected_firms": [
                {"id": firm.id, "issues": [dict(i) for i in firm.issues]}
                for firm in firms if not firm.valid],
        },
        "firms": [_encode(entry) for entry in entries],
        "summary": [_encode(_summary_entry(entry)) for entry in entries],
        "diagnostics": {
            "excluded_infeasible": _encode(
                excluded_infeasible_diagnostics(entries)),
            "cache": cache.snapshot(),
        },
    }

    if scenarios:
        base_signature = _scenario_signature(direction_obj, year, tuple(modes))
        scenario_rows = []
        for label, override in scenarios:
            alt_direction = resolve_direction(override.get("direction",
                                                           direction_obj))
            alt_year = override.get("year", year)
            alt_modes = tuple(override.get("modes", modes))
            alt_entries, _, _ = run(alt_direction, alt_year, alt_modes)
            alt_signature = _scenario_signature(alt_direction, alt_year,
                                                alt_modes)
            changed = []
            if (base_signature[0], base_signature[1], base_signature[2]) != \
                    (alt_signature[0], alt_signature[1], alt_signature[2]):
                changed.append("direction")
            if base_signature[3] != alt_signature[3]:
                changed.append("year")
            if base_signature[4] != alt_signature[4]:
                changed.append("modes")
            scenario_rows.append(_encode(_compare(entries, alt_entries,
                                                  label, changed)))
        result["scenarios"] = scenario_rows
    return result


def _scenario_signature(direction, year, modes):
    issues = tuple(sorted(issue["code"] for issue in direction.issues))
    return (direction.key, direction.valid, issues, year, tuple(modes))


def _summary_entry(entry):
    if entry["status"] != "optimal":
        return one_line_public(entry)
    certain = [peer["firm_id"] for peer in entry["peer_certainty"]
               if peer["role"] == "certain"]
    possible = [peer["firm_id"] for peer in entry["peer_certainty"]
                if peer["role"] == "possible"]
    return {
        "firm_id": entry["firm_id"],
        "year": entry["year"],
        "mode": entry["mode"],
        "status": "optimal",
        "beta": entry["beta"],
        "projection": entry["projection"],
        "certain_references": certain,
        "possible_references": possible,
        "residual_passed": entry["residual"]["passed"],
    }


def one_line_public(entry):
    out = {"firm_id": entry["firm_id"], "year": entry.get("year"),
           "status": entry["status"]}
    for key in ("reason", "mode", "detail"):
        if key in entry:
            out[key] = entry[key]
    return out


__all__ = ("analyze", "read_firms", "read_direction", "normalize_scales",
           "SolveCache", "rat", "MODE_INCLUDED", "MODE_EXCLUDED", "MODES")


def main():
    """命令行入口：python -m app.frontier <资料JSON路径>。"""
    import json
    import sys
    from pathlib import Path

    from .catalog import load
    from .service import SCHEMAS

    if len(sys.argv) != 2:
        raise SystemExit("请指定资料JSON路径")
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    snapshot = load(payload, SCHEMAS)
    print(json.dumps(analyze(snapshot), ensure_ascii=False, indent=2,
                     allow_nan=False))


if __name__ == "__main__":
    main()
