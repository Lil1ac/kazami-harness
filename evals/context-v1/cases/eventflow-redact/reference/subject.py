"""自建任务模块；输入输出契约见函数说明。"""

def solve(value):
    """递归处理字典与列表，将键名大小写不敏感为password/token的值替换为***，不修改其他值。 返回结构必须为dict。

    参数（均为必填）：value：待脱敏的任意值，None及非容器值原样返回。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    if isinstance(value, dict):
        return {k: '***' if k.lower() in {'password', 'token'} else solve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [solve(v) for v in value]
    return value
