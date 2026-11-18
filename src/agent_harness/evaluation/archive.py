"""跨 Windows/Linux 的候选工作区归档；链接只保存目标字符串，绝不读取目标文件。"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import struct
import tarfile

LX_SYMLINK_TAG = 0xA000001D  # Docker/WSL 创建的 Linux 符号链接，不被 Windows tarfile 识别。


def decode_lx_link(raw: bytes) -> str:
    """校验重解析原始 raw 并返回 UTF-8 链接目标；未知类型、截断及空目标抛 ValueError。"""
    if len(raw) < 12:
        raise ValueError("Linux链接重解析数据不完整")
    tag, length, _ = struct.unpack_from("<IHH", raw)
    version = struct.unpack_from("<I", raw, 8)[0]
    if tag != LX_SYMLINK_TAG or length != len(raw) - 8 or version != 2:
        raise ValueError("Linux链接重解析格式不支持")
    target = raw[12:].decode("utf-8")
    if not target or "\x00" in target:
        raise ValueError("Linux链接目标无效")
    return target


def read_lx_link(path: Path) -> str:
    """用不跟随链接的 Windows 句柄读取 path 的目标字符串；不读目标文件，失败抛 OSError。"""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # OPEN_REPARSE_POINT + BACKUP_SEMANTICS：打开链接本身；absolute 不解析链接目标。
    handle = kernel.CreateFileW(str(path.absolute()), 0, 7, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(16384)
        count = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A8, None, 0, buffer, len(buffer), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return decode_lx_link(buffer.raw[:count.value])
    finally:
        kernel.CloseHandle(handle)


class CandidateArchive(tarfile.TarFile):
    """补足标准库不识别的 LX 链接；普通文件、原生符号链接及归档过滤规则保持原样。"""

    def gettarinfo(self, name=None, arcname=None, fileobj=None):
        """为 name 生成 tar 成员并返回；arcname 默认沿用文件名，fileobj 非空时沿用标准库行为。

        仅当 name 为 Windows LX 链接时保存其链接目标和零内容长度，不读取目标，不改变文件选择。
        """
        member = super().gettarinfo(name, arcname, fileobj)
        if os.name == "nt" and fileobj is None and name is not None:
            status = os.lstat(name)
            if getattr(status, "st_reparse_tag", 0) == LX_SYMLINK_TAG:
                member.type = tarfile.SYMTYPE
                member.linkname = read_lx_link(Path(name))
                member.size = 0
        return member
