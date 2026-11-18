"""基于冻结快照生成四个参考修改，只允许写入预检创建的隔离副本。"""

from pathlib import Path


_FORMATTER_RETURN = '''        return self.formatter_class(
            width=self.terminal_width, max_width=self.max_content_width
        )'''
_CONTEXT_SHOW_DEFAULT = '''        show_default: bool | None = None,
    ) -> None:
        #: the parent context'''
_CONTEXT_MAX_WIDTH = '        self.max_content_width = max_content_width'
_BAD_PARAMETER_FIELDS = '''        param_hint: cabc.Sequence[str] | str | None = None,
    ) -> None:
        super().__init__(message, ctx)
        self.param = param
        self.param_hint = param_hint'''
_CHOICE_ERROR = '''        except StopIteration:
            self.fail(
                self.get_invalid_choice_message(value=value, ctx=ctx),
                param=param,
                ctx=ctx,
            )'''
_EXIT_DEFINITION = '''    :param code: the status code to exit with.
    """

    __slots__ = ("exit_code",)

    exit_code: t.Final[int]

    def __init__(self, code: int = 0) -> None:
        self.exit_code = code'''
_SUGGESTION_BODY = '''        if possibilities:
            from difflib import get_close_matches

            possibilities_ = get_close_matches(command_name, possibilities)
        else:
            possibilities_ = None
        self.possibilities = possibilities_'''


# 所有替换锚点在安装源码中必须各出现一次，以免升级或执行路径异常导致误改。
_REFERENCES = {
    "cross-click-usage-prefix": {
        "core.py": (
            (
                _CONTEXT_SHOW_DEFAULT,
                _CONTEXT_SHOW_DEFAULT.replace(
                    "    ) -> None:", "        usage_prefix: str | None = None,\n    ) -> None:"
                ),
            ),
            (
                _CONTEXT_MAX_WIDTH,
                _CONTEXT_MAX_WIDTH + '''

        # None 从父上下文继承；空字符串是有意隐藏标题。
        self.usage_prefix = parent.usage_prefix if usage_prefix is None and parent else usage_prefix''',
            ),
            (
                _FORMATTER_RETURN,
                _FORMATTER_RETURN.replace("return self.", "formatter = self.") + '''
        if self.usage_prefix is not None:
            formatter.usage_prefix = self.usage_prefix
        return formatter''',
            ),
        ),
        "formatting.py": (
            (
                "        self.buffer = []",
                "        self.buffer = []\n        # None 使用默认本地化前缀，空字符串取消前缀。\n        self.usage_prefix: str | None = None",
            ),
            (
                '        if prefix is None:\n            prefix = "{usage} ".format(usage=_("Usage:"))',
                '        if prefix is None:\n            prefix = self.usage_prefix\n        if prefix is None:\n            prefix = "{usage} ".format(usage=_("Usage:"))',
            ),
        ),
    },
    "cross-click-choice-candidates": {
        "exceptions.py": (
            (
                _BAD_PARAMETER_FIELDS,
                _BAD_PARAMETER_FIELDS + '''
        # 只有 Choice 校验拒绝时填入规范化候选，其余异常保持 None。
        self.choice_candidates: tuple[str, ...] | None = None''',
            ),
        ),
        "types.py": (
            (
                _CHOICE_ERROR,
                '''        except StopIteration:
            try:
                self.fail(
                    self.get_invalid_choice_message(value=value, ctx=ctx),
                    param=param,
                    ctx=ctx,
                )
            except BadParameter as error:
                # 复用本次规范化候选及原有 fail 扩展点，保持错误文本和上下文。
                error.choice_candidates = tuple(normalized_mapping.values())
                raise''',
            ),
        ),
    },
    "cross-click-exit-message": {
        "core.py": (
            (
                '    def exit(self, code: int = 0) -> t.NoReturn:\n        """Exits the application with a given exit code.',
                '    def exit(self, code: int = 0, *, message: str | None = None) -> t.NoReturn:\n        """清理上下文资源并发出退出信号，code 是退出码，message 缺省时保持静默。',
            ),
            ("        raise Exit(code)", "        raise Exit(code, message=message)"),
            (
                "        except Exit as e:\n            if standalone_mode:\n                sys.exit(e.exit_code)",
                "        except Exit as e:\n            if standalone_mode:\n                if e.message is not None:\n                    echo(e.message, err=True)\n                sys.exit(e.exit_code)",
            ),
        ),
        "exceptions.py": (
            (
                _EXIT_DEFINITION,
                '''    :param code: the status code to exit with.
    :param message: 独立运行输出到 stderr 的退出说明，None 表示不输出。
    """

    __slots__ = ("exit_code", "message")

    exit_code: t.Final[int]
    message: t.Final[str | None]

    def __init__(self, code: int = 0, *, message: str | None = None) -> None:
        """保存调用方的退出码与可选说明，由命令入口决定是否输出。"""
        self.exit_code = code
        self.message = message''',
            ),
        ),
    },
    "cross-click-command-suggestion-limit": {
        "core.py": (
            (
                _CONTEXT_SHOW_DEFAULT,
                _CONTEXT_SHOW_DEFAULT.replace(
                    "    ) -> None:",
                    "        command_suggestion_limit: int | None = None,\n    ) -> None:",
                ),
            ),
            (
                _CONTEXT_MAX_WIDTH,
                _CONTEXT_MAX_WIDTH + '''

        # 子命令继承父设定，根命令沿用默认的三个候选。
        if command_suggestion_limit is None:
            command_suggestion_limit = parent.command_suggestion_limit if parent else 3
        if command_suggestion_limit < 0:
            raise ValueError("command_suggestion_limit must be non-negative")
        self.command_suggestion_limit = command_suggestion_limit''',
            ),
        ),
        "exceptions.py": (
            (
                _SUGGESTION_BODY,
                '''        # 零意味着不运行候选计算；直接构造异常仍最多返回三个结果。
        limit = ctx.command_suggestion_limit if ctx is not None else 3
        if possibilities and limit:
            from difflib import get_close_matches

            possibilities_ = get_close_matches(command_name, possibilities, n=limit)
        else:
            possibilities_ = None
        self.possibilities = possibilities_''',
            ),
        ),
    },
}


def apply_reference(package: Path, identifier: str) -> tuple[str, ...]:
    """在 package 指定的隔离 Click 副本中应用 identifier 对应的参考解。

    返回改动文件名。锚点与冻结快照不符时抛出 ValueError。
    """
    filenames = _REFERENCES[identifier]
    for name, replacements in filenames.items():
        path = package / name
        original = path.read_bytes()
        modified = original.decode("utf-8").replace("\r\n", "\n")
        for before, after in replacements:
            if modified.count(before) != 1:
                raise ValueError(f"参考解 {identifier} 在 {name} 的锚点不唯一")
            modified = modified.replace(before, after, 1)
        if b"\r\n" in original:
            modified = modified.replace("\n", "\r\n")
        path.write_bytes(modified.encode("utf-8"))
    return tuple(filenames)
