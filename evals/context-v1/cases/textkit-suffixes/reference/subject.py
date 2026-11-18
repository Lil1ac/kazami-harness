"""自建任务模块；输入输出契约见函数说明。"""

def solve(paths):
    """从路径列表取最后扩展名的小写值，省略无扩展名路径，保留重复项和顺序。 返回结构必须为list。

    参数（均为必填）：paths：路径字符串列表，允许空列表。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    from pathlib import PurePosixPath
    return [PurePosixPath(x).suffix.lower() for x in paths if PurePosixPath(x).suffix]
