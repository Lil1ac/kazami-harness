import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Input, Space } from 'antd';
import { errorMessage, saveDesktopCredential } from '../api';
import type { OptionsResponse } from '../types';

/** 输入并安全保存本机模型密钥；configured 只表示是否已配置，不回读正文。 */
export function DesktopCredentials({ configured }: { configured: boolean }) {
  const cache = useQueryClient();
  const [secret, setSecret] = useState(''); // 仅输入期间保留在组件内存，保存或清除后立即置空。
  const [saving, setSaving] = useState(false); // 防止重复写入凭据库。
  const [error, setError] = useState<string | null>(null); // 后端受控失败说明，不含输入值。

  /** 保存 value 或以空字符串明确清除；成功只更新存在标记，不把输入写入查询缓存。 */
  async function save(value: string): Promise<void> {
    setSaving(true);
    setError(null);
    try {
      const result = await saveDesktopCredential(value);
      cache.setQueryData<OptionsResponse>(
        ['options'],
        /* 只合并存在状态，保持配置及活动任务不变。 */ (current) =>
          current ? { ...current, ...result } : current,
      );
      setSecret('');
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section>
      <h2>模型密钥</h2>
      <Alert
        type={configured ? 'success' : 'warning'}
        showIcon
        title={configured ? '已加密保存到本机' : '请配置 DeepSeek API Key'}
      />
      <Input.Password
        aria-label="DeepSeek API Key"
        placeholder={configured ? '输入新密钥以替换' : 'DeepSeek API Key'}
        value={secret}
        autoComplete="new-password"
        maxLength={2560}
        disabled={saving}
        onChange={
          /* 只更新当前组件内存，不持久化到浏览器存储。 */ (event) => setSecret(event.target.value)
        }
        style={{ marginTop: 16, marginBottom: 12 }}
      />
      <Space>
        <Button
          type="primary"
          disabled={!secret.trim()}
          loading={saving}
          onClick={/* 用户明确点击后才写入系统凭据库。 */ () => void save(secret)}
        >
          保存密钥
        </Button>
        {configured && (
          <Button
            disabled={saving}
            onClick={/* 清除只影响后续任务，已执行调用不会被撤销。 */ () => void save('')}
          >
            清除密钥
          </Button>
        )}
      </Space>
      {error && <Alert type="error" title={error} style={{ marginTop: 12 }} />}
    </section>
  );
}
