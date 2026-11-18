"""自建任务模块；输入输出契约见函数说明。"""

def solve(price, percent):
    """整数分price按整数百分比percent折扣；返回折后整数分，半分向上取整。 返回结构必须为数值（int或float）。

    参数（均为必填）：price：折扣前整数分金额；percent：折扣百分数，100表示全部折扣。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    from decimal import Decimal, ROUND_HALF_UP
    return int((Decimal(price) * (100 - percent) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
