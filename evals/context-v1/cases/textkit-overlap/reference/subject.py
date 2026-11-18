"""自建任务模块；输入输出契约见函数说明。"""

def solve(text, pattern):
    """统计子串pattern重叠出现次数；空pattern返回0。 返回结构必须为数值（int或float）。

    参数（均为必填）：text：待处理字符串，空串按上述需求处理；pattern：需计数的子串，空串按需求返回0。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return sum(text.startswith(pattern, i) for i in range(len(text))) if pattern else 0
