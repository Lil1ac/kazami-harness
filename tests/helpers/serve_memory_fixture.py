"""显式记忆页面验收服务：离线模型驱动真实 HTTP、SQLite、Runner 和文件工具。"""

import argparse
from pathlib import Path

import uvicorn

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig, WebWorkspace
from tests.web.test_memory_api import MemoryModel


def main() -> None:
    """从命令行读取专属 data-dir 和 port；启动本机服务，所有样例仅写指定验收目录。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18839)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspaces = []
    for identifier, label in (("one", "记忆验收项目"), ("two", "独立项目")):
        path = root / identifier
        path.mkdir(exist_ok=True)
        workspaces.append(WebWorkspace(id=identifier, label=label, path=path))
    web = WebConfig(project_dir=root, worktrees_root=root / "trees", frontend_dist=Path(__file__).resolve().parents[2] / "frontend/dist", workspaces=workspaces)
    app = create_app(web, RunConfig(tools=["read_file", "write_file"]), root / "runs", model_client=MemoryModel())

    def key(config):
        """给离线 config 提供凭据占位，不读取或发送用户密钥，返回固定字符串。"""
        return "offline-memory-fixture"

    app.state.manager._key_provider = key
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
