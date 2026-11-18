"""把真实外部测试资源接入独立评分阶段，保持求解容器、上游测试和评分脚本原样。"""

import json
from pathlib import Path

from agent_harness.evaluation.reproduction import digest_bytes
from agent_harness.evaluation.repository import RepositoryEnvironment


def cache_receipt(folder: Path) -> dict:
    """核验只读 folder 中声明的响应体和评分接入代码，返回可纳入环境证书的指纹。"""
    manifest = folder / "responses.json"
    spec = json.loads(manifest.read_bytes())
    seen = set()
    for row in spec["responses"]:
        key = row["method"], row["url"]
        signature = row["body_sha256"]
        if (key in seen or key[0] != "GET" or not key[1].startswith("https://") or len(signature) != 64
                or any(char not in "0123456789abcdef" for char in signature)):
            raise ValueError("冻结响应条目无效")
        seen.add(key)
        body = (folder / signature).read_bytes()
        if len(body) != row["bytes"] or digest_bytes(body) != signature:
            raise ValueError("冻结响应体与清单不一致")
    return {"responses_sha256": digest_bytes(manifest.read_bytes()), "count": len(seen),
            "adapter_sha256": digest_bytes(Path(__file__).with_name("http_replay_adapter.py").read_bytes()),
            "integration_sha256": digest_bytes(Path(__file__).read_bytes())}


class CachedGradingEnvironment(RepositoryEnvironment):
    """拥有同一仓库执行环境的独立评分器，只有宿主的 grade 容器得到公开资源缓存。"""

    def __init__(self, *args, http_cache: Path, **kwargs):
        """转交仓库材料 args 和环境选项 kwargs；http_cache 是协议批准的冻结响应目录。"""
        super().__init__(*args, **kwargs)
        self.http_cache = http_cache.resolve(strict=True)  # 只读宿主资源，不进入求解工作树。
        self.http_identity = cache_receipt(self.http_cache)  # 预检与付费双组均核对同一组 SHA。
        if self.record["instance_id"] not in json.loads((self.http_cache / "responses.json").read_bytes())["case_ids"]:
            raise ValueError("缓存未登记该题")

    async def prepare(self, expected=None):
        """核验原环境与独立评分的缓存指纹；expected 非空时必须匹配对应的有效预检证书。"""
        original = None
        if expected is not None:
            original = dict(expected)
            if original.pop("grading_http_cache", None) != self.http_identity:
                raise ValueError("独立评分 HTTPS 缓存与预检不同")
        return {**await super().prepare(original), "grading_http_cache": self.http_identity}

    async def container(self, command, mounts, seconds=600, *, deadline=None):
        """只为 grade 入口添加独立缓存挂载，其他 command、mounts、时限与清理遵从父环境。"""
        if len(command) == 4 and command[0] == "-c" and command[2] == "grade":
            if cache_receipt(self.http_cache) != self.http_identity:
                raise ValueError("冻结之后评分缓存发生变化")
            adapter = Path(__file__).with_name("http_replay_adapter.py").resolve()
            mounts = [*mounts, "--mount", f"type=bind,source={self.http_cache},target=/harness-http-cache,readonly",
                      "--mount", f"type=bind,source={adapter},target=/harness-http-code/http_replay_adapter.py,readonly"]
            # 官方 tox --current-env 使用预装 testbed 解释器。挂载和 .pth 都随这一个评分容器销毁。
            setup = """site_dir=$(/opt/miniconda3/envs/testbed/bin/python -c 'import sysconfig; print(sysconfig.get_path("purelib"))') || exit 96
printf '%s\\n' /harness-http-code 'import http_replay_adapter; http_replay_adapter.install()' >"$site_dir/harness_grading_http.pth" || exit 96
"""
            script = setup + "(\n" + command[1] + "\n)\nresult=$?\n" + (
                'if [ -f /tmp/harness-http-hits.jsonl ]; then sed "s/^/HARNESS_HTTP_REPLAY /" /tmp/harness-http-hits.jsonl; fi\n'
                'exit "$result"')
            command = [command[0], script, *command[2:]]
        return await super().container(command, mounts, seconds, deadline=deadline)
