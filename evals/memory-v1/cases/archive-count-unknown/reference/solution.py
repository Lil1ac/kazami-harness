"""归档演练的起点；只执行传入的文件名数据。"""

def normalize_names(items: list[str]) -> list[str]:
    """规范化 items 中的文件名；起点保留原输入。"""
    return list(items)


def build_manifest(paths: list[str]) -> dict:
    """按 paths 生成清单；起点缺少规范化和数量。"""
    return {"files": list(paths)}
