import type { JobView } from './types';

/** 判断 status 是否仍在执行或等待处理，供队列分组和侧栏数量共用。 */
export function isPendingJob(status: JobView['status']): boolean {
  return status !== 'completed' && status !== 'failed' && status !== 'cancelled';
}
