"""把已确认记忆组织成可重建主题；视图不拥有独立事实。"""

from agent_harness.memory.types import MemoryType


_TYPE_LABELS: dict[MemoryType, str] = {
    "rule": "项目约定", "decision": "项目决策", "fact": "已确认事实", "reference": "查阅入口", "experience": "执行经验",
}  # 没有整理主题标签时，使用用户已熟悉的类型名称。


def project_topics(items: list[dict], query: str = "") -> list[dict]:
    """以 items 的整理主题和类型聚合已生效条目；query 为空浏览，返回可回读的编号与修订。"""
    groups: dict[str, list[dict]] = {}
    for item in items:
        if item["status"] != "active" or item.get("unavailable_reason"):
            continue
        topic = item["source"].get("topic") or _TYPE_LABELS[item["type"]]
        entry = {key: item[key] for key in ("id", "revision", "title", "type")}
        groups.setdefault(topic, []).append(entry)
    needle = query.strip().casefold()
    values = []
    for label, entries in sorted(groups.items()):
        ordered = sorted(entries, key=lambda item: (item["title"], item["id"]))
        if not needle or needle in label.casefold() or any(needle in item["title"].casefold() for item in ordered):
            values.append({"title": label, "items": ordered[:20], "count": len(ordered)})
    return values


def topic_outline(topics: list[dict], limit_bytes: int = 2048) -> list[dict]:
    """构造 topics 的短索引；limit_bytes 为 UTF-8 JSON 上限，超出时按主题截去。"""
    from agent_harness.memory.service import encode

    result = []
    for topic in topics:
        short = {"title": topic["title"], "count": topic["count"], "items": topic["items"][:2]}
        if len(encode([*result, short]).encode("utf-8")) > limit_bytes:
            break
        result.append(short)
    return result
