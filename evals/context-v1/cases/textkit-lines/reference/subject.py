"""自建任务模块；输入输出契约见函数说明。"""

def solve(text):
    """统一CRLF/CR为LF，删除空白行，保留非空行的原始空白。 返回结构必须为list。

    参数（均为必填）：text：待处理字符串，空串按上述需求处理。
    返回上述约定结构；参考实现无外部副作用，起点占位实现抛NotImplementedError。
    """
    return [line for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n') if line.strip()]
