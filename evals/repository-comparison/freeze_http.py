"""经 TLS 实际获取上游公开测试引用的原始响应；只读缓存已有文件，不能合成缺失资源。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx

URLS = ("https://www.python.org/static/img/python-logo.png", "https://www.google.com/NOT_EXIST.PNG")  # 公开测试文档里的远程图片。
HERE = Path(__file__).resolve().parent / "grader-http"  # 经验证的冻结副本，只注入独立评分容器。


def freeze(downloads, proxy):
    """把两次真实 GET 的响应体先存至 downloads，然后冻结到评分夹具；proxy 为空时直连。"""
    manifest = HERE / "responses.json"
    if manifest.exists():
        for row in json.loads(manifest.read_bytes())["responses"]:
            if hashlib.sha256((HERE / row["body_sha256"]).read_bytes()).hexdigest() != row["body_sha256"]:
                raise ValueError("已冻结响应体损坏")
        print("cached", hashlib.sha256(manifest.read_bytes()).hexdigest())
        return
    downloads.mkdir(parents=True, exist_ok=True)
    HERE.mkdir(parents=True, exist_ok=True)
    rows = []
    with httpx.Client(trust_env=False, proxy=proxy, follow_redirects=False, timeout=40,
                      headers={"Accept-Encoding": "identity"}) as client:
        for index, url in enumerate(URLS):
            response = client.get(url)
            body = response.content
            digest = hashlib.sha256(body).hexdigest()
            if len(body) > 2**20 or response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("响应超限或存在传输编码，须核对原始字节")
            (downloads / digest).write_bytes(body)
            print("GET", url, response.status_code, len(body), digest, flush=True)
            # 不把封锁页、代理报错或临时重定向作为上游资源。
            if (index == 0 and (response.status_code != 200 or not body.startswith(b"\x89PNG\r\n\x1a\n"))
                    or index == 1 and response.status_code != 404):
                raise ValueError("实取结果与公开资源身份不符，保留原始接收字节并停止冻结")
            (HERE / digest).write_bytes(body)
            rows.append({"method": "GET", "url": url, "status": response.status_code, "reason": response.reason_phrase,
                         "bytes": len(body), "body_sha256": digest, "fetched_at": datetime.now(timezone.utc).isoformat(),
                         "headers": {k: response.headers[k] for k in ("content-type", "content-length", "last-modified", "etag", "date")
                                     if k in response.headers}})
    receipt = {"case_ids": ["sphinx-doc__sphinx-10435"], "source": "tests/roots/test-images/index.rst",
               "original_preflight": "bc94b6aa-ed68-44b8-a877-d5e58fe3f350", "responses": rows}
    with manifest.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    print("frozen", hashlib.sha256(manifest.read_bytes()).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--downloads", type=Path, required=True)
    parser.add_argument("--proxy")
    args = parser.parse_args()
    freeze(args.downloads, args.proxy)
