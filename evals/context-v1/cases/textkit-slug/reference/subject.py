"""自建任务模块；输入输出契约见函数说明。"""

def solve(text):
    """把字符串转小写，将连续非ASCII字母数字替换为一个短横线，移除两端短横线。 返回结构必须为str。

    参数（均为必填）：text：待处理字符串，空串按上述需求处理。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    import re
    return re.sub('[^a-z0-9]+', '-', text.lower()).strip('-')
