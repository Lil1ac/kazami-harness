"""项目绑定与首页的专属离线页面验收服务；原生选择返回受控目录，其余链路真实执行。"""

import argparse
import os
from pathlib import Path

import uvicorn

from agent_harness.schemas import RunConfig
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig
from tests.web.test_project_catalog import EditingModel, repository


class FixturePicker:
    """依次返回Git、普通、重复Git和取消结果；只代替GUI，目录校验和项目绑定均走生产逻辑。"""

    available = True  # 页面验收允许点击原生选择入口。

    def __init__(self, git: Path, plain: Path) -> None:
        """绑定git与plain测试目录并初始化选择顺序；无文件副作用。"""
        self.paths = [git, plain, git, None]  # 第三次核对去重，第四次核对取消。
        self.index = 0  # 服务生命周期内已完成的选择次数，重启归零。

    async def choose(self) -> Path | None:
        """返回下一次预设目录或None；超出预设次数始终取消，不访问真实用户目录。"""
        selected = self.paths[min(self.index, len(self.paths) - 1)]
        self.index += 1
        return selected


def main() -> None:
    """按--root创建有归属标记的资源并在--port（默认18831）启动；已有项目与记录保持。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18831)
    args = parser.parse_args()
    root = args.root.resolve()
    marker = root / ".fixture-owned"
    owner = "harness-project-binding-20261030"
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != owner):
        raise RuntimeError("目录不属于本次项目绑定验收。")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(owner, encoding="utf-8")
    git = root / "订单 后端"
    plain = root / "研究 文件"
    if not git.exists():
        repository(git)
    if not plain.exists():
        plain.mkdir()
        (plain / "app.py").write_text("value = 1\n", encoding="utf-8")
    project = Path(__file__).resolve().parents[2]
    os.environ["DEEPSEEK_API_KEY"] = "offline-project-binding"
    app = create_app(
        WebConfig(project_dir=project, worktrees_root=root / "copies", frontend_dist=project / "frontend" / "dist"),
        RunConfig(tools=["write_file"]), root / "runs", model_client=EditingModel(),
    )
    app.state.manager.folder_picker = FixturePicker(git, plain)
    uvicorn.run(app, host="127.0.0.1", port=args.port, loop="asyncio", timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
