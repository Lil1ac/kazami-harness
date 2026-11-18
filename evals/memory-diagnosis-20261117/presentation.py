"""仅用于记忆重读诊断的候选输入呈现，复用已通过产品选择的记忆。"""

import json

from agent_harness.memory.service import MemoryController, encode


VERIFIED_FACT = "host_verified_files"  # 本次准备输入时，宿主核对了事实证据的完整文件指纹。
MATCHED_RULE = "applicable_rule"  # 当前适用范围由宿主判定，表示约定匹配而非文件核验。
_OPEN = "<project_memory>\n"
_CLOSE = "\n</project_memory>"
_CURRENT_FACT_GUIDE = "fact须核对证据"


def present_verified(controller: MemoryController, original: str) -> str:
    """返回增强后的原项目记忆段；无记忆时返回原来的空字符串。

    controller.selected 必须来自原选择流程，original 是同次 input_text 的文本。
    仅转换真正写入原投影的条目，不重新选择或复核，不修改运行状态和原始证据。
    产品段结构变化或原投影与选择结果不一致时抛出 ValueError，以保留实验口径。
    """
    if not original:
        return original
    if not original.startswith(_OPEN) or not original.endswith(_CLOSE):
        raise ValueError("候选呈现需要原项目记忆段")

    header, separator, serialized = original.removesuffix(_CLOSE).rpartition("\n")
    if not separator or _CURRENT_FACT_GUIDE not in header:
        raise ValueError("原项目记忆段缺少预期的事实说明或条目")
    rows = json.loads(serialized)
    if not isinstance(rows, list):
        raise ValueError("原记忆投影必须是条目列表")

    selected = {(item["id"], item["revision"]): item for item in controller.selected}
    for row in rows:
        item = selected.get((row["id"], row["revision"]))
        if item is None or row["type"] != item["type"] or row["usage_text"] != item["usage_text"]:
            raise ValueError("原记忆投影与本次选择结果不一致")

        if item["type"] == "fact" and item.get("status") == "active":
            evidence = item.get("evidence_refs", [])
            # 产品 applicability 只在全部指纹一致时才会选中带文件证据的事实。
            if evidence and all(ref.get("path") and ref.get("sha256") for ref in evidence):
                row["verification"] = {
                    "status": VERIFIED_FACT,
                    "evidence_paths": [ref["path"] for ref in evidence],
                    "meaning": "宿主本次准备输入时已核对所列相对路径的完整文件指纹，文件版本与确认时一致。"
                               "本条正文足够回答当前问题且无冲突时，可直接使用其中的事实。",
                }
        if item["type"] == "rule" and item.get("status") == "active":
            row["verification"] = {
                "status": MATCHED_RULE,
                "meaning": "宿主已判定本约定的范围对当前执行适用。正文完整覆盖当前动作时，可直接依照这条约定。",
            }

    header = header.replace(_CURRENT_FACT_GUIDE, "fact按本条 verification 判断是否已有宿主文件版本核验", 1)
    guide = (
        "宿主文件核验确认字节版本一致。事实未标明已核验、内容不足、疑似失效、与当前资料冲突或用户明确要求核对原文时，读取当前证据。"
        "编辑已有文件仍须使用当前目标文件的完整指纹与唯一匹配的旧片段。"
    )
    return header + "\n" + guide + "\n" + encode(rows) + _CLOSE
