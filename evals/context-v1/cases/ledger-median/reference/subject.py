"""自建任务模块；输入输出契约见函数说明。"""

def solve(items):
    """返回非空整数列表的中位数；偶数长度返回中间两值平均数，不修改输入。 返回结构必须为数值（int或float）。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    values = sorted(items)
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
