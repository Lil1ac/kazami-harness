"""启动分层上下文压缩的显式离线浏览器验收；不访问真实模型。"""

import argparse
import os
from pathlib import Path
import sys

import uvicorn

from agent_harness.schemas import ContextPolicy, RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebVerifier, WebWorkspace


def main() -> None:
    """读取--port（默认18774）启动带归属标记的独立服务；保留记录，不覆盖用户项目或密钥。"""
    parser = argparse.ArgumentParser(description="分层压缩浏览器验收")
    parser.add_argument("--port", type=int, default=18774)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project / "tests/web"))
    from test_compression_api import CompressionEditingModel

    root = project / ".runtime/compression-browser"
    marker = root / ".fixture-owned"
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != "harness-compression-browser"):
        raise RuntimeError("同名目录没有正确归属标记")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text("harness-compression-browser", encoding="utf-8")
    workspace = root / "source"
    workspace.mkdir(exist_ok=True)
    initial = {
        "AGENTS.md": "保留 RULE_COMPRESSION 和兼容接口。禁止修改验收入口。\n",
        "small.txt": "真实代码任务的文件证据。\n",
        "app.py": "def add(a,b):\n    return a-b\n",
        "large.txt": "HEAD\n" + "x" * 6000 + "\nMIDDLE_EVIDENCE\n" + "y" * 1000 + "\nTAIL\n",
    }
    for name, content in initial.items():
        path = workspace / name
        if not path.exists():
            path.write_bytes(content.encode("utf-8"))
    verifier = root / "verify.py"
    if not verifier.exists():
        verifier.write_text(
            '"""宿主独立检查本轮app.py的加法与规则标记。"""\n'
            'import os,runpy\nfrom pathlib import Path\n'
            'p=Path(os.environ["HARNESS_WORKSPACE"])/"app.py"\n'
            'assert "RULE_COMPRESSION" in p.read_text(encoding="utf-8")\n'
            'assert runpy.run_path(str(p))["add"](2,3)==5\n'
            'print("host checks passed")\n', encoding="utf-8",
        )
    toml = root / "verify.toml"
    if not toml.exists():
        toml.write_text('argv = ["python", "verify.py"]\ncwd = "."\ntimeout_seconds = 10.0\n', encoding="utf-8")
    os.environ["DEEPSEEK_API_KEY"] = "offline-compression-browser-key"
    config = WebConfig(
        project_dir=project, runtime_root=root / "copies", frontend_dist=project / "frontend/dist",
        workspaces=[WebWorkspace(id="compression", label="上下文压缩联动（离线）", path=workspace, copy_on_run=True)],
        verifiers=[WebVerifier(id="compression", label="代码加法与规则验收", description="仅检查本服务的app.py。", path=toml)],
    )
    host = RunConfig(
        requested_model="offline-compression", tools=["read_file", "write_file"],
        context=ContextPolicy(compression="hybrid", max_input_bytes=15000, inline_tool_bytes=1024, keep_recent_groups=1, max_summary_calls=4),
    )
    app = create_app(config, host, root / "runs", model_client=CompressionEditingModel(delay=0.2))
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
