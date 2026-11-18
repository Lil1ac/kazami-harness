"""自建任务模块；输入输出契约见函数说明。"""

def solve(rows, completed):
    """返回依赖deps都在completed中的任务id，排除自身已完成项，保持输入顺序。 返回结构必须为list。

    参数（均为必填）：rows：含上述字段的记录列表，允许空列表；completed：已完成任务id列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    raise NotImplementedError("待实现")
