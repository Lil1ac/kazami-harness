"""为验证既有执行机制的测试显式提供宿主预授权，不改变生产默认权限。"""

from agent_harness.permissions.models import PermissionPolicy
from agent_harness.schemas import RunConfig


def trusted_config(**overrides) -> RunConfig:
    """返回明确允许本机命令和验收的测试配置；overrides覆盖其他字段或完整宿主策略。

    仅供已有进程、修复及交付机制的测试使用；权限场景应直接构造RunConfig验证真实默认。
    本函数不写文件、不启动进程，不修改调用方传入对象。
    """
    return RunConfig(**{"permission_policy": PermissionPolicy(command="allow", verification="allow"), **overrides})
