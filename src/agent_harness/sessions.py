"""会话关联、追加请求幂等与历史重建；不续写旧运行，不自动重放任何工具。"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from .schemas import DataModel
from .store import RunHistory


class SessionError(Exception):
    """携带可展示的稳定会话原因，不回显磁盘异常或请求正文。"""

    def __init__(self, code: str, message: str) -> None:
        """保存 code 与安全 message，供 Web 层映射为明确错误；返回 None。"""
        self.code = code  # 稳定原因码。
        self.message = message  # 受控中文说明。
        super().__init__(message)


# 请求意图先于工作区准备落盘，重启后禁止盲目重新执行尚未确认的请求。
class SubmissionIntent(DataModel):
    parent_run_id: UUID  # 本次请求指定的父运行。
    run_id: UUID  # 分配给本次请求的唯一新运行编号。
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")  # 包括新要求和配置的规范载荷摘要。
    prompt: str  # 仅供尚未建立 Runner 时展示准备状态。
    state: Literal["preparing", "committed", "failed"] = "preparing"  # committed 后不自动再次执行。
    error_code: str | None = None  # 准备失败时的稳定原因。
    error_message: str | None = None  # 准备失败时可读提示。


# 一个文件原子保存会话顺序和幂等请求，不用第二个写入者修改 RunStore。
class SessionRecord(DataModel):
    session_id: UUID  # 等于会话首轮 run_id。
    run_ids: list[UUID] = Field(min_length=1)  # 已提交的用户轮次顺序，末项为最新一轮运行。
    requests: dict[str, SubmissionIntent] = Field(default_factory=dict)  # UUID 请求编号到固定意图。


class SessionStore:
    """管理受保护的会话旁路元数据；调用方使用 Web 的单槽锁串行化变更。"""

    MAX_BYTES = 2 * 1024 * 1024  # 单个会话元数据最多2MiB，避免无界读取。

    def __init__(self, runs_root: Path) -> None:
        """绑定 runs_root，不创建目录；正常 UUID 运行与会话元数据分别存放。"""
        self.runs_root = runs_root.resolve()  # 受保护运行根目录。
        self.root = self.runs_root / '.sessions'  # 仅保存会话 JSON，不向模型工具开放。

    def _path(self, session_id: UUID) -> Path:
        """生成 session_id 的安全固定路径；链接或目录替换抛 SessionError，不创建文件。"""
        path = self.root / (str(UUID(str(session_id))) + '.json')
        if self.root.resolve() != self.root or path.resolve() != path or self.root.is_symlink() or path.is_symlink():
            raise SessionError('session_unreadable', '会话记录路径发生变化，请检查本地记录。')
        return path

    def read(self, session_id: UUID) -> SessionRecord:
        """读取 session_id；尚无元数据时返回只含首轮的视图，不写文件或执行任务。"""
        try:
            path = self._path(session_id)
            if not path.exists():
                return SessionRecord(session_id=session_id, run_ids=[session_id])
            with path.open('rb') as source:
                raw = source.read(self.MAX_BYTES + 1)
            if len(raw) > self.MAX_BYTES:
                raise ValueError('metadata limit')
            payload = json.loads(raw)
            # 普通请求旧元数据只读核对后移除；真实旧恢复意图和不一致轮次仍拒绝。
            requests = payload.get("requests") if isinstance(payload, dict) else None
            run_ids = payload.get("run_ids", []) if isinstance(payload, dict) else []
            if isinstance(requests, dict) and isinstance(run_ids, list):
                for item in requests.values():
                    if not isinstance(item, dict) or item.get("kind", "followup") != "followup":
                        continue
                    parent = item.get("parent_run_id")
                    ordinal = run_ids.index(parent) + 2 if parent in run_ids else None
                    defaults = {"kind": "followup", "checkpoint_id": None}
                    for key, default in defaults.items():
                        if key in item and type(item[key]) is type(default) and item[key] == default:
                            del item[key]
                    # 曾保存的冗余轮次必须与线性父关系一致；不能把同轮其他执行洗成新要求。
                    for key, expected in (("turn_index", ordinal), ("attempt_index", 1)):
                        if key in item and (
                            item[key] is None
                            or (expected is not None and type(item[key]) is int and item[key] == expected)
                        ):
                            del item[key]
            record = SessionRecord.model_validate_json(json.dumps(payload, ensure_ascii=False, allow_nan=False))
            if record.session_id != session_id or record.run_ids[0] != session_id or len(set(record.run_ids)) != len(record.run_ids):
                raise ValueError('invalid relation')
            for key in record.requests:
                UUID(key)
            return record
        except SessionError:
            raise
        except (OSError, ValueError, ValidationError):
            raise SessionError('session_unreadable', '会话关联记录无法核对，请检查本地记录。') from None

    def _write(self, record: SessionRecord) -> None:
        """原子发布 record；提交前刷新临时文件，失败抛 SessionError，不执行外部动作。"""
        temporary = None
        try:
            path = self._path(record.session_id)
            self.root.mkdir(parents=True, exist_ok=True)
            self._path(record.session_id)
            raw = record.model_dump_json(indent=2).encode('utf-8')
            if len(raw) > self.MAX_BYTES:
                raise ValueError('metadata limit')
            temporary = self.root / ('.pending-' + str(uuid4()))
            with temporary.open('xb') as target:
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        except SessionError:
            raise
        except (OSError, ValueError):
            raise SessionError('session_write_failed', '无法保存会话状态，尚未确认的请求不会自动重放。') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def lookup(self, session_id: UUID, request_id: UUID, digest: str) -> SubmissionIntent | None:
        """按 session/request 查询已有意图；相同编号但不同载荷抛冲突，未找到返回 None。"""
        intent = self.read(session_id).requests.get(str(request_id))
        if intent is not None and intent.digest != digest:
            raise SessionError('request_conflict', '该请求编号已用于不同内容，请核对后重新提交。')
        return intent

    def begin(
        self, session_id: UUID, request_id: UUID, parent_id: UUID, run_id: UUID, digest: str, prompt: str,
    ) -> None:
        """派生工作区前登记session/request的唯一意图；成功原子写入并返回None。

        parent_id须为最新轮；run_id是分配的新轮编号，digest固定完整载荷，
        prompt仅供准备状态展示。已有未确认请求或编号冲突时拒绝，不执行工作区操作。
        """
        record = self.read(session_id)
        if record.run_ids[-1] != parent_id:
            raise SessionError('session_stale_parent', '只能从会话最新一轮继续，请先打开最新运行。')
        if str(request_id) in record.requests:
            raise SessionError('request_pending', '该追加请求已登记，请查看原请求状态。')
        if any(item.state == 'preparing' for item in record.requests.values()):
            raise SessionError('request_uncertain', '会话存在尚未确认的追加准备，请核对后再继续。')
        record.requests[str(request_id)] = SubmissionIntent(parent_run_id=parent_id, run_id=run_id, digest=digest, prompt=prompt)
        self._write(record)

    def commit(self, session_id: UUID, request_id: UUID) -> None:
        """准备完成后固定新轮关系，随后只允许启动一次 Runner；崩溃后读取记录而不重放。"""
        record = self.read(session_id)
        intent = record.requests[str(request_id)]
        if intent.state != 'preparing' or record.run_ids[-1] != intent.parent_run_id:
            raise SessionError('request_conflict', '追加请求状态已变化，未启动新的执行。')
        record.run_ids.append(intent.run_id)
        intent.state = 'committed'
        self._write(record)

    def fail(self, session_id: UUID, request_id: UUID, code: str, message: str) -> None:
        """把未提交意图标为准备失败；已提交意图保持原样，禁止以重试方式再次执行。"""
        record = self.read(session_id)
        intent = record.requests.get(str(request_id))
        if intent is not None and intent.state == 'preparing':
            intent.state, intent.error_code, intent.error_message = 'failed', code, message
            self._write(record)


def payload_digest(value: dict | list) -> str:
    """按紧凑 UTF-8 JSON 计算 value 的稳定摘要；不读取凭据，不接受 NaN。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()


def history_messages(history: RunHistory, *, allow_interrupted: bool = False) -> list[dict]:
    """从可信父 history 还原完整会话消息；保留工具配对，移除旧宿主/指令前缀。

    子运行的 run_started 已包含之前历史，因此只重建直接父轮，避免递归重复拼接。
    allow_interrupted默认False，仍要求可信终态；宿主明确接纳中断记录时才设True。
    此时为尾部未配对调用补充未知/未开始标记，绝不执行工具或改写原记录。
    损坏事件或中间批次失配仍抛SessionError；返回独立消息副本。
    """
    interrupted = history.result is None and history.interrupted
    if history.warnings or ((history.result is None or history.interrupted) and not (allow_interrupted and interrupted)):
        raise SessionError('history_unverified', '前轮记录缺少可核对的完整事件，不能直接继续。')
    starts = [event for event in history.events if event.type == 'run_started']
    if len(starts) != 1:
        raise SessionError('history_unverified', '前轮没有完整模型输入记录，不能继承历史。')
    payload = starts[0].payload
    initial = payload.get('messages')
    loaded = any(event.type == 'context_prepared' and isinstance(event.payload.get('instruction'), dict) and event.payload['instruction'].get('status') == 'loaded' for event in history.events)
    offset = payload.get('conversation_start', 2 if loaded else 1)
    if not isinstance(initial, list) or type(offset) is not int or offset not in (1, 2) or len(initial) <= offset:
        raise SessionError('history_unverified', '前轮模型输入格式无法核对。')
    messages = deepcopy(initial[offset:])
    for event in history.events:
        if event.seq <= starts[0].seq:
            continue
        if event.type == 'assistant_message':
            messages.append(deepcopy(event.payload.get('message')))
        elif event.type == 'tool_result':
            if not isinstance(event.payload.get('result'), dict) or type(event.payload['result'].get('ok')) is not bool:
                raise SessionError('history_unverified', '历史工具结果格式无法核对。')
            messages.append({'role': 'tool', 'tool_call_id': event.payload.get('call_id'), 'content': json.dumps(event.payload['result'], ensure_ascii=False, allow_nan=False)})
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'tool'):
            raise SessionError('history_unverified', '历史消息角色无法核对。')
        if message['role'] == 'tool':
            identifier = message.get('tool_call_id')
            if not isinstance(identifier, str) or identifier not in pending:
                raise SessionError('history_unverified', '历史工具调用与结果不匹配。')
            pending.remove(identifier)
        else:
            if pending:
                raise SessionError('history_unverified', '历史工具批次结果不完整。')
            if message['role'] == 'assistant':
                calls = message.get('tool_calls') or []
                try:
                    identifiers = [call['id'] for call in calls]
                    if any(not isinstance(item, str) or not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
                        raise ValueError('call ids')
                    pending.update(identifiers)
                except (TypeError, KeyError, ValueError):
                    raise SessionError('history_unverified', '历史工具标识无法核对。') from None
    if pending and interrupted and allow_interrupted:
        started = {event.payload.get('call_id') for event in history.events if event.type == 'tool_started'}
        # 只补最后一批缺失结果；前面不完整的批次已经在角色配对检查中拒绝。
        for call in calls:
            identifier = call['id']
            if identifier not in pending:
                continue
            unknown = identifier in started
            result = {
                'ok': False, 'data': None, 'truncated': False,
                'error': {
                    'code': 'tool_execution_unknown' if unknown else 'tool_not_started',
                    'message': '前轮在结果落盘前中断；操作可能已经生效，须核对当前现场。' if unknown else '前轮中断前未记录该工具开始，宿主未重放。',
                },
            }
            messages.append({'role': 'tool', 'tool_call_id': identifier, 'content': json.dumps(result, ensure_ascii=False)})
        pending.clear()
    if interrupted and allow_interrupted:
        messages.append({'role': 'assistant', 'content': '【宿主接续说明】前轮执行中断，以上为已保存记录。未知结果不代表操作未发生；必须先核对当前文件与命令后置状态，不得仅因缺少结果而重复旧命令。此前测试不代表本轮通过。'})
    if pending or not messages or messages[0].get('role') != 'user':
        raise SessionError('history_unverified', '历史消息或工具结果不完整。')
    return messages
