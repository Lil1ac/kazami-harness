"""自建任务模块；输入输出契约见函数说明。"""

def solve(rows, now):
    """给定now整数秒，筛出expires_at为None或严格大于now的记录，保留顺序。 返回结构必须为list。

    参数（均为必填）：rows：含上述字段的记录列表，允许空列表；now：当前时间，整数秒。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    raise NotImplementedError("待实现")
