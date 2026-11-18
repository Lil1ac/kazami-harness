"""评测清单契约：同一实验只改变上下文策略，其余条件在启动前固定。"""

from typing import Literal
from pathlib import PurePosixPath, PureWindowsPath
from pydantic import Field, field_validator, model_validator
from agent_harness.schemas import DataModel
from agent_harness.evaluation.memory_cases import MEMORY_STRATEGIES, MemoryFixture


class Case(DataModel):
    """一项可独立重建的编码任务；路径均相对清单目录，评分代码不进入模型工作区。"""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$", description="稳定题号，不能复用于不同需求。")
    project: str = Field(min_length=1, description="来源项目名称；用于分层统计，不代表真实开源仓库。")
    category: str = Field(min_length=1, description="任务类别，例如修复、功能或长上下文。")
    split: Literal["dev", "test"] = Field(description="开发题可调优；测试题冻结后不得用于调优。")
    prompt: str = Field(min_length=1, description="完整需求；评分条件必须由需求推出。")
    backend: Literal["local", "swebench"] = Field(default="local", description="本机自建夹具或官方SWE-bench容器评分；同题所有试次一致。")
    source: str = Field(description="local为起点目录，swebench为固定官方任务JSON；禁止共享上次产物。")
    reference: str | None = Field(default=None, description="local必填参考解目录；swebench使用任务记录内参考补丁。")
    grader: str | None = Field(default=None, description="local必填可信Python脚本；swebench使用固定上游规格和解析器。")
    required_evidence: list[str] = Field(default_factory=list, description="记忆题依赖的学习会话编号；空列表表示不要求历史。")
    forbidden_evidence: list[str] = Field(default_factory=list, description="本题不可召回的撤回或外项目会话编号。")

    @field_validator("source", "reference", "grader")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        """校验相对路径value并返回原值；可选资源None原样返回，拒绝绝对、父级和Windows驱动路径。"""
        if value is None:
            return None
        if not value or "\\" in value or ".." in PurePosixPath(value).parts or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive:
            raise ValueError("评测资源必须为清单目录内的相对路径")
        return value

    @model_validator(mode="after")
    def backend_resources(self):
        """核对各backend资源互斥且齐备；返回自身，不允许隐式降级评分。"""
        if self.backend == "local" and (self.reference is None or self.grader is None):
            raise ValueError("本地题必须提供参考解及评分器")
        if self.backend == "swebench" and (self.reference is not None or self.grader is not None):
            raise ValueError("仓库题只能使用官方任务内的参考和测试补丁")
        return self


class Suite(DataModel):
    """可复现的题集及其来源声明，不把自建任务包装成公开基准。"""

    name: str = Field(min_length=1, description="题集展示名称。")
    version: str = Field(min_length=1, description="人工版本；实际冻结还会计算内容哈希。")
    provenance: str = Field(min_length=1, description="题集出处、构造方式及适用限制。")
    cases: list[Case] = Field(min_length=1, max_length=500, description="有唯一题号的任务。")
    kind: Literal["context", "memory"] = Field(default="context", description="上下文压缩或跨会话记忆实验。")
    learning: dict[str, MemoryFixture] = Field(default_factory=dict, description="仅记忆实验使用，键为项目:划分。")

    @model_validator(mode="after")
    def unique_ids(self):
        """拒绝重复题号，返回自身，防止覆盖试次及错误配对。"""
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("题号必须唯一")
        if self.kind == "context" and (self.learning or any(case.required_evidence or case.forbidden_evidence for case in self.cases)):
            raise ValueError("上下文压缩题集不携带学习历史")
        if self.kind == "memory":
            for case in self.cases:
                fixture = self.learning.get(case.project + ":" + case.split)
                if not fixture or case.backend != "local" or not case.project.replace("-", "").isalnum():
                    raise ValueError("记忆题必须是具有隔离学习材料的本地题")
                episodes = {item.id for item in fixture.episodes}
                if not set(case.required_evidence + case.forbidden_evidence) <= episodes or set(case.required_evidence) & set(case.forbidden_evidence):
                    raise ValueError("记忆题的所需及禁止证据必须唯一且来自该项目题集")
        return self


CONTEXT_POLICIES = ("off", "window", "masking", "hybrid", "hybrid_handoff", "hybrid_budgeted", "hybrid_budgeted_verified_handoff")  # 运行策略名称。


class EvaluationGroup(DataModel):
    """一个可冻结的执行器与上下文组合；组编号不再承担策略配置职责。"""

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="报告和配对使用的稳定组编号。")
    executor: Literal["agent_harness", "mini_swe_agent"] = Field(default="agent_harness", description="实际执行器；mini使用固定上游实现。")
    context_policy: str = Field(description="上下文或记忆策略名称；mini必须为native。")

    @model_validator(mode="after")
    def validate_policy(self):
        """检查执行器与策略组合并返回自身；非法组合抛校验错误。"""
        allowed = ("native",) if self.executor == "mini_swe_agent" else (*CONTEXT_POLICIES, *MEMORY_STRATEGIES)
        if self.context_policy not in allowed:
            raise ValueError("执行器不支持此上下文策略")
        return self


def preset_groups(names: list[str]) -> list[EvaluationGroup]:
    """将命名预设 names 转为显式组定义；未知名称由组模型拒绝，不改动调用方列表。"""
    presets = {"A": ("mini_swe_agent", "native"), "B": ("agent_harness", "window"),
               "C": ("agent_harness", "hybrid_budgeted")}
    return [EvaluationGroup(id=name, executor=presets.get(name, ("agent_harness", name))[0],
                            context_policy=presets.get(name, ("agent_harness", name))[1]) for name in names]


class Experiment(DataModel):
    """单次有界对照协议；执行器与上下文分别冻结，所有试次共享模型及资源上限。"""

    split: Literal["dev", "test"] = Field(default="dev", description="开发集或正式测试集。")
    kind: Literal["context", "memory"] = Field(default="context", description="上下文或记忆实验轴。")
    groups: list[EvaluationGroup] = Field(default_factory=lambda: preset_groups(["off", "hybrid"]), min_length=1, max_length=4, description="单组用于链路验收，多组按配对协议比较。")
    repeats: int = Field(default=1, ge=1, le=10, description="每题每组独立重复数。")
    repeat_offset: int = Field(default=0, ge=0, le=9, description="重复编号起点；稳定性追加使用1，正式首次使用0。")
    seed: int = Field(default=20261112, ge=0, description="试次顺序与重采样种子，不承诺模型确定性。")
    max_model_calls: int = Field(default=160, ge=1, le=20000, description="整批请求上限，含摘要及格式修复。")
    max_known_tokens: int = Field(default=2000000, ge=1, description="已结算token停止线，含失败成本。")
    max_cost_cents: int | None = Field(default=None, ge=1, description="整批美元分；None用于无计费夹具。")
    max_consolidation_calls: int = Field(default=4, ge=1, le=4, description="记忆单项目整理请求上限。")
    grader_timeout_seconds: float = Field(default=20.0, gt=0, le=600, description="独立评分超时秒数，不占Agent执行时钟。")

    @property
    def group_ids(self) -> list[str]:
        """返回声明顺序的组编号；由显式 groups 派生，不保存第二份策略配置。"""
        return [group.id for group in self.groups]

    @model_validator(mode="after")
    def unique_groups(self):
        """核对组身份和实验轴；返回自身，拒绝重复组及记忆协议误配。"""
        if len(set(self.group_ids)) != len(self.groups):
            raise ValueError("组编号必须唯一")
        policies = [group.context_policy for group in self.groups]
        if self.kind == "memory" and (tuple(policies) != MEMORY_STRATEGIES or self.repeats != 1 or self.max_model_calls > 300):
            raise ValueError("记忆试点固定四组、每题一次、至多300次调用")
        if self.kind == "memory" and self.max_cost_cents is not None:
            raise ValueError("记忆试点未启用美元限额")
        if self.kind == "context" and any(policy in MEMORY_STRATEGIES for policy in policies):
            raise ValueError("上下文评测不使用记忆策略")
        if "hybrid_budgeted_verified_handoff" in policies and set(policies) != {"hybrid_budgeted", "hybrid_budgeted_verified_handoff"}:
            raise ValueError("经自测的预算收尾须对照同样使用预算投影的末轮策略")
        return self


class QualityReview(DataModel):
    """独立任务质量复核；绑定原协议指纹，只补充结论，不重写评分或历史。"""

    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="被复核协议指纹，防止复核附到其他实验。")
    verdict: Literal["invalid_comparison"] = Field(description="当前只支持判定对照无效；不以缺省值推断已通过人工审查。")
    case_ids: list[str] = Field(min_length=1, description="导致对照不可采信的题号；原始试次与失败仍保留。")
    reason: str = Field(min_length=1, max_length=4000, description="需求与评分器不一致的可核对理由。")
