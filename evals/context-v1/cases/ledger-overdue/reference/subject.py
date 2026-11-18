"""自建任务模块；输入输出契约见函数说明。"""

def solve(rows, today):
    """返回due日期早于today且paid为False的id，按输入顺序；日期都是ISO YYYY-MM-DD。 返回结构必须为list。

    参数（均为必填）：rows：含上述字段的记录列表，允许空列表；today：用于比较的ISO日期YYYY-MM-DD。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return [row['id'] for row in rows if not row['paid'] and row['due'] < today]
