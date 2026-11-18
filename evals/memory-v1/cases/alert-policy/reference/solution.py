"""告警演练的起点，各入口可独立修改。"""

def alert_route(level: str) -> str:
    """返回 level 对应的告警目的地；当前统一返回占位。"""
    return "unconfigured"


def alert_envelope(level: str, minutes: int) -> dict:
    """按 level 和持续 minutes 生成告警对象；起点字段待修复。"""
    return {"level": level, "minutes": minutes}


def webhook_policy() -> str:
    """返回迁移后确认的 webhook 重试策略标识。"""
    return "exp-3"


def demo_route() -> str:
    """返回开发演练的路由；起点尚未实现。"""
    return "unknown"


def demo_ttl() -> int:
    """返回开发演练的秒数；起点尚未实现。"""
    return 0
