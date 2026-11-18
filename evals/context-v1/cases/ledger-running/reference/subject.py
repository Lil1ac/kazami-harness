"""自建任务模块；输入输出契约见函数说明。"""

def solve(amounts, initial):
    """返回整数交易列表每一步的累计余额，起始为initial。 返回结构必须为list。

    参数（均为必填）：amounts：可正可负的整数交易额列表；initial：第一笔交易之前的整数余额。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    out = []
    for amount in amounts:
        initial += amount
        out.append(initial)
    return out
