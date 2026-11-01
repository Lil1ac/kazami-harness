"""从现有运行和会话记录构建只读导航索引，不保存第二份会话事实。"""

import os
from typing import TYPE_CHECKING
from uuid import UUID

from agent_harness.storage.sessions import SessionError
from agent_harness.storage.runs import StorageError
from agent_harness.web.schemas import ConversationPage, ConversationRunState, ConversationSource, ConversationSummary

if TYPE_CHECKING:
    from agent_harness.storage.runs import RunHistory
    from agent_harness.web.service import WebRunManager
    from agent_harness.web.schemas import RunView


class ConversationIndex:
    """先核对并聚合完整会话，再做项目筛选、排序和分页。"""

    def __init__(self, manager: "WebRunManager") -> None:
        """绑定唯一manager；无目录创建、文件写入、Git探测或模型调用。"""
        self.manager = manager  # 复用运行缓存和会话元数据，不维护单独的可变索引。

    def _registry(self) -> dict[tuple[str, str, str], str]:
        """返回当前登记身份到名称的快照；规范路径防止编号重用导致跨项目混入。"""
        projects, workspaces, _ = self.manager.catalog.snapshot()
        return {
            (kind, item.id, os.path.normcase(str(item.path.resolve()))): item.label
            for kind, entries in (("project", projects), ("workspace", workspaces))
            for item in entries
        }

    def source_for(self, history: "RunHistory", registry: dict | None = None) -> ConversationSource | None:
        """按history明确来源返回项目身份；registry=None读取当前登记，无来源时返回None。"""
        origin = history.manifest.origin
        if origin is None:
            return None
        registry = self._registry() if registry is None else registry
        key = (origin.kind, origin.preset_id, os.path.normcase(str(origin.registered_path)))
        return ConversationSource(
            id=origin.preset_id, kind=origin.kind, path=str(origin.registered_path), execution_mode=origin.execution_mode,
            label=registry.get(key) or origin.registered_path.name or str(origin.registered_path),
            registered=key in registry,
        )

    def _summary(self, session_id: UUID, entries: list[tuple], registry: dict) -> ConversationSummary:
        """把同session_id的可读清单与权威轮次表核对，返回一条摘要；损坏时保留可读入口。"""
        by_id = {view.run_id: (view, history) for view, history in entries}
        seed, history = min(entries, key=lambda pair: (pair[0].turn_index, pair[0].created_at))
        root = by_id.get(session_id, (seed, history))
        first, first_history = root
        source = self.source_for(first_history, registry)
        latest_id, count, latest, error = None, None, None, None
        try:
            record = self.manager.conversations.store.read(session_id)
            latest_id, count = record.run_ids[-1], len(record.run_ids)
            if session_id not in by_id or any(identifier not in record.run_ids for identifier in by_id):
                raise SessionError("session_unreadable", "会话关联记录无法核对，请打开会话检查。")
            for index, identifier in enumerate(record.run_ids):
                pair = by_id.get(identifier)
                if pair is None:
                    raise SessionError("session_unreadable", "会话包含缺失或不可读的轮次，请打开会话检查。")
                view, item_history = pair
                manifest = item_history.manifest
                if manifest.parent_run_id != (record.run_ids[index - 1] if index else None) or manifest.turn_index != index + 1:
                    raise SessionError("session_unreadable", "会话轮次关联不一致，请打开会话检查。")
                item_source = self.source_for(item_history, registry)
                if item_source != source:
                    raise SessionError("session_unreadable", "会话项目来源不一致，请打开会话检查。")
            view = by_id[latest_id][0]
            latest = ConversationRunState(
                **{key: getattr(view.result, key) for key in ("execution_status", "validation_status", "cleanup_status")},
                **{key: getattr(view, key) for key in ("active", "cancelling", "interrupted", "phase")},
            )
            if view.warnings:
                error = "运行记录存在异常，请打开会话查看提示。"
        except SessionError as failure:
            error = failure.message
        updated = max(item.events[-1].time_utc if item.events else view.created_at for view, item in entries)
        return ConversationSummary(
            session_id=session_id, title=" ".join(first.prompt.split())[:200] or "未命名会话",
            entry_run_id=latest_id if latest is not None else first.run_id,
            latest_run_id=latest_id, turn_count=count, created_at=first.created_at,
            updated_at=updated, source=source, latest=latest, error=error,
        )

    def list(self, offset: int, limit: int, project_id: str | None = None, status: str | None = None) -> ConversationPage:
        """返回聚合后的会话页；offset从0、limit为1..100，项目筛选匹配当前完整来源身份。

        project_id=None查看全部历史（包括已移除登记），status=None不过滤最新执行状态。
        非法记录不导致整个列表失败；列表只读，不创建会话、触碰源码或启动执行。
        """
        from agent_harness.web.service import WebServiceError

        registry = self._registry()
        if project_id is not None and not any(key[1] == project_id for key in registry):
            raise WebServiceError(404, "unknown_project", "项目登记不存在，请从全部会话查找历史或重新打开文件夹。")
        groups: dict[UUID, list[tuple]] = {}
        if self.manager.runs_root.is_dir():
            for directory in self.manager.runs_root.iterdir():
                if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
                    continue
                try:
                    identifier = UUID(directory.name)
                    view = self.manager.get_run(identifier)
                    history = self.manager._read_history(identifier)
                except (ValueError, StorageError, WebServiceError):
                    continue
                groups.setdefault(view.session_id or identifier, []).append((view, history))
        summaries = [self._summary(identifier, entries, registry) for identifier, entries in groups.items()]
        if project_id is not None:
            summaries = [item for item in summaries if item.source is not None and item.source.id == project_id and item.source.registered]
        if status is not None:
            summaries = [item for item in summaries if item.latest is not None and item.latest.execution_status == status]
        summaries.sort(key=lambda item: (item.updated_at, str(item.session_id)), reverse=True)
        return ConversationPage(items=summaries[offset:offset + limit], total=len(summaries), busy_run_id=self.manager.busy_run_id)
