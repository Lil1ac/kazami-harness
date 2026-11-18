"""在干净评分镜像的探针进程内记录真正返回的 HTTP 请求或实际启动的命令。"""

import json
import os
import runpy
import sys


def observe(script, kind, target):
    """运行 script 并保留退出语义；kind 指定入口类型，target 是公开命令或 API 名，页面时为空。"""
    evidence = []  # 仅保留有限数量的路由、方法、状态或程序名，不收集正文和凭据。

    def profile(frame, event, value):
        """从解释器 frame 的返回事件提取边界数据；value 是实际返回值，普通函数直接忽略。"""
        if event != "return" or len(evidence) >= 48:
            return
        name = frame.f_code.co_name
        module = frame.f_globals.get("__name__", "")
        if kind == "api" and name == target.rsplit(".", 1)[-1] and frame.f_code.co_filename.startswith("/testbed/"):
            receiver = frame.f_locals.get("self")
            cls = type(receiver) if receiver is not None else frame.f_locals.get("cls")
            aliases = {module + "." + name}
            if isinstance(cls, type):
                for parent in cls.__mro__:
                    aliases.update((parent.__name__ + "." + name, parent.__module__ + "." + parent.__name__ + "." + name))
            # 包顶层可能重新导出子模块函数；只在运行时导出的对象确实共享此代码对象时接受别名。
            parent_module = sys.modules.get(target.rpartition(".")[0])
            exported = vars(parent_module).get(name) if parent_module else None
            if getattr(exported, "__code__", None) is frame.f_code:
                aliases.add(target)
            if target in aliases and not evidence:
                evidence.append({"symbol": target, "implementation": module + "." + name})
        if kind in ("http", "admin") and name == "get_response" and module == "django.core.handlers.base":
            request = frame.f_locals.get("request")
            match = getattr(request, "resolver_match", None)
            status = getattr(value, "status_code", None)
            if isinstance(status, int):
                evidence.append({"method": request.method, "path": request.path, "status": status,
                                 "route": getattr(match, "view_name", "") or "",
                                 "admin": "admin" in (getattr(match, "app_names", None) or [])})
        elif kind == "http" and name == "open" and module in {"werkzeug.test", "flask.testing"}:
            request = getattr(value, "request", None)
            if request is not None and isinstance(getattr(value, "status_code", None), int):
                evidence.append({"method": request.method, "path": request.path, "status": value.status_code})
        elif kind == "cli" and name == "__init__" and module == "subprocess":
            process = frame.f_locals.get("self")
            args = getattr(process, "args", None)
            if isinstance(args, (list, tuple)) and args and getattr(process, "pid", None):
                command = [os.path.basename(os.fsdecode(args[0]))]
                if len(args) >= 3 and args[1] == "-m":
                    command += ["-m", str(args[2])]
                elif len(args) >= 2 and str(args[0]).split("/")[-1].startswith("python"):
                    command += [os.path.basename(str(args[1]))]
                evidence.append({"command": command})

    sys.argv = [script]
    sys.path[0] = os.path.dirname(script)
    previous = sys.getprofile()  # 嵌入测试也能恢复调用方已有的观察器。
    sys.setprofile(profile)
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        sys.setprofile(previous)
        print("HARNESS_ENTRY_TRACE", json.dumps(evidence, ensure_ascii=True))


if __name__ == "__main__":
    observe(sys.argv[1], sys.argv[2], sys.argv[3])
