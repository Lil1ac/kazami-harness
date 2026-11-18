# eventflow

返回第attempt次重试延迟min(base*2**attempt, cap)，attempt从0开始，所有参数非负。 返回结构必须为数值（int或float）。
