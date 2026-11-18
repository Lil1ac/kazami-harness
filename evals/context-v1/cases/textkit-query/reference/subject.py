"""自建任务模块；输入输出契约见函数说明。"""

def solve(text):
    """解析URL查询字符串为键到所有值的列表，保留空值并解码加号与百分号。 返回结构必须为dict。

    参数（均为必填）：text：待处理字符串，空串按上述需求处理。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    from urllib.parse import parse_qs
    return parse_qs(text, keep_blank_values=True)
