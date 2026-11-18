"""分离环境证据、实验行为和计费审计；未知源码变化仍由恢复审查阻断。"""

import hashlib
import json
from pathlib import Path


def digest(value: object) -> str:
    """返回 value 的规范 JSON 摘要；不访问磁盘，不接受凭据作为输入。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def file_hash(path: Path) -> str:
    """返回普通文件 path 的字节摘要；拒绝链接，缺失或不可读时抛异常。"""
    if path.resolve() != path.absolute():
        raise ValueError("证据路径包含链接")
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 环境执行的显式保守依赖；运行时全量清单另外保留，新增未归类依赖必须审查。
ENVIRONMENT_SOURCES = (
    "evaluation/preflight.py", "evaluation/protocols.py", "evaluation/repository.py", "evaluation/reproduction.py",
    "evaluation/entry_observer.py",
    "evaluation/swe_worker.py", "evaluation/public_self_test.py", "execution/processes.py",
    "execution/python.py", "execution/workspace.py", "execution/scheduling.py",
    "schemas.py", "storage/runs.py",
)
PROCESS_LIMITS = ("command_timeout_seconds", "cleanup_grace_seconds",
                  "tool_output_preview_chars", "max_process_capture_bytes")  # 仅预检实际使用的进程限额。


def environment_contract(record: dict, config: dict, *, public_self_test: bool = False,
                         sources: dict | None = None) -> dict:
    """按 record、配置及显式依赖构造单题环境契约；返回可哈希对象，不拉镜像。

    sources 为源码清单，None 读取当前依赖。public_self_test 为额外公开自测门槛，默认关闭。
    镜像 ID、官方脚本、解包基线等实际材质由独立证据封存并在付费 prepare 时再次核对。
    """
    root = Path(__file__).resolve().parents[1]
    dependencies = {name: sources[name] if sources is not None else file_hash(root / name)
                    for name in ENVIRONMENT_SOURCES}
    return {"version": 1, "record": record, "dependencies": dependencies,
            "limits": {key: config["limits"][key] for key in PROCESS_LIMITS},
            "public_self_test": public_self_test, "grader": "swebench==4.1.0",
            "docker": {"cpus": 2, "memory": 4294967296, "network": "none", "pids": 256}}


def identities(protocol: dict) -> dict:
    """分列整轮 protocol 的审计身份；计费代码也纳入执行身份，因为它能改变准入和停止行为。"""
    runtime = protocol["runtime"]
    return {"environment": digest({name: runtime[name] for name in ENVIRONMENT_SOURCES}),
            "execution": digest({"config": protocol["config"], "groups": protocol["groups"],
                                 "runtime": runtime, "mini": protocol["mini"]}),
            "accounting": digest({"limits": protocol["phase_limits"], "max_usd": protocol["max_usd"],
                                  "budget": {name: runtime[name] for name in
                                             ("evaluation/budget.py", "evaluation/campaign_budget.py")}})}
