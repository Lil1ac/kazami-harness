"""自建任务模块；输入输出契约见函数说明。"""

def solve(base, attempt, cap):
    """返回第attempt次重试延迟min(base*2**attempt, cap)，attempt从0开始，所有参数非负。 返回结构必须为数值（int或float）。

    参数（均为必填）：base：重试基础延迟，非负且与cap同单位；attempt：从0开始的重试序号，非负整数；cap：延迟上限，非负且与base同单位。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return min(base * 2 ** attempt, cap)
