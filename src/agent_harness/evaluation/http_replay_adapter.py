"""评分容器内仅重放冻结清单中的只读 HTTPS GET，其余请求沿用容器原有的无网行为。"""

import hashlib
import io
import json
from pathlib import Path


def install(folder=Path("/harness-http-cache"), hits=Path("/tmp/harness-http-hits.jsonl")):
    """从只读 folder 安装 requests 适配，每次命中追加到容器内 hits；仅独立评分解释器调用。"""
    import requests

    original = requests.adapters.HTTPAdapter.send
    manifest = json.loads((folder / "responses.json").read_bytes())
    cached = {(row["method"], row["url"]): row for row in manifest["responses"]}

    def send(adapter, request, *args, **kwargs):
        """按 request 方法和 URL 重放；带身份等未录制请求的 args 与 kwargs 原样交原适配器。"""
        row = cached.get((request.method, request.url))
        sensitive = {"authorization", "cookie", "range", "if-modified-since", "if-none-match"}
        if row is None or request.body or sensitive.intersection(k.lower() for k in request.headers):
            return original(adapter, request, *args, **kwargs)
        body = (folder / row["body_sha256"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
            raise ValueError("冻结 HTTPS 响应内容发生变化")
        response = requests.Response()
        response.status_code, response.reason, response.url = row["status"], row["reason"], request.url
        response.headers = requests.structures.CaseInsensitiveDict(row["headers"])
        response.request, response.connection = request, adapter
        response.raw = io.BytesIO(body)
        response._content, response._content_consumed = body, True
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        with hits.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"method": request.method, "url": request.url, "status": row["status"],
                                     "body_sha256": row["body_sha256"]}) + "\n")
        return response

    requests.adapters.HTTPAdapter.send = send
