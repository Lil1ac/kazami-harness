"""记忆实验的学习材料、四组隔离回放及原始证据追踪。"""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import time
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from agent_harness.config import prepare_task
from agent_harness.memory.consolidation import consolidate, publish
from agent_harness.memory.evidence import EvidenceIndex, evidence_metadata, project_events
from agent_harness.memory.service import MemoryChange, MemoryError, MemoryService, MemoryStore, encode
from agent_harness.memory.types import MemoryContent, conflicts
from agent_harness.providers.base import ModelResponse
from agent_harness.runtime.runner import run_task
from agent_harness.schemas import DataModel, RunConfig, RunManifest
from agent_harness.storage.runs import RunHistory, RunStore
from agent_harness.tools.registry import ToolExecutor


MEMORY_STRATEGIES = ("memory_current", "memory_history", "memory_full", "memory_off")  # 第一组是当前已确认记忆基线。


class Episode(DataModel):
    """一条预先冻结的学习会话，内容只在所属项目范围内可回读。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")  # 用于对齐不同试次的同一来源。
    prompt: str = Field(min_length=1, max_length=1600)  # 原始学习证据，与最终测试题分开。
    captured_at: str  # 带时区的虚构发生时间，用于检索时间和更新题。
    foreign: bool = False  # True 放在独立项目，用于检验相似来源不越界。


class SeedNote(DataModel):
    """项目已有的人工确认知识，来源必须指向真实回放的一条学习会话。"""

    episode: str  # 关联 Episode 的稳定编号。
    content: MemoryContent  # 已确认的结构正文。
    withdrawn: bool = False  # True 表示测试前撤回，仍保留抑制记录。


class ReviewRule(DataModel):
    """只从学习材料制定的发布规则，测试任务和参考答案不参与审阅。"""

    id: str  # 稳定规则编号，写入报告以便复核。
    episodes: list[str] = Field(min_length=1, max_length=8)  # 草案必须引用的学习来源。
    terms: list[list[str]] = Field(min_length=1)  # 每组至少命中一个同义词或标识符。
    forbidden: list[str] = Field(default_factory=list)  # 主张正文出现其中任何词就拒绝。


class MemoryFixture(DataModel):
    """一个项目在某一题集划分中的固定历史、已有知识和证据审阅规则。"""

    episodes: list[Episode] = Field(min_length=1, max_length=12)  # 按已知时间注入可信来源。
    notes: list[SeedNote] = Field(default_factory=list, max_length=20)  # 各组共有的初始知识。
    review: list[ReviewRule] = Field(default_factory=list, max_length=24)  # 只接受支持且可复用的候选。

    @model_validator(mode="after")
    def validate_sources(self):
        """核对 episode 唯一、时间及知识/审阅引用，返回自身；错误属于无效评测题集。"""
        ids = {item.id for item in self.episodes}
        own = {item.id for item in self.episodes if not item.foreign}
        if len(ids) != len(self.episodes) or not own:
            raise ValueError("学习会话编号重复或缺少项目内来源")
        if any(datetime.fromisoformat(item.captured_at).tzinfo is None for item in self.episodes):
            raise ValueError("学习证据时间必须带时区")
        if any(note.episode not in own for note in self.notes) or any(not set(rule.episodes) <= own for rule in self.review):
            raise ValueError("学习笔记和审阅规则只能引用本项目证据")
        return self


class _ReplayModel:
    """以固定确认回复重放学习会话；不消耗真实模型额度。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """对已固定的 messages 返回确定性确认；tools 不使用，无外部调用。"""
        return ModelResponse({"role": "assistant", "content": "已记录本次提供的项目事实。"}, [], "stop",
                             "offline-memory-replay", usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})


class MemoryTrialExecutor(ToolExecutor):
    """记忆小题只开放固定文件、记忆和澄清工具，避免其他工作流成为组间变量。"""

    def _enabled_names(self) -> list[str]:
        """返回实际允许的试次工具名；四组仅按配置改变记忆读取能力。"""
        selected = {*self.config.tools, "memory_search", "memory_get", "ask_user_question"}
        return [name for name in super()._enabled_names() if name in selected]


def _policy(config: RunConfig, strategy: str) -> RunConfig:
    """按 strategy 投影 config 的记忆读能力；测试答题阶段统一关闭学习。"""
    trial = config.model_copy(deep=True)
    trial.memory.use = strategy != "memory_off"
    trial.memory.history = strategy in {"memory_history", "memory_full"}
    trial.memory.topics = strategy == "memory_full"
    trial.memory.capture = False
    return trial


class MemoryTrialEnvironment:
    """每个项目先做一次真实 Runner 学习回放，之后给试次复制固定证据与已确认修订。"""

    def __init__(self, fixture: MemoryFixture, project: str, root: Path, work: Path, config: RunConfig):
        """绑定 fixture/project 和隔离目录 root/work；构造不写盘，不调用模型。"""
        self.fixture = fixture  # 与测试任务独立冻结的学习材料。
        self.project = project  # 可在报告里定位的试点项目。
        self.root = root  # 所有真实及回放 Run 的宿主数据目录。
        self.work = work  # 本项目学习回放目录。
        self.config = config  # 已冻结前台配置；学习仅使用离线回复。
        self.history: dict[str, RunHistory] = {}  # 原始学习会话回执，按 fixture episode 索引。
        self.refs: dict[str, dict] = {}  # episode 到学习运行的用户证据定位。
        self.baseline: list[dict] = []  # 整理前的不可变最新修订集合。
        self.enriched: list[dict] = []  # 证据审阅后的修订集合。

    async def seed(self) -> MemoryService:
        """用真实 Runner 回放项目与干扰会话，保存已有记忆和撤回状态，返回学习仓储。"""
        self.work.mkdir(parents=True, exist_ok=False)
        foreign = self.work.with_name(self.work.name + "-foreign")
        foreign.mkdir()
        replay = _policy(self.config, "memory_off")
        replay.memory.capture = True
        replay.limits.max_model_turns = 1
        service = MemoryService(MemoryStore(self.root), self.work)
        for episode in self.fixture.episodes:
            workspace = foreign if episode.foreign else self.work
            task = prepare_task(episode.prompt, workspace)
            manifest = RunManifest(run_id=uuid4(), task=task, config=replay, created_at=datetime.fromisoformat(episode.captured_at))
            outcome = await run_task(manifest, self.root, "offline-memory-replay", model_client=_ReplayModel())
            if outcome.result.execution_status != "completed":
                raise ValueError("学习材料回放未完成：" + episode.id)
            history = RunStore.read(self.root, str(manifest.run_id))
            self.history[episode.id] = history
            self.refs[episode.id] = evidence_metadata(next(item for item in project_events(history) if item["kind"] == "user"))
        for note in self.fixture.notes:
            item = service.create(note.content, str(uuid4()), provenance={"kind": "evaluation_fixture", "refs": [self.refs[note.episode]]})
            if note.withdrawn:
                service.update(item["id"], MemoryChange(request_id=uuid4(), action="delete", expected_revision=item["revision"]))
        self.baseline = service.store.read(service.source)["items"]
        self.enriched = deepcopy(self.baseline)
        return service

    def eligible_runs(self) -> list[str]:
        """返回可用于整理的本项目源 Run；已撤回和独立项目不送给整理模型。"""
        withdrawn = {note.episode for note in self.fixture.notes if note.withdrawn}
        return [self.refs[item.id]["run_id"] for item in self.fixture.episodes if not item.foreign and item.id not in withdrawn]

    async def organize(self, service: MemoryService, api_key: str, *, max_calls: int, client=None, progress=None) -> dict:
        """用最多 max_calls 次请求整理共享资料；返回草案、实际计量和固定证据审阅决定。"""
        identifier = str(uuid4())
        config = self.config.model_copy(deep=True)
        config.limits.max_model_turns = min(config.limits.max_model_turns, max_calls)
        config.limits.model_max_output_tokens = max(config.limits.model_max_output_tokens, 8192)
        result = await consolidate(service, self.eligible_runs(), config, api_key, client=client, progress=progress)
        review_started = time.monotonic()
        decisions = []
        selected = []
        if result["state"] == "draft":
            aliases = {reference["id"]: name for name, reference in self.refs.items()}
            active = [item for item in self.baseline if item["status"] == "active"]
            for index, change in enumerate(result["changes"]):
                used = {aliases[value] for value in change["evidence_ids"] if value in aliases}
                content = change.get("content") or {}
                details = encode(content.get("details", {})).casefold()
                match = next((rule for rule in self.fixture.review if set(rule.episodes) <= used
                              and all(any(term.casefold() in details for term in group) for group in rule.terms)
                              and not any(term.casefold() in details for term in rule.forbidden)), None)
                targets = {target["id"] for target in change["targets"]}
                remaining = [item for item in active if item["id"] not in targets]
                accepted = (match is not None and change["action"] in {"add", "replace"}
                            and not content.get("evidence") and not conflicts(content, remaining))
                if accepted:
                    selected.append(index)
                    active = [*remaining, {**content, "id": "draft:" + str(index), "revision": 1, "status": "active"}]
                decisions.append({"index": index, "accepted": accepted, "rule": match.id if match else None,
                                  "reason": None if accepted else "scope_or_conflict" if match else "evidence_rule_unmatched"})
            try:
                result["receipt"] = publish(service, identifier, result, selected, str(uuid4()))
                self.enriched = service.store.read(service.source)["items"]
            except MemoryError as error:
                result.update(review_error=error.code, review_message=error.message)
        result.update(id=identifier, project=self.project, review=decisions,
                      review_seconds=time.monotonic() - review_started, review_method="frozen_source_rules",
                      replay_runs=len(self.history), before_sequence=len(self.baseline))
        return result

    def bind(self, workspace: Path, strategy: str) -> tuple[RunConfig, dict[str, str]]:
        """把固定历史重绑 workspace 身份，返回试次配置及 episode→证据编号；无外部模型调用。

        每个试次生成自己的真实存储记录，其他试次的记忆、运行与文件在项目作用域外。
        只复制回放时的用户事实与公开终态，派生版本重新关联新证据编号。
        """
        if strategy == "memory_off":
            return _policy(self.config, strategy), {}
        store = MemoryStore(self.root)
        scope = store.scope(None, workspace)
        index = EvidenceIndex(self.root)
        mapped = {}
        aliases = {}
        for episode in self.fixture.episodes:
            old = self.history[episode.id]
            if episode.foreign:
                # 干扰会话保持在独立项目，供越界断言检查其已存在而不可见。
                aliases[episode.id] = self.refs[episode.id]["id"]
                continue
            manifest = old.manifest.model_copy(update={"run_id": uuid4(), "memory_scope_id": scope,
                "task": old.manifest.task.model_copy(update={"workspace": workspace, "python_execution": None})})
            writer = RunStore.create(self.root, manifest)
            writer.append("run_started", {"messages": [{"role": "system", "content": "固定学习回放。"},
                {"role": "user", "content": manifest.task.prompt}], "tools": [], "conversation_start": 1})
            for event in old.events:
                if event.type in {"assistant_message", "tool_result", "verification_result"}:
                    writer.append(event.type, event.payload)
            writer.finalize(old.result.model_copy(update={"run_id": manifest.run_id, "artifacts": []}))
            replayed = RunStore.read(self.root, str(manifest.run_id))
            index.record(replayed)
            for before, after in zip(project_events(old), project_events(replayed), strict=True):
                if (before["kind"], before["text"]) != (after["kind"], after["text"]):
                    raise ValueError("试次回放证据与固定学习历史不一致")
                mapped[before["id"]] = evidence_metadata(after)
            aliases[episode.id] = mapped[self.refs[episode.id]["id"]]["id"]
        items = self.enriched if strategy == "memory_full" else self.baseline
        with store.transaction(write=True) as db:
            store.scope(db, workspace, create=True)
            for frozen in items:
                item = deepcopy(frozen)
                source = item["source"]
                source["refs"] = [mapped[ref["id"]] for ref in source.get("refs", [])]
                store.append(db, scope, item)
        return _policy(self.config, strategy), aliases


def memory_trace(history: RunHistory, aliases: dict[str, str], required: list[str], forbidden: list[str]) -> dict:
    """从 history 提取真实检索和输入证据，按 aliases 对齐预标注的所需及禁用来源。"""
    snapshots = [event.payload["memory"] for event in history.events if event.type in {"memory_selected", "memory_read"} and "memory" in event.payload]
    memory = snapshots[-1] if snapshots else {}
    searches = memory.get("history_searches", [])
    retrieved = {item["id"] for search in searches for item in search["items"]}
    references = {item["id"]: item for search in searches for item in search["items"]}
    exposed = {identifier for use in memory.get("uses", []) for identifier in use.get("history_evidence_ids", [])}
    for use in memory.get("uses", []):
        for item in use["selected"]:
            for ref in item["source"].get("refs", []):
                exposed.add(ref["id"])
                references[ref["id"]] = ref
    for page in memory.get("reads", []):
        if page.get("evidence"):
            references[page["evidence"]["id"]] = page["evidence"]
            continue
        if not page.get("eof") or page.get("offset") != 0:
            continue
        try:
            read = json.loads(page["content"])
        except ValueError:
            continue
        # 完整条目回读确实进入下一请求时，来源与分页记录一起归入采用候选。
        if any(page["sha256"] in use.get("exposed_memory_sha256", []) for use in memory.get("uses", [])):
            for ref in read.get("source", {}).get("refs", []):
                exposed.add(ref["id"])
                references[ref["id"]] = ref
    return {"required": required, "retrieved": [name for name in required if aliases.get(name) in retrieved],
            "exposed": [name for name in required if aliases.get(name) in exposed],
            "forbidden_hits": [name for name in forbidden if aliases.get(name) in retrieved | exposed],
            "searches": len(searches), "topic_requests": sum(bool(use.get("topics")) for use in memory.get("uses", [])),
            "evidence": [references[identifier] for identifier in sorted(exposed) if identifier in references],
            "all_required_exposed": all(aliases.get(name) in exposed for name in required) if required else None}


def billable_usage(history: RunHistory) -> list[dict]:
    """返回 history 中每个模型回执的用量及时间偏移；未返回的用量保留空值。"""
    return [{"elapsed_ms": event.elapsed_ms, "usage": event.payload.get("usage"),
             "model": event.payload.get("response_model")} for event in history.events if event.type == "model_response"]
