"""Electron 管理的后台服务打包入口；标准流仅承担受控宿主协议。"""

import sys

from agent_harness.execution.search_worker import WORKER_FLAG, worker_main


if __name__ == "__main__":
    if sys.argv[1:] == [WORKER_FLAG]:
        raise SystemExit(worker_main())
    from agent_harness.desktop.server import main

    raise SystemExit(main())
