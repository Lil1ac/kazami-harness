"""SQLite 保存公开连接与审核状态；更新以指纹比较保证并发编辑不会丢失。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from agent_harness.mcp.models import McpServerConfig
from agent_harness.storage.sessions import payload_digest
from agent_harness.work.tasks import TaskError


def tool_name(server_id: str, name: str) -> str:
    """把连接 server_id 与远端工具 name 编码为稳定模型名；返回短哈希命名空间。"""
    return "mcp_" + payload_digest([server_id, name])[:24]


def fingerprint(record: dict) -> str:
    """计算 record 的配置、凭据代次、目录与研究授权指纹；状态和错误文本不参与。"""
    return payload_digest({key: record[key] for key in ("config", "revision", "catalog", "research_tools")})


class McpStore:
    """跨入口共享连接存储；读取不会启动服务或加载凭据。"""

    def __init__(self, runs_root: Path) -> None:
        """绑定 runs_root 下扩展数据库；仅保存路径，返回 None，不创建文件。"""
        self.path = runs_root / ".extensions" / "mcp.sqlite3"  # 运行记录旁独立目录，不写入项目。

    @contextmanager
    def transaction(self):
        """打开串行写事务并返回连接；异常回滚，数据库错误转为受控存储异常。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=10) as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS servers (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        except sqlite3.Error:
            raise TaskError("mcp_storage_error", "外部连接记录暂时无法读写。") from None

    def list(self) -> list[dict]:
        """返回所有公开连接的新副本；数据库不存在时返回空列表，不产生目录。"""
        if not self.path.exists():
            return []
        with self.transaction() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM servers ORDER BY id")]

    def get(self, identifier: str) -> dict:
        """按 identifier 返回公开记录；不存在抛 mcp_not_found，不触发网络。"""
        item = next((item for item in self.list() if item["config"]["id"] == identifier), None)
        if item is None:
            raise TaskError("mcp_not_found", "外部连接不存在。")
        return item

    def save(self, config: McpServerConfig, expected: str | None) -> dict:
        """按 expected 指纹新增或替换 config；替换递增代次并撤销旧审核、目录和启用状态。"""
        with self.transaction() as connection:
            row = connection.execute("SELECT body FROM servers WHERE id=?", (config.id,)).fetchone()
            old = json.loads(row[0]) if row else None
            if (old and old["fingerprint"] != expected) or (not old and expected is not None):
                raise TaskError("mcp_stale", "连接版本已变化，请刷新后重新审核。")
            value = {"config": config.model_dump(), "revision": old["revision"] + 1 if old else 1,
                     "trusted": False, "enabled": False, "catalog": [], "research_tools": [],
                     "status": "untrusted", "error": None, "protocol_version": None}
            value["fingerprint"] = fingerprint(value)
            connection.execute("INSERT OR REPLACE INTO servers VALUES (?,?)", (config.id, json.dumps(value, ensure_ascii=False)))
            return value

    def update(self, identifier: str, expected: str, changes: dict) -> dict:
        """仅在 identifier 当前指纹等于 expected 时应用宿主 changes；返回重新计算的公开记录。"""
        with self.transaction() as connection:
            row = connection.execute("SELECT body FROM servers WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise TaskError("mcp_not_found", "外部连接不存在。")
            value = json.loads(row[0])
            if value["fingerprint"] != expected:
                raise TaskError("mcp_stale", "连接或工具定义已变化，请刷新后重新审核。")
            if any(key in changes for key in ("trusted", "enabled", "catalog", "research_tools", "trusted_identity")):
                value["revision"] += 1  # 撤权再启用也不能恢复原指纹，旧审批永远不复活。
            value.update(changes)
            value["fingerprint"] = fingerprint(value)
            connection.execute("UPDATE servers SET body=? WHERE id=?", (json.dumps(value, ensure_ascii=False), identifier))
            return value

    def options(self) -> list[dict]:
        """返回启用且已审核的工具选择目录；每项绑定服务指纹，不读取密钥或连接网络。"""
        return [{"name": tool_name(item["config"]["id"], tool["name"]), "fingerprint": item["fingerprint"],
                 "server_id": item["config"]["id"], "server_label": item["config"]["label"],
                 "tool_name": tool["name"], "description": tool.get("description", ""),
                 "research": tool["name"] in item["research_tools"]}
                for item in self.list() if item["trusted"] and item["enabled"] for tool in item["catalog"]]
