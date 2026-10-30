"""Electron 管理的后台服务打包入口；标准流仅承担受控宿主协议。"""

from agent_harness.desktop.server import main


if __name__ == "__main__":
    raise SystemExit(main())
