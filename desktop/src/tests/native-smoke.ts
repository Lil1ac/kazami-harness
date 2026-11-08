import assert from 'node:assert/strict';
import { app } from 'electron';
import { mkdirSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { NativeService } from '../native';

const directory = path.resolve(__dirname, '../../../.runtime/native-smoke'); // 专属测试目录，不读取或覆盖用户的应用凭据。
mkdirSync(directory, { recursive: true });
app.setPath('userData', directory);

/** 在真实 Electron/Windows 密码服务中保存、重读和清除虚构密钥，返回 Promise<void>。 */
async function check(): Promise<void> {
  const native = new NativeService(directory, /* 密钥测试不创建任何原生窗口。 */ () => undefined);
  try {
    const secret = 'electron-native-smoke-test-secret';
    await native.writeCredential('model', 'deepseek', secret);
    await native.writeCredential('model', 'qwen', 'qwen-smoke-secret');
    assert.equal(await native.readCredential('model', 'deepseek'), secret);
    assert.ok(!(await readFile(path.join(directory, 'model-credentials', 'deepseek.enc'))).includes(secret));
    await native.writeCredential('model', 'deepseek', '');
    assert.equal(await native.readCredential('model', 'deepseek'), '');
    assert.equal(await native.readCredential('model', 'qwen'), 'qwen-smoke-secret');
    await native.writeCredential('model', 'qwen', '');
    const custom = 'custom-smoke-' + 'a'.repeat(64); // 自定义连接版本对应的密钥分区。
    await native.writeCredential('model', custom, 'custom-smoke-secret');
    assert.equal(await native.readCredential('model', custom), 'custom-smoke-secret');
    assert.equal(await native.readCredential('model', 'custom-smoke-' + 'b'.repeat(64)), '');
    await native.writeCredential('model', custom, '');
    await native.writeCredential('mcp', 'docs-a', 'connection-a-secret');
    await native.writeCredential('mcp', 'docs-b', 'connection-b-secret');
    assert.equal(await native.readCredential('mcp', 'docs-a'), 'connection-a-secret');
    assert.equal(await native.readCredential('mcp', 'docs-b'), 'connection-b-secret');
    assert.ok(!(await readFile(path.join(directory, 'mcp-credentials/docs-a.enc'))).includes('connection-a-secret'));
    await native.writeCredential('mcp', 'docs-a', '');
    assert.equal(await native.readCredential('mcp', 'docs-a'), '');
    assert.equal(await native.readCredential('mcp', 'docs-b'), 'connection-b-secret');
    await native.writeCredential('mcp', 'docs-b', '');
    console.log('真实 Electron 系统加密保存、重读、清除通过');
    app.exit(0);
  } catch {
    console.error('真实 Electron 系统加密检查失败');
    app.exit(1);
  }
}

void app.whenReady().then(check);
