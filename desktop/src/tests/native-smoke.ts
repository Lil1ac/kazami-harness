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
    await native.writeCredential(secret);
    assert.equal(await native.readCredential(), secret);
    assert.ok(!(await readFile(path.join(directory, 'credential.enc'))).includes(secret));
    await native.writeCredential('');
    assert.equal(await native.readCredential(), '');
    console.log('真实 Electron 系统加密保存、重读、清除通过');
    app.exit(0);
  } catch {
    console.error('真实 Electron 系统加密检查失败');
    app.exit(1);
  }
}

void app.whenReady().then(check);
