"""自建任务模块；输入输出契约见函数说明。"""

def solve(items, n):
    """按金额降序、同金额名称升序取前n个(name,amount)记录，返回列表的列表。 返回结构必须为list。

    参数（均为必填）：items：按上述元素约束提供的输入列表，允许空列表；n：分配份数或排名上限，范围按上述需求。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return [list(x) for x in sorted(items, key=lambda x: (-x[1], x[0]))[:n]]
