"""用生产网页验证项目解释器、递归搜索及会话工作树；离线模型，真实工具与项目测试。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import uvicorn

from agent_harness.projects import ProjectPreset
from agent_harness.providers.base import ModelResponse, ToolCall
from tests.helpers.models import configured_run
from agent_harness.web.api import create_app
from agent_harness.web.config import WebConfig


class ExecutionBrowserModel:
    """按本次请求后的工具观察生成固定响应，验证产品链路，不连接外部模型服务。"""

    def __init__(self, evidence: Path) -> None:
        """绑定 evidence 请求日志路径；创建服务前由宿主准备目录，不修改项目文件。"""
        self.evidence = evidence  # 只记录专属验收请求，用于证明页面确实驱动模型/工具链路。

    async def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        """按 messages 最近用户请求调度搜索、改码和命令；tools 为真实启用定义，返回标准响应。"""
        await asyncio.sleep(0.05)
        start = max(index for index, item in enumerate(messages) if item["role"] == "user")
        prompt = messages[start]["content"]
        observed = [item for item in messages[start + 1:] if item["role"] == "tool"]
        number = 3 if "第三轮" in prompt else 2 if "第二轮" in prompt else 1
        with self.evidence.open("a", encoding="utf-8") as target:
            target.write(json.dumps({"prompt": prompt, "step": len(observed), "tools": [tool["function"]["name"] for tool in tools]}, ensure_ascii=False) + "\n")
        calls = []
        if len(observed) == 0:
            calls = [ToolCall(uuid4().hex, "search_files", json.dumps({"query": "def ", "glob": "**/*.py"}))]
        elif len(observed) == 1:
            content = 'def add(left, right):\n    """返回两个数之和；本轮改动供历史快照核对。"""\n    return left + right\n\nROUND = ' + repr(number) + "\n"
            calls = [ToolCall(uuid4().hex, "write_file", json.dumps({"path": "src/calc.py", "content": content, "overwrite": True}, ensure_ascii=False))]
        elif len(observed) == 2:
            program = "import json,os,sys,calc;print(json.dumps({'cwd':os.getcwd(),'python':sys.executable,'source':calc.__file__,'round':calc.ROUND,'sum':calc.add(2,3)}))"
            calls = [ToolCall(uuid4().hex, "run_command", json.dumps({"argv":["python","-c",program]}))]
        elif len(observed) == 3:
            calls = [ToolCall(uuid4().hex, "run_command", json.dumps({"argv":["python","-m","unittest","discover","-s","tests","-v"]}))]
        message = {"role":"assistant","content":None if calls else f"第 {number} 轮修改完成，请查看项目测试与代码证据。"}
        if calls:
            message["tool_calls"] = [{"id":call.call_id,"type":"function","function":{"name":call.name,"arguments":call.arguments_json}} for call in calls]
        return ModelResponse(message=message,tool_calls=calls,finish_reason="tool_calls" if calls else "stop",
                             response_model="offline-execution-browser",usage={"prompt_tokens":5,"completion_tokens":3,"total_tokens":8})


def prepare_sources(root: Path) -> tuple[Path, Path]:
    """在专属 root 首次准备 Git 项目及真实虚拟环境，返回目录和解释器；重启保持已有文件。

    仅复用具有本辅助服务归属标记的目录；不会重置仓库或覆盖用户未知目录。
    """
    marker = root / ".fixture-owned"
    if root.exists() and (not marker.is_file() or marker.read_text(encoding="utf-8") != "execution-browser-20261027"):
        raise RuntimeError("验收目录已存在且不属于本辅助服务")
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text("execution-browser-20261027", encoding="utf-8")
    source = root / "source"
    if not source.exists():
        (source / "src").mkdir(parents=True)
        (source / "tests").mkdir()
        (source / "src/calc.py").write_text('def add(left, right):\n    """待修复的计算函数。"""\n    return left - right\n',encoding="utf-8")
        (source / "main.py").write_text('def main():\n    """根目录搜索探针。"""\n    return "root"\n',encoding="utf-8")
        (source / "tests/test_calc.py").write_text('import unittest\nfrom calc import add\n\nclass CalcTest(unittest.TestCase):\n    """验证项目的已有计算行为。"""\n\n    def test_add(self):\n        """两个数相加必须得到正确结果。"""\n        self.assertEqual(add(2, 3), 5)\n',encoding="utf-8")
        (source / ".gitignore").write_text("__pycache__/\n.venv/\n",encoding="utf-8")
        (source / "README.md").write_text("专属执行配置验收项目。\n",encoding="utf-8")
        for arguments in [["init","-q"],["config","core.autocrlf","false"],["add","."],
                          ["-c","user.name=HarnessFixture","-c","user.email=fixture@localhost","commit","-qm","browser baseline"]]:
            subprocess.run(["git",*arguments],cwd=source,capture_output=True,check=True,timeout=10)
        with (source / "README.md").open("a",encoding="utf-8") as target:
            target.write("用户的未提交说明必须保持。\n")
    environment = root / "project-python"
    executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        subprocess.run([sys.executable,"-m","venv","--without-pip",str(environment)],capture_output=True,check=True,timeout=60)
    return source, executable


def main() -> None:
    """读取 --root 专属临时路径及 --port（默认18794），启动真实生产 API 与前端静态包。"""
    parser=argparse.ArgumentParser(description="执行配置与会话工作树离线页面验收")
    parser.add_argument("--root",type=Path,default=Path("D:/Codex/Temp/harness-execution-workspace-browser-20261027"))
    parser.add_argument("--port",type=int,default=18794)
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[2]
    source,executable=prepare_sources(args.root.resolve())
    # 页面证据与代码共用本次专属根目录，避免新验收混入上一轮历史。
    evidence=args.root.resolve()/"evidence"
    evidence.mkdir(parents=True,exist_ok=True)
    (evidence/"fixture.json").write_text(json.dumps({"source":str(source),"python":str(executable),"port":args.port},ensure_ascii=False,indent=2),encoding="utf-8")
    os.environ["HARNESS_TEST_MODEL_KEY"]="offline-execution-browser"
    config=WebConfig(project_dir=project,worktrees_root=evidence/"worktrees",frontend_dist=project/"frontend/dist",
                     projects=[ProjectPreset(id="execution-project",label="执行配置验收项目",path=source)],workspaces=[])
    run_config=configured_run(requested_model="offline-execution-browser",tools=["read_file","search_files","write_file","run_command"])
    app=create_app(config,run_config,evidence/"runs",model_client=ExecutionBrowserModel(evidence/"requests.jsonl"))
    uvicorn.run(app,host="127.0.0.1",port=args.port,loop="asyncio",workers=1,timeout_graceful_shutdown=2)


if __name__ == "__main__":
    main()
