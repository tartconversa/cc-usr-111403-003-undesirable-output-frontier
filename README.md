# 工业绿色效率的弱可处置前沿与全最优参照

本项目读取工业企业的投入、期望产出和不期望产出资料，输出两类结果：

1. 已有业务摘要：按修订快照统计各角色总量。
2. 非参数生产前沿分析：可变规模报酬、期望产出强可处置、不期望产出弱可处置下的方向距离，为每家企业给出可核对的效率缺口 beta、可行投影、约束残差和同业参照。该分析只描述生产技术集合中的差距与投影，不作真实生产调度。

app/catalog.py 按集合和记录键解析修订，拒绝同修订冲突；app/baseline.py 承载已有摘要；app/service.py 输出摘要及输入摘要哈希；app/exact_lp.py 是分数精确的两阶段单纯形；app/frontier.py 是前沿分析入口。
数据字段、模型口径和状态语义见 [资料契约](docs/data-contract.md)。fixtures/sample.json 与 fixtures/frontier.json 均为合成小数据，不对应真实企业或个人。
运行环境为 Python，模块只使用标准库，无需 numpy/scipy。

读取并计算已有摘要：

    python -m app.service fixtures/sample.json

运行前沿分析：

    python -m app.frontier fixtures/frontier.json

前沿分析要点：

- 技术集合：lambda>=0、sum(lambda)=1、0<=z<=lambda、X lambda<=x、Y z>=y、B z=b。坏产出以等式进入弱可处置约束，不能像普通投入那样自由丢弃。
- 目标：max beta>=0，投影 (x-beta*gx, y+beta*gy, b-beta*gb)；输出同时保留投影、调整量和逐条约束残差。
- 最优 beta 唯一而最优权重可能多解。程序对每家企业求 z、lambda 在最优面中的最小/最大值，区分必然参照、可能参照和非参照，并为端点值保存通过校验的完整权重见证；不会用任意微扰挑一个解后隐藏多解。
- self_included 与 self_excluded（留一/超效率）分别求解并标明被评价者是否在参照池；超效率不可行、数据无效、方向无效与参照池为空是不同状态。
- 结果保留原始单位、归一化尺度、data_version 与 input_digest；有理数输出为整数或 "p/q" 文本，不产生浮点数。
- scenarios 逐项隔离方向、参照年度和参照口径的变化，只报告可观察到的分数差异来源。

运行全部测试：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests
