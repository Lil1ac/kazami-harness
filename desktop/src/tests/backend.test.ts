import assert from "node:assert/strict";
import { randomBytes, randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { BackendProcess } from "../backend";
import { downloadFile } from "../files";

const repository = path.resolve(__dirname, "../../.."); // 测试运行于 desktop/dist/tests，固定源码根目录。

/** 在真实后台提交 path/body 并读取 JSON；只用于测试，令牌不进入浏览器或磁盘。 */
async function api(
  backend: BackendProcess,
  token: string,
  route: string,
  body?: object,
  method = "POST",
): Promise<any> {
  const response = await fetch(backend.origin + route, {
    method: body ? method : "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(10000),
  });
  assert.ok(response.ok, await response.clone().text());
  return await response.json();
}

/** 最多等待20秒直到 identifier 的真实 Runner 收尾，返回最终视图；不会重放任务。 */
async function finished(
  backend: BackendProcess,
  token: string,
  identifier: string,
): Promise<any> {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const view = await api(backend, token, `/api/runs/${identifier}`);
    if (!view.active) return view;
    await new Promise(
      /* 轮询只等待本地任务落盘，不调用模型或原生能力。 */ (resolve) =>
        setTimeout(resolve, 25),
    );
  }
  throw new Error("后台任务没有按时收尾");
}

/** 创建使用真实 Python/HTTP/标准流的离线后台；root 隔离设置、项目和运行记录。 */
function makeBackend(
  root: string,
  token: string,
  extraArgs: string[] = [],
): BackendProcess {
  return new BackendProcess({
    executable: path.join(repository, ".venv/Scripts/python.exe"),
    args: [
      "-X",
      "utf8",
      "-m",
      "tests.helpers.desktop_fixture",
      "--data-dir",
      root,
      "--runs-dir",
      path.join(root, "runs"),
      ...extraArgs,
    ],
    cwd: repository,
    token,
    apiKey: "node-desktop-test-secret",
    logFile: path.join(root, "backend.log"),
    native: /* 原生选择替身只返回专属项目，下载使用主进程真实实现。 */ async (
      request,
    ) => {
      if (request.method === "folder.choose") return path.join(root, "project");
      if (request.method === "file.download")
        return await downloadFile(
          request.parameters.source as string,
          request.parameters.directory as string,
        );
      return null;
    },
    onUnexpectedExit: /* 任何非预期后台退出都使本测试失败。 */ () => {
      throw new Error("测试后台意外退出");
    },
  });
}

test(
  "桌面父子进程贯通规划与动作批准、真实工具及检查、下载和重启历史",
  { skip: process.platform !== "win32", timeout: 45000 },
  /* 验证生产宿主协议，只有模型和系统目录选择返回使用离线替身。 */ async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "harness-backend-"));
    let backend = makeBackend(root, "a".repeat(64));
    let token = "a".repeat(64);
    try {
      await backend.start();
      assert.equal(
        (
          await fetch(backend.origin + "/api/options", {
            signal: AbortSignal.timeout(10000),
          })
        ).status,
        403,
      );
      const selection = (await api(backend, token, "/api/projects/pick", {}))
        .selection;
      const project = await api(backend, token, "/api/projects", {
        selection_id: selection.selection_id,
        label: "桌面项目",
      });
      await api(
        backend,
        token,
        `/api/projects/${project.id}/settings`,
        {
          test_command:
            "python -c \"from pathlib import Path; assert Path('result.txt').is_file()\"",
          test_timeout_seconds: 10,
          python_executable: path.join(repository, ".venv/Scripts/python.exe"),
        },
        "PUT",
      );
      const options = await backend.read<any>("/api/options");
      const settings = {
        requested_model: options.config.requested_model,
        tools: options.config.tools,
        limits: {
          max_model_turns: 20,
          max_tool_calls: 40,
          run_timeout_seconds: 120,
        },
      };
      await api(
        backend,
        token,
        "/api/desktop/settings",
        { config: settings, downloads_directory: path.join(root, "downloads") },
        "PUT",
      );
      const created = await api(backend, token, "/api/jobs", {
        request_id: randomUUID(),
        create: {
          project_id: project.id,
          prompt: "先规划再交付",
          mode: "plan",
        },
      });
      const planned = await finished(backend, token, created.run_id);
      assert.equal(planned.result.execution_status, "waiting_input");
      await assert.rejects(readFile(path.join(root, "project/result.txt")));
      const pending = planned.result.planning.pending;
      const approvalRequest = randomUUID(); // 作业和续轮共用同一幂等编号。
      const approved = await api(backend, token, "/api/jobs", {
        request_id: approvalRequest,
        parent_run_id: created.run_id,
        followup: {
          request_id: approvalRequest,
          prompt: "批准并执行",
          decision: {
            interaction_id: pending.id,
            action: "approve",
            plan_version: pending.plan_version,
          },
        },
      });
      let result = await finished(backend, token, approved.run_id);
      // 计划批准不预授权本机命令和宿主验收；两条原动作分别通过正式作业接口批准。
      for (const name of ["run_command", "verification"]) {
        assert.equal(result.result.execution_status, "waiting_input");
        const action = result.result.permissions.pending;
        assert.equal(action.name, name);
        const requestId = randomUUID();
        const next = await api(backend, token, "/api/jobs", {
          request_id: requestId,
          parent_run_id: result.run_id,
          followup: {
            request_id: requestId,
            prompt: "批准本次动作",
            action_decision: {
              action_id: action.id,
              fingerprint: action.fingerprint,
              action: "approve",
            },
          },
        });
        result = await finished(backend, token, next.run_id);
      }
      assert.equal(result.result.execution_status, "completed");
      assert.equal(result.result.validation_status, "passed");
      assert.equal(result.result.model_turns, 0); // 最终检查直接执行保存入口，不重新请求模型。
      assert.equal(
        await readFile(path.join(root, "project/result.txt"), "utf8"),
        "desktop acceptance passed\n",
      );
      const artifact = `/api/runs/${approved.run_id}/artifacts?location=workspace&path=result.txt`;
      for (const name of ["result.txt", "result (1).txt"]) {
        const saved = await api(backend, token, "/api/desktop/download", {
          url: artifact,
        });
        assert.equal(saved.filename, name);
        assert.equal(
          await readFile(saved.path, "utf8"),
          "desktop acceptance passed\n",
        );
      }
      assert.ok(
        !(await readFile(path.join(root, "settings.json"), "utf8")).includes(
          "node-desktop-test-secret",
        ),
      );
      await backend.stop();
      token = randomBytes(32).toString("hex");
      backend = makeBackend(root, token);
      await backend.start();
      const history = await api(
        backend,
        token,
        `/api/runs/${approved.run_id}/conversation`,
      );
      assert.equal(history.turns.length, 4);
      assert.equal(history.turns[3].validation_status, "passed");
    } finally {
      await backend.stop();
      await rm(root, { recursive: true, force: true });
    }
  },
);

test(
  "错误配置结束后台并返回固定启动错误",
  { skip: process.platform !== "win32", timeout: 20000 },
  /* 验证无法就绪时不遗留半启动服务，不暴露启动密钥。 */ async () => {
    const root = await mkdtemp(
      path.join(os.tmpdir(), "harness-backend-error-"),
    );
    const backend = makeBackend(root, "b".repeat(64), [
      "--config",
      path.join(root, "missing.toml"),
    ]);
    try {
      await assert.rejects(backend.start(), /后台启动失败/);
    } finally {
      await backend.stop();
      await rm(root, { recursive: true, force: true });
    }
  },
);
