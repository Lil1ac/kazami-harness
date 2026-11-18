"""使用脚本模型启动真实本机控制台，供浏览器验证整理和评测结果。"""

import argparse
from pathlib import Path

import uvicorn

from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.helpers.models import configured_run
from tests.web.test_memory_consolidation import ConsolidationModel


def main() -> None:
    """解析专属工作目录和本机端口，启动真实 HTTP；runs-dir 可绑定已有评测报告。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path)
    parser.add_argument("--port", type=int, default=18847)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    workspaces = []
    for identifier, label in (("one", "记忆验收项目"), ("two", "独立验收项目")):
        workspace = root / identifier
        workspace.mkdir(parents=True, exist_ok=True)
        workspaces.append(WebWorkspace(id=identifier, label=label, path=workspace))
    web = WebConfig(project_dir=root, worktrees_root=root / "trees", workspaces=workspaces,
                    frontend_dist=Path(__file__).resolve().parents[2] / "frontend" / "dist")
    app = create_app(web, configured_run(local=True, tools=["read_file", "write_file"]),
                     args.runs_dir or root / "runs", model_client=ConsolidationModel())
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
