import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { downloadFile } from "../files";
import { nativeRequest, textParameter } from "../protocol";
import { externalUrl } from "../native";
import {
  loadWindowState,
  saveWindowState,
  visibleWindow,
} from "../window-state";

test("下载不覆盖已有同名文件，拒绝相对宿主路径", /* 使用真实临时文件验证用户已有下载不受影响。 */ async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "harness-download-"));
  try {
    const source = path.join(root, "result.txt");
    await writeFile(source, "new result");
    const directory = path.join(root, "downloads");
    const first = await downloadFile(source, directory);
    const second = await downloadFile(source, directory);
    assert.equal(first.filename, "result.txt");
    assert.equal(second.filename, "result (1).txt");
    assert.equal(await readFile(first.path, "utf8"), "new result");
    await assert.rejects(downloadFile("relative.txt", directory));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("窗口跨启动保存，多显示器变化后仍能看到标题栏", /* 分别验证正常副屏坐标和已拔除副屏的恢复边界。 */ async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "harness-window-"));
  try {
    const state = {
      bounds: { x: -1400, y: 100, width: 1000, height: 700 },
      maximized: true,
    };
    const file = path.join(root, "window.json");
    await saveWindowState(file, state);
    const loaded = await loadWindowState(file);
    assert.deepEqual(loaded, state);
    assert.equal(
      visibleWindow(loaded, [
        { x: 0, y: 0, width: 1280, height: 720 },
        { x: -1600, y: 0, width: 1600, height: 900 },
      ]).bounds.x,
      -1400,
    );
    const restored = visibleWindow(loaded, [
      { x: 0, y: 0, width: 1280, height: 720 },
    ]);
    assert.equal(restored.bounds.x, 140);
    assert.equal(restored.bounds.y, 10);
    assert.equal(restored.maximized, true);
    await writeFile(file, '{"bounds":{"width":"bad"}}');
    await assert.rejects(loadWindowState(file));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("原生协议与外链只接受固定能力和安全协议", /* 任意命令、脚本链接和带凭据 URL 必须在原生入口拒绝。 */ () => {
  assert.throws(() =>
    nativeRequest({
      type: "native",
      id: "a".repeat(32),
      method: "execute",
      parameters: {},
    }),
  );
  assert.equal(
    nativeRequest({
      type: "native",
      id: "a".repeat(32),
      method: "folder.choose",
      parameters: {},
    }).method,
    "folder.choose",
  );
  assert.throws(() => textParameter({ secret: 123 }, "secret"));
  for (const url of [
    "file:///C:/private",
    "javascript:alert(1)",
    "https://user:secret@example.com",
    "https://example.com/\n",
  ])
    assert.throws(() => externalUrl(url));
  assert.equal(
    externalUrl("https://example.com/docs"),
    "https://example.com/docs",
  );
});
