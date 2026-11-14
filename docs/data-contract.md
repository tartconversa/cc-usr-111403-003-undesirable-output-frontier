# 资料与现有计算约定

## 修订快照

schema_version为整数1，metadata为业务方案字典，collections为业务集合字典。每条记录有非空字符串id和正整数revision。
同集合相同id取最高revision；同id同revision内容不同即冲突，即便存在更高修订也拒绝。deleted=true为撤回，之后更高修订可恢复。
撤回记录不要求携带业务字段。输入顺序与相同记录重试不改变语义；非有限数字拒绝。
Snapshot.rows返回独立副本；input_digest绑定有效修订和全部metadata。输出data_version沿用metadata，不由程序自造资料版本。
既有摘要入口为app.service.inspect(payload)，输出schema_version、input_digest、data_version和summary。

## 业务资料

firms有inputs、good、bad，均为非负数值向量，维数由metadata确定。技术集合定义为存在lambda>=0且sum(lambda)=1，另有0<=z<=lambda，使Xlambda<=x、Yz>=y、Bz=b；lambda表示投入容量组合，z表示有效产出活动强度，允许按比例同时缩减好坏产出而不释放投入。参照可能性与必然性以z权重为准，投入容量lambda另列；关闭活动不算有效标杆。评价目标最大beta>=0，使(x-beta*gx,y+beta*gy,b-beta*gb)在集合中，三个方向分量非负。此定义为用户业务口径，数值求解方式未指定。baseline.analyze仅检查维数与非负并统计角色总量。

各集合必要字段：{"firms": ["inputs", "good", "bad"]}。

## 前沿分析（app.frontier）

新增入口 app.frontier.analyze(snapshot, ...)，不改变既有摘要接口。技术集合严格按上一节：可变规模报酬、sum(lambda)=1、期望产出强可处置、不期望产出弱可处置（Bz=b 等式且 0<=z<=lambda）。求解 max beta，投影为 (x-beta*gx, y+beta*gy, b-beta*gb)。

### 方向与数据的前置处理

metadata.direction 缺失、维数不符、任一分量为负或全零时，方向判为无效，所有企业标为 invalid_direction，不猜测替代方向。firms 中维数不符、非数值、负值、全零观测判为无效，列入 rejected_firms 且不进入参照池；它们仍是本次运行的显式记录，不会静默消失。这些都属于数据或方向问题，与求解不可行分开报告。

### 两种参照口径

- self_included：参照池为通过年度筛选的全部有效企业，包含被评价者自身；这是常规技术集合口径，beta=0 表示观测位于前沿。
- self_excluded：参照池移除被评价者（留一 / 超效率口径），分数不以 1 为上限。若移除后参照池为空，状态为 no_reference，不伪造一个分数。

被评价者是否在参照池中始终由 mode 决定并写入结果，不依赖调用方另行约定。

### 最优面与参照企业

最优 beta 唯一，但最优 (lambda, z) 可以不唯一。程序固定 beta 后，对每个参照企业分别最小化和最大化 z_i 与 lambda_i，得到它们在最优面中的区间，并保存取到这些端点的完整权重见证；每个见证都独立重算投影并通过全部约束校验。

- z_i 的最小值大于 0：必然参照（所有最优方案都用到它）。
- z_i 最大值为正而最小值为 0：可能参照（存在某个最优方案用到它，但不能称为唯一标杆）。
- z_i 最大值为 0：非参照。

lambda 是投入容量权重，z 是有效产出活动权重。lambda 可正而 z 恒为零，表示该企业只贡献投入容量、不贡献有效产出活动，列为 lambda_positive_without_activity，不算标杆。结果还附 one_optimal_weights，仅作为可复核的单个最优解示例，不能当作标杆清单。

### 状态语义

- optimal：求得最优 beta，projection、adjustment、residual 均通过校验。
- invalid：数据无效（invalid_data）或方向无效（invalid_direction）。
- infeasible：该参照池和方向下投影约束不可行（reference_projection_infeasible）。若仅 self_excluded 不可行而 self_included 最优，diagnostics.excluded_infeasible 将其分类为 super_efficiency_infeasible，并说明这不是数据无效。
- no_reference：移除被评价者后参照池为空。
- excluded：该企业不在所限定的参照年度，未计算跨年度外推分数。
- unbounded：目标无界，拒绝返回分数。

### 数值与可比性

求解使用两阶段单纯形、Fraction 精确算术和 Bland 规则；不使用容差、随机微扰，也不把坏产出取倒数后放入常规 DEA。结果中所有有理数编码为稳定文本（整数或 "p/q"），不输出浮点数。

输出保留原始单位标签（metadata.units，可选）、归一化尺度、data_version 和 input_digest。归一化按角色维度除以有效企业最大绝对值，只缩放约束行，不改变 beta、投影或参照判定；量级差异过大时 diagnostics 给出 extreme 提示，跨维比较应使用原始单位。

### 情景与复用

analyze 接受 scenarios=[(label, override)]，override 可改 direction、year 或 modes。每个情景逐企业输出前后状态、beta 与 delta_beta，并标注 changed 为 direction、year 或 modes，用来说明分数变化来自哪一个假设；不做不可识别的“份额分解”。

SolveCache 可跨调用共享。它在相同模型签名与固定 beta 下复用第一阶段的可行基，不同目标各自做第二阶段；复用不改变任何目标的精确最优解，也不会因预热基不同而偏向某个最优面端点。diagnostics.cache 报告命中与不同基数量。

### 运行

    python -m app.frontier fixtures/frontier.json

fixtures/frontier.json 为合成小数据（含两个年度、两个投入维和单位标签），不对应真实企业或个人。

## 当前能力

app.baseline.analyze(snapshot)产生已有摘要，app.service.inspect负责绑定输入身份；app.frontier.analyze 提供上述前沿分析，已有接口及数据语义保持兼容。
