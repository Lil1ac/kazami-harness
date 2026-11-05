"""本机资源协调：宿主排他、持久资源归属与父子共享限流。"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess

from agent_harness.storage.runs import RunStore, StorageError
from agent_harness.work.tasks import ExecutionLease, TaskError, WorkStore, encode


@dataclass
class HostLimits:
    """同一宿主所有主运行及后代共享的容量；信号量等待可被所属运行取消。"""

    models: asyncio.Semaphore  # 真实模型请求总槽位，包含摘要和子执行。
    processes: asyncio.Semaphore  # 真实命令总槽位，持有至进程清理结束。


host_limits: ContextVar[HostLimits | None] = ContextVar("harness_host_limits", default=None)  # CLI 未绑定时仅使用原有单运行预算。


@asynccontextmanager
async def capacity(kind: str):
    """为 kind（models/processes）申请宿主容量；未绑定宿主时直接放行，退出自动释放。"""
    limits = host_limits.get()
    if limits is None:
        yield
    else:
        async with getattr(limits, kind):
            yield


def canonical(path: Path) -> str:
    """返回 path 的规范绝对身份文本；Windows 合并大小写，解析链接但不创建目录。"""
    return os.path.normcase(str(path.resolve(strict=True)))


def resources(paths: list[Path], groups: dict[str, list[Path]]) -> dict:
    """按 paths 固定目录及仓库资源；groups 为宿主声明的共享目录组，不运行项目脚本。

    返回 paths（规范目录）、repositories（Git 公共目录）、groups（命中组名）及
    identities（路径到设备/对象编号的映射），用于排队后的身份核对；不可用时抛 TaskError/OSError。
    """
    directories, repositories, memberships = set(), set(), set()
    from agent_harness.execution.processes import _child_environment
    program = shutil.which("git")  # 普通目录不强制安装 Git；可识别的仓库必须能核对公共目录。
    for path in paths:
        identity = canonical(path)
        directories.add(identity)
        result = None
        if program is None:
            if any((parent / ".git").exists() or ((parent / "HEAD").is_file() and (parent / "objects").is_dir()) for parent in (path, *path.parents)):
                raise TaskError("resource_unavailable", "Git 项目需要可用的 Git，不能在无法核对仓库归属时执行。")
        else:
            try:
                # 桌面 stdin 承载私有协议，Git 不能继承；同时避免把模型凭据带入探测子进程。
                result = subprocess.run([program, "-C", identity, "rev-parse", "--path-format=absolute", "--git-common-dir"], stdin=subprocess.DEVNULL, env=_child_environment(), capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except (OSError, subprocess.SubprocessError):
                raise TaskError("resource_unavailable", "无法核对目录的 Git 资源身份，请检查本机 Git 环境。") from None
        if result is not None and result.returncode == 0:
            repositories.add(canonical(Path(os.fsdecode(result.stdout).strip())))
        for name, members in groups.items():
            if any(overlap(identity, canonical(member)) for member in members):
                memberships.add(name)
                directories.update(canonical(member) for member in members)
    identities = {name: [Path(name).stat().st_dev, Path(name).stat().st_ino] for name in directories | repositories}
    return {"paths": sorted(directories), "repositories": sorted(repositories), "groups": sorted(memberships), "identities": identities}


def overlap(left: str, right: str) -> bool:
    """判断规范路径 left/right 是否相同或为父子；不同磁盘返回 False，不按字符串前缀误判。"""
    a, b = Path(left), Path(right)
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def conflicts(left: dict, right: dict) -> bool:
    """判断两份资源快照是否冲突；相同仓库、声明共享资源或目录重叠均串行。"""
    return bool(set(left["repositories"]) & set(right["repositories"]) or set(left["groups"]) & set(right["groups"]) or any(overlap(a, b) for a in left["paths"] for b in right["paths"]))


class Coordinator:
    """一个宿主持有数据目录锁，并为独立项目分配持久租约；未知结果只阻塞相关资源。"""

    def __init__(self, store: WorkStore, maximum: int, models: int, processes: int):
        """绑定 store；maximum 为 1—8 个作业槽，models/processes 为 1—32 个请求/进程槽，构造不抢锁或写盘。"""
        self.store = store  # 与作业共用事务，资源认领和作业 preparing 同时提交。
        self.maximum = maximum  # 主运行与集成作业总并发上限。
        self.lease = ExecutionLease(store.root)  # 宿主级操作系统锁，CLI 或另一服务不能绕过。
        self.owned: set[str] = set()  # 本进程已认领的作业；持锁至全部收尾。
        self.limits = HostLimits(asyncio.Semaphore(models), asyncio.Semaphore(processes))  # 后代通过上下文继承。

    def records(self, db=None) -> list[dict]:
        """读取持久租约副本；db 可复用写事务，None 自行读取，未建库返回空列表。"""
        if db is not None:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM execution_claims")]
        if not self.store.path.exists():
            return []
        with self.store.transaction() as connection:
            return self.records(connection)

    def blocker(self, scope: dict, session: str | None, owner: str | None = None) -> dict | None:
        """返回 scope/session 的首个冲突租约；owner 可排除自身，None 不排除，不改变归属。"""
        return next((record for record in self.records() if record["id"] != owner and ((session is not None and record["session_id"] == session) or conflicts(scope, record["resources"]))), None)

    def reconcile(self, db) -> None:
        """已持有宿主锁时对账遗留租约；可信终态删除，其余标未知，不重放任何动作。"""
        for record in self.records(db):
            if record["id"] in self.owned:
                continue
            row = db.execute("SELECT body FROM jobs WHERE id=?", (record["id"],)).fetchone()
            job = json.loads(row[0]) if row else None
            complete = job is not None and job["status"] in {"completed", "cancelled", "failed", "waiting_input"}
            if not complete and record.get("run_id") and record["recover_run"] and (job is None or job.get("kind") != "integration"):
                try:
                    history = RunStore.read(self.store.root, record["run_id"])
                    complete = history.result is not None and history.result.cleanup_status in {"complete", "not_needed"}
                except StorageError:
                    pass
            if complete:
                db.execute("DELETE FROM execution_claims WHERE id=?", (record["id"],))
            else:
                if record["status"] != "unknown":
                    record["status"] = "unknown"
                    db.execute("UPDATE execution_claims SET body=? WHERE id=?", (encode(record), record["id"]))

    def acquire(self, owner: str, scope: dict, session: str | None, run_id: str | None, job: dict | None = None, *, recover_run: bool = True) -> None:
        """认领 owner 的 scope/session；run_id 用于恢复对账，job 非空时事务推进 preparing。

        session=None 表示首轮尚未建立会话，run_id=None 表示集成作业没有自身 Run；
        job=None 用于 CLI 或人工文件操作，不联动作业状态。成功返回 None。
        recover_run=False 用于人工文件操作，关联旧 Run 只作定位，旧终态不能证明该操作完成。
        冲突或容量满抛 TaskError，不启动工具；首次认领先取得宿主锁，失联租约不直接释放。
        """
        if owner in self.owned:
            return
        if len(self.owned) >= self.maximum:
            raise TaskError("run_capacity", "运行槽位已满，等待其他工作收尾。")
        if self.lease.handle is None:
            self.lease.acquire()
        try:
            with self.store.transaction() as db:
                self.reconcile(db)
                if any(record["id"] == owner for record in self.records(db)):
                    raise TaskError("resource_uncertain", "原执行归属尚未核对，不能用相同编号覆盖或重放。")
                blocked = next((record for record in self.records(db) if record["id"] != owner and ((session is not None and session == record["session_id"]) or conflicts(scope, record["resources"]))), None)
                if blocked is not None:
                    raise TaskError("resource_uncertain" if blocked["status"] == "unknown" else "resource_busy", "相关工作结果待核对。" if blocked["status"] == "unknown" else "相关项目或共享资源正在使用。")
                record = {"id": owner, "resources": scope, "session_id": session, "run_id": run_id, "status": "active", "recover_run": recover_run}
                db.execute("INSERT OR REPLACE INTO execution_claims VALUES (?,?)", (owner, encode(record)))
                if job is not None:
                    job.update(status="preparing", blocked_reason=None, blocked_by=None)
                    db.execute("UPDATE jobs SET body=? WHERE id=?", (encode(job), owner))
                    self.store.event(db, session or owner, {"type": "job_updated", "job": {key: value for key, value in job.items() if key not in {"payload", "digest", "memory_policy", "resources"}}})
            self.owned.add(owner)
        except BaseException:
            if not self.owned:
                self.lease.release()
            raise

    def release(self, owner: str, uncertain: bool = False) -> None:
        """释放本进程 owner；uncertain=True 保留未知资源记录，操作系统锁仅在全部执行收尾后释放。"""
        if owner not in self.owned:
            return
        try:
            with self.store.transaction() as db:
                if uncertain:
                    record = next(record for record in self.records(db) if record["id"] == owner)
                    record["status"] = "unknown"
                    db.execute("UPDATE execution_claims SET body=? WHERE id=?", (encode(record), owner))
                else:
                    db.execute("DELETE FROM execution_claims WHERE id=?", (owner,))
        finally:
            self.owned.discard(owner)
            if not self.owned:
                self.lease.release()

    def resolve(self, owner: str) -> None:
        """人工确认 owner 现场后解除未知租约；活动租约拒绝解除，不自动重放原作业。"""
        with self.store.transaction() as db:
            if any(record["id"] == owner and record["status"] != "unknown" for record in self.records(db)):
                raise TaskError("resource_busy", "资源仍由活动执行持有。")
            db.execute("DELETE FROM execution_claims WHERE id=?", (owner,))
