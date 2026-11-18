"""独立解释器中的官方评分桥；只加载规格和解析模块，不启动依赖Unix resource的官方编排入口。"""

import json
import os
from importlib.metadata import distribution
from pathlib import Path
import sys
import types


def restore_official_test_reporting(script: str, repo: str, setup_commands: list[str]) -> str:
    """在Sphinx评分脚本中恢复上游逐用例日志设置，返回新的脚本文本。

    script是固定swebench版本生成的完整Shell脚本；repo是官方仓库标识，非Sphinx原样返回。
    setup_commands是该仓库版本的官方pre_install列表，只接受上游已经规定的pytest -rA设置。
    不改变用例选择、断言、超时或解析器；若固定上游约定不再匹配，抛ValueError停止评分。
    """
    if repo != "sphinx-doc/sphinx":
        return script
    reporting = "sed -i 's/pytest/pytest -rA/' tox.ini"  # 官方镜像构建时加入、基线reset会清除的日志选项。
    location = "cd /testbed\n"  # 在可信评分目录恢复设置，不读取Agent提供的路径。
    if reporting not in setup_commands or location not in script:
        raise ValueError("Sphinx官方逐用例日志设置与固定协议不符")
    return script.replace(location, location + reporting + "\n", 1)


def main() -> None:
    """从argv读取record/output/log；无log时写官方脚本，有log时写测试判定；异常退出非零。"""
    package = distribution("swebench")
    if package.version != "4.1.0":
        raise ValueError("评分器必须固定为swebench 4.1.0")
    # 此桥只生成脚本与解析文本。提前限制上游依赖初始化的 BLAS 线程，避免占用宿主提交内存。
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    # 上游 __init__ 会导入Unix进程编排；只注册包路径，实际规格、断言列表和解析算法仍执行上游原文件。
    for name in ("swebench", "swebench.harness"):
        module = types.ModuleType(name)
        module.__path__ = [str(package.locate_file(name.replace('.', '/')))]
        sys.modules[name] = module
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.test_spec.python import make_eval_script_list_py
    from swebench.harness.test_spec.test_spec import TestSpec
    from swebench.harness.grading import get_eval_report
    record = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    spec = TestSpec(instance_id=record["instance_id"], repo=record["repo"], version=record["version"],
        repo_script_list=[], env_script_list=[], eval_script_list=make_eval_script_list_py(record,
            MAP_REPO_VERSION_TO_SPECS[record["repo"]][record["version"]], "testbed", "/testbed", record["base_commit"], record["test_patch"]),
        arch="x86_64", FAIL_TO_PASS=json.loads(record["FAIL_TO_PASS"]), PASS_TO_PASS=json.loads(record["PASS_TO_PASS"]),
        language="py", docker_specs={}, namespace="swebench")
    # 宿主在全新镜像内reset基线后评分，因此需恢复被清除的官方日志设置。
    script = restore_official_test_reporting(spec.eval_script, record["repo"],
        MAP_REPO_VERSION_TO_SPECS[record["repo"]][record["version"]].get("pre_install", []))
    value = {"script": script, "image": spec.instance_image_key, "version": package.version}
    if len(sys.argv) > 3:
        patch = Path(sys.argv[3]).with_name("candidate.patch").read_text(encoding="utf-8")
        value = get_eval_report(spec, {"instance_id": spec.instance_id, "model_patch": patch, "model_name_or_path": "agent-harness"}, sys.argv[3], True)[spec.instance_id]
    Path(sys.argv[2]).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
