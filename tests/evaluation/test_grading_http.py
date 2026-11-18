"""验证公开响应只影响独立评分容器的两个真实资源，原始状态、字节及其他请求保持可核对。"""

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
import requests

from agent_harness.evaluation.grading_http import CachedGradingEnvironment, cache_receipt
from agent_harness.evaluation.http_replay_adapter import install
from agent_harness.evaluation.repository import RepositoryEnvironment

CACHE = Path(__file__).resolve().parents[2] / "evals/repository-comparison/grader-http"  # 获批冻结的原始响应体。


def test_only_captured_public_gets_are_replayed(tmp_path, monkeypatch):
    """在 tmp_path 记录命中，对新增头或陌生 URL 走原发送路径，不访问网络。"""
    def unmatched(adapter, request, *args, **kwargs):
        """原适配器的离线替身，返回 request 的 URL 供校验透传。"""
        return request.url

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", unmatched)
    hits = tmp_path / "hits.jsonl"
    install(CACHE, hits)
    adapter = requests.adapters.HTTPAdapter()
    for entry in json.loads((CACHE / "responses.json").read_bytes())["responses"]:
        prepared = requests.Request("GET", entry["url"]).prepare()
        response = adapter.send(prepared)
        assert response.status_code == entry["status"]
        assert hashlib.sha256(response.content).hexdigest() == entry["body_sha256"]
        prepared.headers["If-None-Match"] = "different"
        assert adapter.send(prepared) == entry["url"]
        assert adapter.send(requests.Request("HEAD", entry["url"]).prepare()) == entry["url"]
    assert len(hits.read_text().splitlines()) == 2


def test_cache_is_host_grader_only(tmp_path, monkeypatch):
    """在 tmp_path 隔离环境核对容器挂载，复现、导出和普通试次容器都无评分缓存。"""
    seen = []

    async def record(environment, command, mounts, seconds, *, deadline):
        """接收宿主容器的命令、挂载及期限，记录后返回成功，不启动 Docker。"""
        seen.append((command, mounts))
        return {"exit_code": 0}

    monkeypatch.setattr(RepositoryEnvironment, "container", record)
    env = CachedGradingEnvironment({"instance_id": "sphinx-doc__sphinx-10435"}, tmp_path, None, Path(__file__), http_cache=CACHE)
    for kind in ("export", "repro", "grade"):
        asyncio.run(env.container(["-c", "true", kind, "commit"], []))
    assert seen[0][1] == seen[1][1] == []
    assert any("target=/harness-http-cache,readonly" in value for value in seen[2][1])
    assert "/opt/miniconda3/envs/testbed" in seen[2][0][1]
    with pytest.raises(ValueError, match="未登记"):
        CachedGradingEnvironment({"instance_id": "other"}, tmp_path, None, Path(__file__), http_cache=CACHE)
    assert cache_receipt(CACHE)["count"] == 2
