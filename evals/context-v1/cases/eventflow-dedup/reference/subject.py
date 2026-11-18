"""自建任务模块；输入输出契约见函数说明。"""

def solve(rows):
    """按事件id去重，保留第一次出现的完整事件和输入顺序。 返回结构必须为list。

    参数（均为必填）：rows：含上述字段的记录列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    seen = set()
    out = []
    for row in rows:
        if row['id'] not in seen:
            seen.add(row['id'])
            out.append(row)
    return out
