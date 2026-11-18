"""在独立进程中按题验证 Click 副本，预检工作区不包含评分代码。"""

import argparse
import difflib
import importlib
import json
import sys
from enum import Enum
from pathlib import Path
from unittest.mock import patch


def _usage_default(click, runner) -> None:
    """用指定 click 副本和 runner 执行未配置的帮助命令，差异以断言报告。"""
    command = click.Command("report")
    reply = runner.invoke(command, ["--help"])
    assert reply.exit_code == 0
    assert reply.output.startswith("Usage: report ")


def _usage_context(click, runner) -> None:
    """用指定 click 副本和 runner 核对 CLI 根命令前缀、子命令继承及覆盖。"""
    group = click.Group("report", context_settings={"usage_prefix": "用法示例 "})
    group.add_command(click.Command("inherit"))
    group.add_command(click.Command("hidden", context_settings={"usage_prefix": ""}))
    group.add_command(click.Command("own", context_settings={"usage_prefix": "Custom "}))

    expected = {
        ("--help",): "用法示例 report ",
        ("inherit", "--help"): "用法示例 report inherit ",
        ("hidden", "--help"): "report hidden ",
        ("own", "--help"): "Custom report own ",
    }
    for args, prefix in expected.items():
        reply = runner.invoke(group, args, catch_exceptions=False)
        assert reply.exit_code == 0, reply.output
        assert reply.output.startswith(prefix), reply.output


def _usage_formatter(click, runner) -> None:
    """直接检查 click 的格式器，runner 用于统一评分签名，无需调用 CLI。"""
    command = click.Command("report")
    ctx = click.Context(command, usage_prefix="Default ")
    formatter = ctx.make_formatter()
    formatter.write_usage("first", prefix="Explicit ")
    formatter.write_usage("second", prefix="")
    formatter.write_usage("third")
    assert formatter.getvalue() == "Explicit first\nsecond\nDefault third\n"


class _ReportStyle(Enum):
    """候选应使用枚举的规范化名字，不能意外返回底层数值。"""

    DRY_RUN = 3  # 演练报告。
    SAFE_MODE = 9  # 常规报告。


def _choice_output(click, runner) -> None:
    """用 click 副本构造命令，runner 核对成功转换、旧报错与退出码。"""

    @click.command()
    @click.option("--style", type=click.Choice(_ReportStyle, case_sensitive=False))
    def report(style):
        """把 Click 转换后的 style 枚举数值输出给外部评分，无返回值。"""
        click.echo(str(style.value))

    correct = runner.invoke(report, ["--style", "SAFE_MODE"])
    assert (correct.exit_code, correct.output) == (0, "9\n")
    bad = runner.invoke(report, ["--style", "wrong"])
    assert bad.exit_code == 2
    assert "Invalid value for '--style': 'wrong' is not one of 'dry_run', 'safe_mode'." in bad.stderr


def _normalize_token(token: str) -> str:
    """将传入的命令行 token 中的短横线转换为下划线，返回规范形式。"""
    return token.replace("-", "_")


def _choice_candidates(click, runner) -> None:
    """直接校验 click 的结构化异常，runner 用于统一评分签名。"""
    choice = click.Choice(["Mixed-CASE", "another-value"], case_sensitive=False)
    option = click.Option(["--style"], type=choice)
    ctx = click.Context(click.Command("report"), token_normalize_func=_normalize_token)
    try:
        choice.convert("wrong", option, ctx)
    except click.BadParameter as error:
        assert error.choice_candidates == ("mixed_case", "another_value")
        assert error.param is option and error.ctx is ctx
        assert "'mixed_case', 'another_value'" in error.format_message()
    else:
        raise AssertionError("无效值未产生 BadParameter")

    choice = click.Choice(_ReportStyle, case_sensitive=False)
    try:
        choice.convert("wrong", None, None)
    except click.BadParameter as error:
        assert error.choice_candidates == ("dry_run", "safe_mode")
    else:
        raise AssertionError("无效枚举名未失败")
    assert click.BadParameter("unrelated").choice_candidates is None


def _plain_exit(click, runner) -> None:
    """用 click 副本和 runner 核对不带说明时的退出码、返回值和输出。"""

    @click.command()
    @click.pass_context
    def report(ctx):
        """让当前命令上下文 ctx 发出静默退出信号，无返回值。"""
        ctx.exit(7)

    standalone = runner.invoke(report)
    assert (standalone.exit_code, standalone.stdout, standalone.stderr) == (7, "", "")
    embedded = runner.invoke(report, standalone_mode=False)
    assert (embedded.exit_code, embedded.return_value, embedded.stderr) == (0, 7, "")


def _exit_message(click, runner) -> None:
    """用 click 与 runner 校验退出消息的输出流、清理顺序和嵌入模式。"""

    def announce_cleanup() -> None:
        """在上下文清理时向 stderr 写出顺序标记。"""
        click.echo("closing", err=True)

    @click.command()
    @click.option("--code", type=int, default=0)
    @click.option("--message", default="done")
    @click.pass_context
    def report(ctx, code, message):
        """注册 ctx 清理标记，并使用 CLI 给定的 code 与 message 退出。"""
        ctx.call_on_close(announce_cleanup)
        ctx.exit(code, message=message)

    for code, message in [(0, "done"), (9, "closing reason"), (0, "")]:
        reply = runner.invoke(report, ["--code", str(code), "--message", message], catch_exceptions=False)
        assert (reply.exit_code, reply.stdout, reply.stderr) == (code, "", f"closing\n{message}\n")
    embedded = runner.invoke(report, ["--code", "7"], standalone_mode=False, catch_exceptions=False)
    assert (embedded.exit_code, embedded.return_value, embedded.stderr) == (0, 7, "closing\n")

    exit_type = importlib.import_module("click.exceptions").Exit
    assert exit_type(3, message="detail").message == "detail"
    assert exit_type().message is None


# 保证给定拼写能产生多于三个候选，区分自定义限制和默认限制。
_COMMANDS = ("stage", "stager", "stage1", "stage2", "stag", "stages")
_MISSPELLING = "stge"


def _suggest_default(click, runner) -> None:
    """用 click 副本和 runner 核对独立异常和未配置 CLI 的候选及退出状态。"""
    error_type = importlib.import_module("click.exceptions").NoSuchCommand
    expected = difflib.get_close_matches(_MISSPELLING, _COMMANDS)
    assert len(expected) == 3
    assert error_type(_MISSPELLING, possibilities=_COMMANDS).possibilities == expected

    group = click.Group("report", commands=[click.Command(name) for name in _COMMANDS])
    reply = runner.invoke(group, [_MISSPELLING])
    assert reply.exit_code == 2
    assert "No such command 'stge'." in reply.stderr
    assert all(name in reply.stderr for name in expected)


def _suggest_limits(click, runner) -> None:
    """用 click 副本和 runner 核对建议设定的传递、关闭及无效值。"""
    error_type = importlib.import_module("click.exceptions").NoSuchCommand
    commands = [click.Command(name) for name in _COMMANDS]
    child = click.Group("child", commands=commands)
    group = click.Group("report", commands=[*commands, child], context_settings={"command_suggestion_limit": 0})

    with patch("difflib.get_close_matches", side_effect=AssertionError("关闭时不应计算候选")):
        for args in [[_MISSPELLING], ["child", _MISSPELLING]]:
            reply = runner.invoke(group, args, standalone_mode=False)
            assert isinstance(reply.exception, error_type), reply.exception
            assert not reply.exception.possibilities
            assert reply.exception.format_message() == "No such command 'stge'."

    child.context_settings["command_suggestion_limit"] = 5
    reply = runner.invoke(group, ["child", _MISSPELLING], standalone_mode=False)
    assert isinstance(reply.exception, error_type), reply.exception
    expected = difflib.get_close_matches(_MISSPELLING, _COMMANDS, n=5)
    assert len(expected) == 5 and reply.exception.possibilities == expected

    try:
        click.Context(click.Command("report"), command_suggestion_limit=-1)
    except ValueError:
        pass
    else:
        raise AssertionError("负数上限未被拒绝")


# 每题分别列出基线必须通过的已有行为，以及参考解才应通过的新功能。
CHECKS = {
    "cross-click-usage-prefix": ((_usage_default,), (_usage_context, _usage_formatter)),
    "cross-click-choice-candidates": ((_choice_output,), (_choice_candidates,)),
    "cross-click-exit-message": ((_plain_exit,), (_exit_message,)),
    "cross-click-command-suggestion-limit": ((_suggest_default,), (_suggest_limits,)),
}


def main() -> None:
    """加载指定副本并跑所选题的两组断言，通过标准输出返回 JSON。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("case_id", choices=CHECKS)
    args = parser.parse_args()
    sys.path.insert(0, str(args.workspace))
    click = importlib.import_module("click")
    if Path(click.__file__).resolve() != (args.workspace / "click" / "__init__.py").resolve():
        raise RuntimeError("评分进程未载入指定副本")

    runner = importlib.import_module("click.testing").CliRunner()
    grouped = {}
    for group, checks in zip(("regression", "feature"), CHECKS[args.case_id], strict=True):
        grouped[group] = {}
        for check in checks:
            try:
                check(click, runner)
            except Exception as exc:
                grouped[group][check.__name__] = {"passed": False, "exception": type(exc).__name__, "message": str(exc)}
            else:
                grouped[group][check.__name__] = {"passed": True}
    print(json.dumps(grouped, ensure_ascii=True))


if __name__ == "__main__":
    main()
