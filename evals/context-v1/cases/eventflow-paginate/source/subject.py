"""自建任务模块；输入输出契约见函数说明。"""

def solve(items, offset, limit):
    """对items从offset起取limit条，返回items和next_offset；取完后next_offset=None；offset非负、limit为正。 返回结构必须为dict。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表；offset：从0开始的分页起点，非负整数；limit：返回元素或字符数上限，范围按上述需求。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    raise NotImplementedError("待实现")
