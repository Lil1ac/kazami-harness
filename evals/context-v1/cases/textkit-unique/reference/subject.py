"""自建任务模块；输入输出契约见函数说明。"""

def solve(items):
    """大小写敏感地去重词列表，保留首次出现顺序。 返回结构必须为list。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return list(dict.fromkeys(items))
