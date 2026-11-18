"""统一文件工具与命令工具的工作区路径边界和运行记录保护规则。"""

import os
from pathlib import Path


class WorkspaceError(Exception):
    """仅暴露稳定错误码与受控说明，不携带宿主路径或底层异常正文。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可安全交回模型的错误信息。"""
        super().__init__(message)
        self.code = code
        self.message = message


class Workspace:
    """解析工作区路径，并同时检查词法路径、链接目标和记录目录别名。"""

    def __init__(self, workspace: Path, runs_root: Path) -> None:
        """保留记录目录的原始绝对路径与实际目标，使双向链接别名都受到保护。"""
        try:
            self.workspace = workspace.resolve()
            self._runs_lexical = Path(os.path.abspath(runs_root))
            self.runs_root = runs_root.resolve()
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("invalid_path", "工作区路径无法解析，请检查路径格式。") from None

    def _is_record_path(self, path: Path) -> bool:
        """判断路径是否落入记录目录的词法位置或其解析后的真实位置。"""
        return path.is_relative_to(self._runs_lexical) or path.is_relative_to(self.runs_root)

    def check_scanned_path(self, path: Path, *, directory: bool = False, resolve: bool = True) -> None:
        """核验受控目录枚举产生的绝对 path，变化或越权时抛 WorkspaceError，成功返回 None。

        directory=True 允许访问授权目录的祖先以继续枚举。resolve=False 仅核对词法
        路径与授权，供文件打开前使用；读句柄前必须再解析并核对同一文件身份。搜索起点
        仍使用完整 resolve，搜索逐层跳过枚举到的符号链接及目录联接。
        """
        if not path.is_absolute() or not path.is_relative_to(self.workspace) or ".." in path.parts:
            raise WorkspaceError("path_outside_workspace", "只能访问当前工作区内的路径。")
        if self._is_record_path(path):
            raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
        if os.name == "nt" and (":" in path.name or path.name.endswith((".", " ")) or Path(path.name).is_reserved()):
            raise WorkspaceError("invalid_path", "文件路径无法解析，请检查路径格式。")
        from agent_harness.permissions.policy import FILE_SCOPE, path_allowed
        scope = FILE_SCOPE.get()
        if scope is not None and scope[0] == self.workspace and not path_allowed(path, scope[1], directory=directory):
            raise WorkspaceError("permission_path_denied", "路径不属于当前授权目录范围。")
        if not resolve:
            return
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("path_changed", "搜索期间文件路径发生变化。") from None
        if self._is_record_path(resolved):
            raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
        if resolved != path:
            raise WorkspaceError("path_changed", "搜索期间文件路径发生变化。")

    def resolve(self, requested: str, *, must_exist: bool = False) -> Path:
        """解析绝对或相对路径，拒绝越界和经过记录目录后再转出的路径。

        检查逐级前缀而不只检查最终 resolve 结果，防止记录目录中的链接指向公开文件，
        以及通过普通别名进入记录目录后再用父级组件返回公开区域。
        """
        if not isinstance(requested, str) or not requested.strip() or "\x00" in requested:
            raise WorkspaceError("invalid_path", "文件路径无法解析，请检查路径格式。")
        try:
            path = Path(requested)
            # Windows 的设备名、流名称和尾部自动归一化不能绕过普通文件边界检查。
            if os.name == "nt":
                for component in path.parts:
                    if component == path.anchor or component in {".", ".."}:
                        continue
                    if ":" in component or component.endswith((".", " ")) or Path(component).is_reserved():
                        raise WorkspaceError("invalid_path", "文件路径无法解析，请检查路径格式。")
            if not path.is_absolute():
                path = self.workspace / path
            prefix = Path(path.anchor)
            for component in path.parts[1:]:
                prefix = prefix / component
                if self._is_record_path(Path(os.path.abspath(prefix))):
                    raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
                target = prefix.resolve(strict=False)
                if self._is_record_path(target):
                    raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
                # 只检查工作区内的链接，绝对路径的宿主祖先不属于工具的可访问范围。
                is_link = prefix.is_symlink() or prefix.is_junction()
                if is_link and prefix.is_relative_to(self.workspace) and not target.is_relative_to(self.workspace):
                    raise WorkspaceError("path_outside_workspace", "只能访问当前工作区内的路径。")
            resolved = path.resolve(strict=False)
            if self._is_record_path(resolved):
                raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
            if not resolved.is_relative_to(self.workspace):
                raise WorkspaceError("path_outside_workspace", "只能访问当前工作区内的路径。")
            if must_exist:
                # 先判定边界再检查存在性，越界的缺失路径也应返回越界错误。
                resolved = path.resolve(strict=True)
                if self._is_record_path(resolved):
                    raise WorkspaceError("run_records_forbidden", "工具不能访问运行记录目录。")
                if not resolved.is_relative_to(self.workspace):
                    raise WorkspaceError("path_outside_workspace", "只能访问当前工作区内的路径。")
            from agent_harness.permissions.policy import FILE_SCOPE, path_allowed
            scope = FILE_SCOPE.get()
            if scope is not None and scope[0] == self.workspace and not path_allowed(resolved, scope[1], directory=resolved.is_dir()):
                raise WorkspaceError("permission_path_denied", "路径不属于当前授权目录范围。")
            return resolved
        except WorkspaceError:
            raise
        except FileNotFoundError:
            raise WorkspaceError("file_not_found", "文件不存在，请检查工作区内的路径。") from None
        except PermissionError:
            raise WorkspaceError("permission_denied", "没有访问此路径的权限。") from None
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceError("invalid_path", "文件路径无法解析，请检查路径格式。") from None
