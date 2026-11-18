"""自建任务模块；输入输出契约见函数说明。"""

def solve(rows, start, end):
    """返回时间戳ts属于[start,end)的事件，保持顺序。 返回结构必须为list。

    参数（均为必填）：rows：含上述字段的记录列表，允许空列表；start：时间窗口起点，包含边界且与事件ts同单位；end：时间窗口终点，不包含边界且与事件ts同单位。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return [row for row in rows if start <= row['ts'] < end]
