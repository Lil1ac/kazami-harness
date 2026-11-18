"""自建任务模块；输入输出契约见函数说明。"""

def solve(text, limit):
    """按Unicode字符截断到limit总长度；超长时末尾加省略号…且计入总长度，limit为非负整数。 返回结构必须为str。

    参数（均为必填）：text：待处理字符串，空串按上述需求处理；limit：返回元素或字符数上限，范围按上述需求。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return text if len(text) <= limit else (text[:limit - 1] + '…' if limit else '')
