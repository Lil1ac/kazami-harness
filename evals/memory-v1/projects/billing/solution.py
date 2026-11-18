"""账单演练的初始实现，未满足的函数通过固定任务分别修正。"""

def legacy_payload(cents: int) -> dict:
    """返回 cents 的 V1 金额对象；当前使用待修正的字段。"""
    return {"raw_cents": cents}


def refund_v2(cents: int) -> dict:
    """返回 cents 对应的 V2 退款对象；当前格式是起点占位。"""
    return {"raw_cents": cents}


def round_v2(mills: int) -> dict:
    """把千分之一元 mills 转成 V2 金额；当前未应用确认规则。"""
    return {"raw_mills": mills}


def draft_amount(cents: int) -> dict:
    """返回 cents 的开发演练对象；起点未实现演练合同。"""
    return {"raw_cents": cents}
