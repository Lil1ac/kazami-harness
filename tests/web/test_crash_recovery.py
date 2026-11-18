"""通过真实 Python 进程、HTTP 和 Git 验证中断恢复，不依赖正常取消的 finally。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest

from tests.helpers.serve_crash_recovery_fixture import AFTER, BEFORE, FIXTURE_PROMPT


def eventually(get, timeout: float = 12):
    """轮询 get 直至返回真值。HTTP 启动期连接错误可重试，超时报告最后观测。"""
    limit = time.monotonic() + timeout
    value = None
    while time.monotonic() < limit:
        try:
            value = get()
            if value:
                return value
        except httpx.TransportError as error:
            value = error
        time.sleep(.04)
    raise AssertionError(f"未达到预期状态：{value!r}")


def free_port() -> int:
    """让内核为夹具分配回环端口，返回测试进程专用端口号。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def backend(root: Path, port: int):
    """在 root 启动独立 Web 后台，返回启动句柄、实际解释器 PID 和 HTTP 客户端。"""
    marker = root / "backend.pid"
    marker.unlink(missing_ok=True)
    with (root / "backend.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "tests.helpers.serve_crash_recovery_fixture",
             "--data-dir", str(root), "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
            try:
                eventually(lambda: client.get("/api/jobs").is_success)
                yield process, int(marker.read_text(encoding="ascii")), client
            finally:
                if process.poll() is None:
                    if marker.exists():
                        terminate_backend(process, int(marker.read_text(encoding="ascii")))
                    else:
                        process.kill()
                    process.wait(timeout=10)


def terminate_backend(process: subprocess.Popen, pid: int) -> None:
    """只终止本夹具新登记的解释器。Windows 的 venv 启动器可能是单独的父进程。"""
    if process.pid == pid:
        process.kill()
    else:
        os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


def entries(path: Path) -> list[dict]:
    """读取已提交的 JSONL 文件 path。无记录时返回空列表。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def first_job(client: httpx.Client) -> dict:
    """读取已持久接纳的原作业，预期只有一次首次提交。"""
    response = client.get("/api/jobs").raise_for_status().json()
    return response["items"][0] if response["items"] else {}


def launch(client: httpx.Client) -> httpx.Response:
    """向真实 API 提交手动 Git 隔离任务，返回 HTTP 响应。"""
    return client.post("/api/jobs", json={"request_id": str(uuid4()), "create": {
        "project_id": "crash", "execution_mode": "isolated", "prompt": FIXTURE_PROMPT}})


def interrupt(root: Path, port: int, phase: str) -> dict:
    """在 phase 处由父测试进程强制结束 Python，返回原作业及断点证据。"""
    (root / "arm.txt").write_text(phase, encoding="utf-8")
    with backend(root, port) as (process, pid, client), ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(launch, client)
        marker = root / ("verifier-started.txt" if phase == "verification" else "fault.json")
        eventually(marker.exists)
        if phase != "verification":
            assert json.loads(marker.read_text(encoding="utf-8"))["pid"] == pid
        terminate_backend(process, pid)
        assert process.wait(timeout=10) != 0
        try:
            response.result()
        except httpx.TransportError:
            pass  # 硬退出可发生在提交 HTTP 回包之前。
    if phase != "verification":
        (root / "arm.txt").write_text("", encoding="utf-8")
    return {"pid": pid, "phase": phase}


@pytest.mark.parametrize("phase", ["intent", "applied", "created", "receipt", "model"])
def test_real_crash_continues_once(tmp_path: Path, phase: str):
    """实杀模型或写入窗口。旧 Run 不变，接续后只提交一次文件并通过真实验收。"""
    port = free_port()
    if phase == "created":
        (tmp_path / "new-file-mode").touch()
    crash = interrupt(tmp_path, port, phase)
    with backend(tmp_path, port) as (_, pid, client):
        source = first_job(client)
        recovery = source["recovery"]
        assert pid != crash["pid"]
        assert recovery["state"] == "ready", source
        assert recovery["recovered_writes"] == (phase in {"applied", "created"})
        assert recovery["usage_complete"] == (phase != "model")
        old_log = tmp_path / "runs" / source["run_id"] / "events.jsonl"
        frozen = old_log.read_bytes()
        assert client.get(f"/api/runs/{source['run_id']}/conversation").raise_for_status().json()["can_continue"] is False
        assert len(entries(tmp_path / "model.jsonl")) == 1
        request = {"request_id": str(uuid4()), "expected_revision": recovery["revision"]}
        url = f"/api/jobs/{source['id']}/recover"
        child = client.post(url, json=request).raise_for_status().json()
        assert client.post(url, json=request).raise_for_status().json()["run_id"] == child["run_id"]
        assert client.post(url, json={**request, "request_id": str(uuid4())}).status_code == 409
        assert client.post(f"/api/jobs/{source['id']}/resolve").status_code == 409
        assert len(entries(tmp_path / "model.jsonl")) == 1  # 仍在暂停队列，读取和接纳不发模型请求。
        client.post("/api/queue/resume").raise_for_status()

        def settled():
            """读取后继作业终态，执行中返回空值以继续轮询。"""
            job = client.get(f"/api/jobs/{child['id']}").raise_for_status().json()
            return job if job["status"] not in {"queued", "preparing", "running"} else None

        result = eventually(settled)
        assert (result["status"], result["validation_status"]) == ("completed", "passed"), result
        assert old_log.read_bytes() == frozen
        new = client.get(f"/api/runs/{child['run_id']}").raise_for_status().json()
        assert new["recovery_source_id"] == source["run_id"]
        assert new["session_id"] == source["session_id"]
        assert new["result"]["permissions"]["spent_model_turns"] == 1
        assert new["result"]["permissions"]["spent_tool_calls"] == (phase != "model")
        assert len(entries(tmp_path / "effects.jsonl")) == 1
        assert len((tmp_path / "verifier-count.txt").read_text().splitlines()) == 1
        assert len(entries(tmp_path / "model.jsonl")) == (3 if phase in {"intent", "model"} else 2)
        assert Path(new["workspace"], "new.txt" if phase == "created" else "app.txt").read_bytes() == AFTER
        assert not list(Path(new["workspace"]).glob(".harness-write-*.tmp"))
        assert (tmp_path / "project" / "app.txt").read_bytes() == BEFORE
        assert len([item for item in client.get("/api/jobs").json()["items"] if item.get("recovery_from") == source["id"]]) == 1
        old = client.get(f"/api/runs/{source['run_id']}").raise_for_status().json()
        sse = client.get(f"/api/runs/{source['run_id']}/stream", headers={"Last-Event-ID": str(old["last_seq"] - 1)})
        assert sse.text.count("event: run_event") == 1 and '"interrupted":true' in sse.text
        assert f"id: {old['last_seq']}" in sse.text
        assert entries(tmp_path / "runs" / child["run_id"] / "events.jsonl")[0]["seq"] == 1
        patch = client.get(f"/api/runs/{child['run_id']}/patch")
        assert patch.is_success and b"+status = 'recovered'" in patch.content


def test_real_crash_keeps_unconfirmed_verifier_blocked(tmp_path: Path):
    """宿主硬退出时子验收仍在运行，禁止发布通过、重新启动验收或解除同仓库占用。"""
    port = free_port()
    interrupt(tmp_path, port, "verification")
    try:
        with backend(tmp_path, port) as (_, _, client):
            source = first_job(client)
            assert source["recovery"]["state"] == "blocked", source
            assert "验收进程" in source["recovery"]["reason"]
            assert source["validation_status"] == "unverified"
            assert client.post(f"/api/jobs/{source['id']}/recover", json={
                "request_id": str(uuid4()), "expected_revision": "0" * 64}).status_code == 409
            client.post("/api/queue/resume").raise_for_status()
            second = launch(client).raise_for_status().json()
            assert second["status"] == "queued" and second["blocked_by"] == source["id"]
            assert not (tmp_path / "verifier-exited").exists()
            assert len((tmp_path / "verifier-count.txt").read_text().splitlines()) == 1
    finally:
        (tmp_path / "release-verifier").touch()
        eventually((tmp_path / "verifier-exited").exists)


def test_same_bytes_from_another_file_are_not_a_receipt(tmp_path: Path):
    """目标由外部替换为相同字节时，原恢复版本失效并保留租约。"""
    port = free_port()
    interrupt(tmp_path, port, "applied")
    with backend(tmp_path, port) as (_, _, client):
        source = first_job(client)
        assert source["recovery"]["state"] == "ready"
        data = json.loads((tmp_path / "fault.json").read_text())
        path = Path(data["workspace"]) / "app.txt"
        replacement = path.with_name("external")
        replacement.write_bytes(AFTER)
        replacement.replace(path)
        url = f"/api/jobs/{source['id']}"
        assert client.post(url + "/recover", json={"request_id": str(uuid4()),
                           "expected_revision": source["recovery"]["revision"]}).status_code == 409
        assert client.get(url).json()["recovery"]["state"] == "blocked"
        assert len(entries(tmp_path / "effects.jsonl")) == len(entries(tmp_path / "model.jsonl")) == 1


def test_terminal_before_queue_update_is_reconciled_without_execution(tmp_path: Path):
    """run_finished 已落盘而作业状态未提交时，只补记原作业终态。"""
    port = free_port()
    interrupt(tmp_path, port, "terminal")
    with backend(tmp_path, port) as (_, _, client):
        source = first_job(client)
        assert (source["status"], source["validation_status"]) == ("completed", "passed")
        assert "recovery" not in source
        assert len(entries(tmp_path / "effects.jsonl")) == 1
        assert len(entries(tmp_path / "model.jsonl")) == 2
        assert len((tmp_path / "verifier-count.txt").read_text().splitlines()) == 1


def test_second_crash_before_recovery_preparation_preserves_request(tmp_path: Path):
    """租约转移后再实杀，恢复同一请求的排队状态，保持原工作树及唯一后继。"""
    port = free_port()
    interrupt(tmp_path, port, "applied")
    with backend(tmp_path, port) as (process, pid, client):
        source = first_job(client)
        body = {"request_id": str(uuid4()), "expected_revision": source["recovery"]["revision"]}
        url = f"/api/jobs/{source['id']}/recover"
        child = client.post(url, json=body).raise_for_status().json()
        (tmp_path / "arm.txt").write_text("claimed", encoding="utf-8")
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(client.post, "/api/queue/resume")
            eventually(lambda: json.loads((tmp_path / "fault.json").read_text())["phase"] == "claimed")
            terminate_backend(process, pid)
            assert process.wait(timeout=10) != 0
            try:
                response.result()
            except httpx.TransportError:
                pass
    (tmp_path / "arm.txt").write_text("", encoding="utf-8")
    with backend(tmp_path, port) as (_, _, client):
        assert client.post(url, json=body).raise_for_status().json()["run_id"] == child["run_id"]
        assert client.get(f"/api/jobs/{child['id']}").json()["status"] == "queued"
        client.post("/api/queue/resume").raise_for_status()
        eventually(lambda: client.get(f"/api/jobs/{child['id']}").json()["status"] == "completed")
        assert len(entries(tmp_path / "effects.jsonl")) == 1
        assert len(entries(tmp_path / "model.jsonl")) == 2


@pytest.mark.parametrize("scenario", ["damaged_log", "replaced_workspace", "budget"])
def test_uncertain_source_never_starts_new_attempt(tmp_path: Path, scenario: str):
    """破损记录、替换工作树或原预算耗尽时，后台只提供人工核对。"""
    port = free_port()
    if scenario == "budget":
        (tmp_path / "budget-one").touch()
    interrupt(tmp_path, port, "applied")
    if scenario == "damaged_log":
        events = next((tmp_path / "runs").glob("*/events.jsonl"))
        with events.open("ab") as stream:
            stream.write(b'{"partial":')
    if scenario == "replaced_workspace":
        target = Path(json.loads((tmp_path / "fault.json").read_text())["workspace"])
        assert target.parent == tmp_path / "trees"
        target.rename(target.with_name("moved-tree"))
        target.mkdir()
    with backend(tmp_path, port) as (_, _, client):
        source = first_job(client)
        assert source["status"] == "needs_review" and source["recovery"]["state"] == "blocked"
        assert len(entries(tmp_path / "effects.jsonl")) == len(entries(tmp_path / "model.jsonl")) == 1
