"""自建任务模块；输入输出契约见函数说明。"""

def solve(items):
    """按首个/之前的前缀分组字符串；无/则用整个字符串，保留每组输入顺序。 返回结构必须为字典：前缀为键、该组字符串列表为值。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    out = {}
    for item in items:
        out.setdefault(item.split('/', 1)[0], []).append(item)
    return out
