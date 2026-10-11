import { Alert, Tag } from 'antd'
import { beijingDateTime } from './date'
import type { ReactNode } from 'react'

export function ModuleTitle({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <div className="page-title"><div><div className="eyebrow">COMPANY INTELLIGENCE</div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}
export function ModulePanel({ title, children, extra }: { title: string; children: ReactNode; extra?: ReactNode }) {
  return <section className="panel"><div className="panel-header"><h2>{title}</h2>{extra}</div>{children}</section>
}
export function ModuleError({ error }: { error: Error | null | undefined }) {
  return error ? <Alert showIcon type="error" message={error.message} className="load-error" /> : null
}
export const dateText = (value?: string | null) => beijingDateTime(value)
const stateNames: Record<string, string> = {
  queued: '排队中', running: '运行中', sending: '发送中', retrying: '等待重试', sent: '已发送',
  unknown: '结果未知', failed: '失败', cancelled: '已取消', succeeded: '完成', partial: '部分完成',
  waiting_login: '需要登录', waiting_verification: '需要人工验证', waiting_account: '等待账号空闲', waiting_device: '等待采集设备',
  blocked_configuration: '需要配置', ready: '可用', configured: '已配置',
  negative: '负面线索', positive: '正面线索', neutral: '中性', mixed: '混合',
  negative_clue: '负面线索', positive_clue: '正面线索', mixed_clue: '混合线索',
  unclassified: '待归类', not_collected: '尚未采集',
  needs_engineering: '需工程处理', awaiting_publish: '已验证 · 待发布', recovery_queued: '恢复采集排队',
  recovering: '恢复采集中', recovered: '已恢复',
  not_configured: '未配置', invalid_configuration: '配置无效', version_mismatch: '版本不一致',
  license_pending: '环境已配置 · 授权待确认', configured_unverified: '配置已核验 · 运行待验证',
}
export const stateTag = (value: string) => <Tag color={['sent', 'succeeded', 'ready'].includes(value) ? 'green' :
  ['failed', 'negative'].includes(value) ? 'red' : ['partial', 'unknown', 'waiting_login'].includes(value) ? 'orange' : undefined}>{stateNames[value] || value}</Tag>
