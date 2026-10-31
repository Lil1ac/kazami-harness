"""记忆测试的数据构造；统一使用当前结构，不模拟供应商或保留旧接口。"""

from agent_harness.memory import MemoryContent


def fact_fields(title: str, text: str) -> dict:
    """将测试的 title/text 明确建模为对象说明；返回当前接口字段，不补造生产知识。"""
    return {"title": title, "content": text, "type": "fact", "details": {"subject": title, "attribute": "说明", "value": text}}


def fact_content(title: str, content: str, **options) -> MemoryContent:
    """创建测试事实模型；options 显式覆盖类型、证据或范围，校验错误原样抛出。"""
    return MemoryContent(**{**fact_fields(title, content), **options})
