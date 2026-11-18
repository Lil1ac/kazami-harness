"""自建任务模块；输入输出契约见函数说明。"""

def solve(items, size):
    """把items切成最多size个元素的连续批次；size为正整数，空输入返回空列表。 返回结构必须为list。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表；size：每批元素数，正整数。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return [items[i:i + size] for i in range(0, len(items), size)]
