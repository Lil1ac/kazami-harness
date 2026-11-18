"""从可信终态的增量事件建立项目证据索引；原始事件始终是证据本体。"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from agent_harness.storage.runs import RunHistory, RunStore, StorageError


# 只索引原始观察，排除记忆、计划、委派回执和宿主已经派生的内容。
_OBSERVATIONS = frozenset({"read_file", "search_files", "list_files", "write_file", "edit_file", "run_command"})
_UNREADABLE = frozenset({"trashed", "purging", "purged"})  # 归档仅停止写入，仍允许回读已保留证据。
_SECRET = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\r\n]*PRIVATE KEY-----|\Z)|\b(?:sk-|sk_|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}|\bBearer\s+[^\s\"']{8,}|(?:api[_-]?key|password|passwd|secret|token|密钥|密码)\s*[:=]\s*[\"']?[^\s\"']{8,}", re.I)


def redact(value: str) -> str:
    """遮盖 value 中已知凭据格式及宿主环境中的凭据值，返回可纳入索引的文本。"""
    for name, secret in os.environ.items():
        if len(secret) >= 8 and re.search(r"(?:API_?KEY|ACCESS_?TOKEN|PASSWORD|SECRET|CREDENTIAL)", name, re.I):
            value = value.replace(secret, "[凭据已隐藏]")
    return _SECRET.sub("[凭据已隐藏]", value)


def _words(text: str) -> list[str]:
    """将 text 转为去重检索词；英文路径拆分，中文以单字或相邻双字检索。"""
    words = []
    for word in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if len(word) > 1 and re.fullmatch(r"[\u3400-\u9fff]+", word):
            words.extend(word[i:i + 2] for i in range(len(word) - 1))
            words.extend(word)  # 支持用户明确搜索一个中文字符。
        else:
            words.append(word)
    return list(dict.fromkeys(words))


def evidence_metadata(item: dict) -> dict:
    """返回 item 的定位字段，去掉摘录或预览正文；用于修订、引用和审计。"""
    return {key: item[key] for key in ("id", "run_id", "session_id", "seq", "kind", "sha256", "captured_at")}


def _suppressed(item: dict, references: set[str] | None) -> bool:
    """判断 item 是否与撤回的编号或原事件位置对应；references 为 None 时不筛选。"""
    return bool(references and (item["id"] in references or f"{item['run_id']}:{item['seq']}" in references
                                or (item["kind"] == "user" and f"{item['run_id']}:user" in references)))


def hidden_sessions(root: Path, db=None) -> set[str]:
    """读取 root 的回收站及清理状态；db 为已持有会话事务，None 单独只读且不建库。"""
    from agent_harness.memory.service import MemoryError

    path = root / ".work" / "work.sqlite3"
    if db is None and not path.exists():
        return set()
    if db is None and (path.resolve() != path or path.parent.is_junction()):
        raise MemoryError("memory_storage_error", "会话状态路径发生变化。")

    def read(connection) -> set[str]:
        """从 connection 返回不可回读的会话编号；缺少管理表时返回空集合。"""
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversations'").fetchone():
            return set()
        return {row[0] for row in connection.execute("SELECT id FROM conversations WHERE json_extract(body,'$.state') IN (?,?,?)", tuple(sorted(_UNREADABLE)))}

    try:
        if db is not None:
            return read(db)
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            return read(connection)
    except sqlite3.Error:
        raise MemoryError("memory_storage_error", "会话可见性无法核对。") from None


def project_events(history: RunHistory) -> list[dict]:
    """投影 history 中当前轮的公开增量证据；缺终态、身份或记录授权时返回空列表。"""
    from agent_harness.memory.service import digest

    manifest = history.manifest
    if not manifest.memory_scope_id or not manifest.config.memory.capture or manifest.delegation or not history.result or history.warnings:
        return []
    run = str(manifest.run_id)
    session = str(manifest.session_id or manifest.run_id)
    values = []
    for event in history.events:
        kind, value = None, None
        if event.type == "run_started":
            kind, value = "user", manifest.task.prompt  # 首条是本轮需求，不重录继承的旧轮消息。
        elif event.type == "assistant_message" and not event.payload.get("source"):
            kind, value = "assistant", event.payload.get("message", {}).get("content")
        elif event.type == "tool_result" and event.payload.get("name") in _OBSERVATIONS:
            kind, value = "observation", json.dumps(event.payload, ensure_ascii=False)
        elif event.type == "verification_result":
            kind, value = "verification", json.dumps(event.payload.get("verification", {}), ensure_ascii=False)
        if not isinstance(value, str) or not value.strip():
            continue
        excerpt = redact(value)[:2400]  # 单证据只保存有界可定位摘录，不持久复制整份日志。
        fingerprint = digest({"seq": event.seq, "kind": kind, "text": excerpt})
        values.append({"id": digest([run, event.seq, fingerprint]), "run_id": run, "session_id": session,
                       "seq": event.seq, "kind": kind, "text": excerpt, "sha256": fingerprint,
                       "captured_at": manifest.created_at.isoformat()})
    # 总量有界，保留首条用户需求与最后的交付/验收观察。
    return values if len(values) <= 64 else [*values[:32], *values[-32:]]


class EvidenceIndex:
    """项目隔离的本地 SQLite 全文索引；查询定位原始运行，清理时删除派生正文。"""

    def __init__(self, root: Path):
        """绑定受保护 root；构造不扫描会话、不创建数据库。"""
        self.root = root.resolve()  # 与 RunStore 使用同一个宿主数据根。
        self.path = self.root / ".memory" / "evidence.sqlite3"  # 可由可信终态重新构建的索引。

    @contextmanager
    def _connect(self, write: bool = False):
        """提供索引连接；write=False 且库未建时 yield None，写入失败回滚并报受控错误。"""
        from agent_harness.memory.service import MemoryError

        db = None
        try:
            if self.path.resolve() != self.path or self.path.parent.is_junction():
                raise OSError
            if not write and not self.path.exists():
                yield None
                return
            if write:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rwc" if write else "?mode=ro"), uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            if write:
                db.executescript("""
                    PRAGMA secure_delete=ON;
                    CREATE TABLE IF NOT EXISTS indexed_runs(seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
                        scope TEXT NOT NULL, session TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS indexed_scope ON indexed_runs(scope, seq);
                    CREATE TABLE IF NOT EXISTS excerpts(ref TEXT UNIQUE NOT NULL, run_seq INTEGER NOT NULL, body TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS indexed_run ON excerpts(run_seq);
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(words);
                    INSERT INTO search_text(search_text,rank) VALUES('secure-delete',1);
                """)
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except (sqlite3.Error, OSError, ValueError):
            raise MemoryError("memory_storage_error", "历史证据索引无法读取或保存。") from None
        finally:
            if db is not None:
                db.close()

    def record(self, history: RunHistory) -> None:
        """幂等索引 history 的可信终态；禁用收集或不可见会话无写入，返回 None。"""
        values = project_events(history)
        if not values:
            return
        first = values[0]
        if first["session_id"] in hidden_sessions(self.root):
            return
        with self._connect(True) as db:
            if first["session_id"] in hidden_sessions(self.root):
                return  # 事务开始后再次核对，避免清理与索引交错时复活已删记录。
            cursor = db.execute("INSERT OR IGNORE INTO indexed_runs(run_id,scope,session,created_at) VALUES(?,?,?,?)",
                                (first["run_id"], history.manifest.memory_scope_id, first["session_id"], first["captured_at"]))
            if cursor.rowcount == 0:
                return
            for value in values:
                row = db.execute("INSERT INTO excerpts VALUES(?,?,?)", (value["id"], cursor.lastrowid, json.dumps(value, ensure_ascii=False)))
                db.execute("INSERT INTO search_text(rowid,words) VALUES(?,?)", (row.lastrowid, " ".join(_words(value["text"]))))

    def refresh(self, scope: str) -> None:
        """补录 scope 最近最多 256 个可信、未索引的 Run；忽略不可用记录，不更改原始历史。"""
        with self._connect() as db:
            indexed = {row[0] for row in db.execute("SELECT run_id FROM indexed_runs")} if db is not None else set()
        if not self.root.exists():
            return
        candidates = []
        for path in self.root.iterdir():
            if path.name in indexed or not re.fullmatch(r"[a-f0-9-]{36}", path.name) or path.is_symlink() or path.is_junction():
                continue
            try:
                manifest = path / "run.json"
                if manifest.resolve() != manifest or manifest.stat().st_size > 4 * 1024 * 1024:
                    continue
                value = json.loads(manifest.read_text(encoding="utf-8"))
                if value.get("memory_scope_id") == scope:
                    candidates.append((value.get("created_at", ""), path.name))
            except (OSError, ValueError):
                continue
        for _, run in sorted(candidates, reverse=True)[:256]:
            try:
                self.record(RunStore.read(self.root, run))
            except StorageError:
                continue

    def cutoff(self, scope: str) -> int:
        """返回 scope 当前索引序号，空库为 0；运行可用它固定历史读取范围。"""
        with self._connect() as db:
            return db.execute("SELECT COALESCE(MAX(seq),0) FROM indexed_runs WHERE scope=?", (scope,)).fetchone()[0] if db is not None else 0

    def _original(self, scope: str, run: str) -> dict[str, dict]:
        """从 run 的可信原始事件重投影 scope 证据；目录替换、损坏或越界时返回空映射。"""
        try:
            identifier = UUID(run)
            directory = self.root / str(identifier)
            if directory.resolve() != directory or directory.is_junction():
                return {}
            history = RunStore.read(self.root, str(identifier))
            if history.manifest.memory_scope_id == scope and history.manifest.run_id == identifier:
                return {item["id"]: item for item in project_events(history)}
        except (StorageError, ValueError, OSError):
            pass
        return {}

    def runs(self, scope: str, cutoff: int) -> list[dict]:
        """返回 scope 不超过 cutoff 的最近可见记录，最多 60 条；供用户选择整理输入。"""
        hidden = hidden_sessions(self.root)
        with self._connect() as db:
            if db is None:
                return []
            rows = db.execute("""SELECT r.run_id,r.session AS session_id,r.created_at,count(e.ref) AS evidence_count,
                COALESCE(MAX(CASE WHEN json_extract(e.body,'$.kind')='user' THEN substr(json_extract(e.body,'$.text'),1,100) END),r.run_id) AS title
                FROM indexed_runs r JOIN excerpts e ON e.run_seq=r.seq
                WHERE r.scope=? AND r.seq<=? GROUP BY r.seq ORDER BY r.created_at DESC,r.seq DESC""", (scope, cutoff))
            items = []
            for row in rows:
                if row["session_id"] not in hidden:
                    items.append(dict(row))
                if len(items) == 60:
                    break
            return items

    def search(self, scope: str, cutoff: int, query: str, *, limit: int = 20, offset: int = 0,
               runs: set[str] | None = None, suppressed: set[str] | None = None) -> dict:
        """检索 scope 的冻结摘录；runs=None 为全部，suppressed 为撤回证据编号，返回有界分页。"""
        hidden = hidden_sessions(self.root)
        terms = _words(query)[:32]
        # 优先匹配中文双字，避免单字的宽泛命中压过真正相关片段。
        if any(len(term) > 1 and re.fullmatch(r"[\u3400-\u9fff]+", term) for term in terms):
            terms = [term for term in terms if not re.fullmatch(r"[\u3400-\u9fff]", term)]
        from_clause = "excerpts e JOIN indexed_runs r ON e.run_seq=r.seq"
        filters = "r.scope=? AND r.seq<=?"
        params = [scope, cutoff]
        if terms:
            from_clause += " JOIN search_text s ON s.rowid=e.rowid"
            filters += " AND search_text MATCH ?"
            params.append(" OR ".join('"' + term + '"' for term in terms))
        if runs is not None:
            if not runs:
                return {"items": [], "next_offset": None}
            filters += " AND r.run_id IN (" + ",".join("?" for _ in runs) + ")"
            params.extend(sorted(runs))
        order = "bm25(search_text)," if terms else ""
        values = []
        with self._connect() as db:
            if db is not None:
                rows = db.execute(f"SELECT e.body,e.ref,r.session FROM {from_clause} WHERE {filters} ORDER BY {order}r.created_at DESC,r.seq DESC,e.rowid ASC LIMIT 2500", params)
                originals = {}  # 同一查询每个原始 Run 只重投影一次，不能返回损坏来源的缓存摘录。
                count = 0
                for row in rows:
                    if row["session"] in hidden:
                        continue
                    value = json.loads(row["body"])
                    if _suppressed(value, suppressed):
                        continue
                    run = value["run_id"]
                    if run not in originals:
                        originals[run] = self._original(scope, run)
                    if originals[run].get(value["id"]) != value:
                        continue
                    if count >= offset:
                        values.append(value)
                    count += 1
                    if len(values) > limit:
                        break
        return {"items": values[:limit], "next_offset": offset + limit if len(values) > limit else None}

    def get(self, scope: str, cutoff: int, reference: str, *, suppressed: set[str] | None = None, db=None) -> dict:
        """回读 reference 并核对原始事件指纹；db 可传会话事务，越界、隐藏或变动抛 MemoryError。"""
        from agent_harness.memory.service import MemoryError

        if reference in (suppressed or ()):
            raise MemoryError("memory_not_found", "此证据已撤回或不可用。")
        hidden = hidden_sessions(self.root, db)
        with self._connect() as connection:
            row = connection.execute("SELECT e.body,r.session FROM excerpts e JOIN indexed_runs r ON e.run_seq=r.seq WHERE r.scope=? AND r.seq<=? AND e.ref=?", (scope, cutoff, reference)).fetchone() if connection is not None else None
        if row is None or row["session"] in hidden:
            raise MemoryError("memory_not_found", "当前项目没有这条可用历史证据。")
        value = json.loads(row["body"])
        if _suppressed(value, suppressed):
            raise MemoryError("memory_not_found", "此证据已撤回或不可用。")
        if self._original(scope, value["run_id"]).get(reference) == value:
            return value
        raise MemoryError("memory_source_unavailable", "历史来源已变化或无法核对。")

    def purge(self, session: str) -> None:
        """移除 session 的所有索引正文和倒排词；未建库时不创建，返回 None。"""
        if not self.path.exists():
            return
        with self._connect(True) as db:
            db.execute("DELETE FROM search_text WHERE rowid IN (SELECT e.rowid FROM excerpts e JOIN indexed_runs r ON e.run_seq=r.seq WHERE r.session=?)", (session,))
            db.execute("DELETE FROM excerpts WHERE run_seq IN (SELECT seq FROM indexed_runs WHERE session=?)", (session,))
            db.execute("DELETE FROM indexed_runs WHERE session=?", (session,))
