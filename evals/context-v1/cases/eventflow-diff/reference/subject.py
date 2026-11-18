"""自建任务模块；输入输出契约见函数说明。"""

def solve(before, after):
    """比较两个字典，返回added/removed/changed键列表，均按名称排序；changed只含共同键且值不同。 返回结构必须为dict。

    参数（均为必填）：before：变更前字典；after：变更后字典。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return {'added': sorted(after.keys() - before.keys()), 'removed': sorted(before.keys() - after.keys()), 'changed': sorted(k for k in before.keys() & after.keys() if before[k] != after[k])}
