"""启动前读取配置与宿主验收清单；本模块不创建运行记录或执行命令。"""

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_harness.schemas import RunConfig, RunLimits, TaskSpec, VerifierSpec


class ConfigurationError(Exception):
    """可以直接显示给用户的启动配置错误，消息中不包含输入值。"""


_FIELD_NAMES = (
    set(RunConfig.model_fields)
    | set(RunLimits.model_fields)
    | set(TaskSpec.model_fields)
    | set(VerifierSpec.model_fields)
)


def _validation_message(label: str, error: ValidationError) -> str:
    """将首个校验错误转为中文提示，只保留受控字段位置和类别，不回显输入值。"""
    # Pydantic 默认错误包含 input；只使用受控字段名和错误类别，防止密钥泄漏。
    first = error.errors(include_input=False, include_context=False, include_url=False)[0]
    location = ".".join(
        str(part) if isinstance(part, int) or part in _FIELD_NAMES else "未知字段"
        for part in first["loc"]
    )
    problem = {
        "extra_forbidden": "包含不支持的字段",
        "missing": "缺少必填字段",
        "literal_error": "不在支持范围内",
        "greater_than": "必须大于零",
        "string_too_short": "不能为空",
        "too_short": "不能为空",
        "value_error": "内容不符合约束，请检查空白内容或重复工具",
    }.get(first["type"], "类型或取值不正确")
    return f"{label}无效：{location or '配置内容'}{problem}。"


def _read_toml(path: Path, label: str) -> dict[str, Any]:
    """读取显式指定的 TOML 文件，将语法、编码和文件错误转换为安全提示。"""
    if not isinstance(path, Path):
        raise ConfigurationError(f"{label}路径类型不正确。")
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except tomllib.TOMLDecodeError:
        # 解析器原文可能引用整行配置，不能直接透传到 CLI 或日志。
        raise ConfigurationError(f"{label}不是有效的 TOML，请检查语法。") from None
    except (OSError, UnicodeError, ValueError):
        raise ConfigurationError(f"无法读取{label}，请确认文件存在、编码正确且可读。") from None


def load_config(path: Path | None = None, overrides: dict | None = None) -> RunConfig:
    """按默认值、显式 TOML、CLI 覆盖的顺序合并并严格校验运行配置。

    limits 按字段合并，列表等其他值整体替换；不会修改调用方的覆盖字典。
    不读取环境变量值或 .env，返回的配置只保存密钥来源的固定变量名。
    """
    if overrides is not None and not isinstance(overrides, dict):
        raise ConfigurationError("配置覆盖参数必须是字典。")
    merged = RunConfig().model_dump(mode="python")
    sources = [_read_toml(path, "运行配置文件")] if path is not None else []
    if overrides is not None:
        sources.append(overrides)
    for source in sources:
        for key, value in source.items():
            # 只对 limits 深合并；工具数组等值应由高优先级来源整体替换。
            if key == "limits" and isinstance(value, dict) and isinstance(merged[key], dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    try:
        return RunConfig.model_validate(merged)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("运行配置", error)) from None


def _resolve_directory(path: Path, label: str) -> Path:
    """要求路径指向已存在且可访问的目录，返回解析符号链接后的绝对路径。"""
    if not isinstance(path, Path):
        raise ConfigurationError(f"{label}路径类型不正确。")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ConfigurationError(f"{label}必须是已存在的目录。")
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError(f"{label}必须是已存在且可访问的目录。") from None


def _prepare_verifier(path: Path, timeout_seconds: float) -> VerifierSpec:
    """读取宿主验收清单，解析工作目录和程序入口，但不执行程序。

    相对 cwd 和程序路径以清单目录为基准；裸命令名也可通过 PATH 查找。
    清单未指定超时时使用传入默认值，返回的 argv[0] 固定为绝对入口路径。
    """
    raw = _read_toml(path, "验收配置文件")
    try:
        base = path.resolve(strict=True).parent
        cwd = raw.get("cwd", ".")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ConfigurationError("验收配置无效：cwd 必须是非空路径字符串。")
        # TOML 不支持 Path，解析字符串是输入边界的显式转换，不放宽模型类型。
        raw["cwd"] = _resolve_directory(base / cwd, "验收工作目录")
        raw.setdefault("timeout_seconds", timeout_seconds)
        # 宿主清单的 Python 源码目录相对验收 cwd 解释，不接受跨工作区导入。
        imports = raw.get("python_paths", [])
        if not isinstance(imports, list) or any(not isinstance(item, str) or not item.strip() for item in imports):
            raise ConfigurationError("验收 Python 搜索目录必须是路径字符串数组。")
        raw["python_paths"] = [(raw["cwd"] / item).resolve() for item in imports]
        spec = VerifierSpec.model_validate(raw)
    except ValidationError as error:
        raise ConfigurationError(_validation_message("验收配置", error)) from None
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("验收配置路径无法解析。") from None

    program = spec.argv[0]
    try:
        local = base / program
        is_path = Path(program).is_absolute() or "/" in program or "\\" in program
        if is_path or local.exists():
            executable = local.resolve(strict=True)
            if not executable.is_file() or (os.name != "nt" and not os.access(executable, os.X_OK)):
                raise ConfigurationError("验收入口必须是可执行文件。")
        else:
            found = shutil.which(program)
            if found is None:
                raise ConfigurationError("未找到验收入口，请使用有效路径或 PATH 中的命令。")
            executable = Path(found).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise ConfigurationError("验收入口不存在或无法解析。") from None

    if os.name == "nt" and executable.suffix.lower() not in {".exe", ".com"}:
        raise ConfigurationError("Windows 验收脚本需通过可执行解释器启动，入口应为 .exe 或 .com。")

    # 固定启动前找到的入口，避免运行时工作目录变化导致调用其他同名程序。
    return spec.model_copy(update={"argv": [str(executable), *spec.argv[1:]]})


def prepare_task(
    prompt: str,
    workspace: Path,
    verifier_path: Path | None = None,
    verifier_timeout: float = 30.0,
    expected_outputs: list[str] | None = None,
) -> TaskSpec:
    """将任务文本、已存在的工作目录和可选验收清单校验为 TaskSpec。

    工作目录会规范化为绝对路径；本函数不创建运行记录，也不要求 API 密钥。
    """
    resolved_workspace = _resolve_directory(workspace, "任务工作目录")
    verifier = (
        _prepare_verifier(verifier_path, verifier_timeout) if verifier_path is not None else None
    )
    try:
        return TaskSpec(prompt=prompt, workspace=resolved_workspace, verifier=verifier, expected_outputs=expected_outputs or [])
    except ValidationError as error:
        raise ConfigurationError(_validation_message("任务参数", error)) from None
