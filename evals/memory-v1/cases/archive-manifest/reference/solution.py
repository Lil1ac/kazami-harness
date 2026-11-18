"""归档演练的起点；只执行传入的文件名数据。"""

def normalize_names(items: list[str]) -> list[str]:
    """规范化 items 中的文件名；起点保留原输入。"""
    return list(items)


def build_manifest(paths: list[str]) -> dict:
    """把 paths 规范为斜杠路径，返回排序后的唯一列表及数量。"""
    files = sorted({path.replace("\\", "/") for path in paths})
    return {"files": files, "count": len(files)}
