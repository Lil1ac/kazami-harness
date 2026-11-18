"""自建任务模块；输入输出契约见函数说明。"""

def solve(times, width):
    """以width宽度分桶整数时间戳，返回桶起点字符串到数量；负数向下取整，width为正。 返回结构必须为dict。

    参数（均为必填）：times：整数时间戳列表，允许负数；width：时间桶宽度，正整数且与时间戳同单位。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    out = {}
    for value in times:
        key = str(value // width * width)
        out[key] = out.get(key, 0) + 1
    return out
