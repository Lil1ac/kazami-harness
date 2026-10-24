"""真实本机文件夹选择与首页验收服务；模型离线，文件、Git及项目测试均真实。"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

import uvicorn

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


def main() -> None:
    """创建有归属标记的专属资源并启动--port服务，默认18780；保留已有目录与用户草稿。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18780)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    root = project / ".runtime" / "open-project-browser"
    marker = root / ".fixture-owned"
    owner = "harness-open-project-browser-20261023"  # 防止覆盖非本轮目录。
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != owner):
        raise RuntimeError("目录不属于本轮辅助服务。")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(owner, encoding="utf-8")
    source = root / "我的 后端项目"
    if not source.exists():
        source.mkdir()
        (source / "app.py").write_text("value = 1\n", encoding="utf-8")
        (source / "test_app.py").write_text("from app import value\n\ndef test_value():\n    \"\"\"已有项目测试核对真实修改。\"\"\"\n    assert value == 2\n", encoding="utf-8")
        (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
        for arguments in (["init","-q"], ["config","core.autocrlf","false"], ["add","."],
                          ["-c","user.name=Fixture","-c","user.email=fixture@invalid","commit","-qm","baseline"]):
            subprocess.run(["git",*arguments],cwd=source,check=True,capture_output=True,timeout=10)
        (source / "local.txt").write_text("用户的未提交文件保持。\n",encoding="utf-8")
    sys.path.insert(0,str(project / "tests" / "web"))
    from test_project_catalog import EditingModel
    os.environ["DEEPSEEK_API_KEY"] = "offline-open-project-browser"
    app = create_app(
        WebConfig(project_dir=project,runtime_root=root/"copies",frontend_dist=project/"frontend"/"dist"),
        RunConfig(tools=["read_file","write_file"],skill_roots=[project/"examples"/"skills"]),
        root/"runs",model_client=EditingModel(),
    )
    uvicorn.run(app,host="127.0.0.1",port=args.port,loop="asyncio",timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
