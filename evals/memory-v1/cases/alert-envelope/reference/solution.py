"""告警演练的起点，各入口可独立修改。"""

def alert_route(level: str) -> str:
    """返回 level 对应的告警目的地；当前统一返回占位。"""
    return "unconfigured"


def alert_envelope(level: str, minutes: int) -> dict:
    """按 level 生成严重等级，把 minutes 换算成秒。"""
    return {"severity": "P1" if level == "high" else "P3", "ttl_s": minutes * 60}


def webhook_policy() -> str:
    """返回当前 webhook 重试策略；起点是待纠正旧值。"""
    return "linear-5"


def demo_route() -> str:
    """返回开发演练的路由；起点尚未实现。"""
    return "unknown"


def demo_ttl() -> int:
    """返回开发演练的秒数；起点尚未实现。"""
    return 0
