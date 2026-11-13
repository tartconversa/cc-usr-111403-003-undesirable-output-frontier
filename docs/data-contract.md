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

## 当前能力

app.baseline.analyze(snapshot)产生已有摘要，app.service.inspect负责绑定输入身份；新增分析可以增设独立入口，已有接口及上述数据语义保持兼容。
