"""归档演练的起点；只执行传入的文件名数据。"""

def normalize_names(items: list[str]) -> list[str]:
    """从 items 生成去空白、统一大小写且去重的有序文件名。"""
    values = {item.strip().casefold() for item in items}
    return sorted(values - {""})


def build_manifest(paths: list[str]) -> dict:
    """按 paths 生成清单；起点缺少规范化和数量。"""
    return {"files": list(paths)}
