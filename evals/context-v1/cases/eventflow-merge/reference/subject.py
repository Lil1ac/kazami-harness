"""自建任务模块；输入输出契约见函数说明。"""

def solve(intervals):
    """合并闭区间，端点相接也合并；输入无序，返回按起点排序的区间列表。 返回结构必须为list。

    参数（均为必填）：intervals：起点不大于终点的闭区间列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return out
