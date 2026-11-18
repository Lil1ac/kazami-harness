"""支持通过 python -m agent_harness 启动命令行程序。"""

import sys

from agent_harness.execution.search_worker import WORKER_FLAG, worker_main

if __name__ == "__main__":
    if sys.argv[1:] == [WORKER_FLAG]:
        raise SystemExit(worker_main())
    from agent_harness.cli import main

    raise SystemExit(main())
