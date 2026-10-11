import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Modal, Space, Steps } from 'antd'
import { api, post } from './api'

export interface ProbeLogin { session_id: string; command_id: string; mode: string; expires_at: string }
interface Command { status: string; error_code: string | null; operation: string }
const errors: Record<string, string> = { chrome_missing: '设备没有安装Google Chrome，请在设备端安装后重试', chrome_start_failed: '设备的Chrome未能启动，请查看设备端提示', waiting_device: '设备离线或暂停，请先恢复探针连接', device_paused: '探针已暂停，请在设备端恢复', execution_unknown: '操作结果未知，请先在设备端查看Chrome，避免重复操作', command_expired: '设备响应超时，请查看探针连接状态' }

export default function ProbeLoginView({ session, onClose }: { session: ProbeLogin | null; onClose: () => void }) {
  const { message } = App.useApp(), cache = useQueryClient()
  const [confirmation, setConfirmation] = useState<string | null>(null), [busy, setBusy] = useState(false)
  const command = useQuery({ queryKey: ['probe-login-command', confirmation || session?.command_id],
    queryFn: () => api<Command>('/probes/commands/' + (confirmation || session!.command_id)), enabled: !!session,
    refetchInterval: q => ['pending', 'sent'].includes(q.state.data?.status || 'pending') ? 1200 : false })
  const saved = !!confirmation && command.data?.status === 'succeeded'
  useEffect(() => { if (saved) cache.invalidateQueries({ queryKey: ['source-accounts'] }) }, [saved, cache])
  useEffect(() => { setConfirmation(null) }, [session?.session_id])
  async function confirm() {
    if (!session) return
    setBusy(true)
    try { setConfirmation((await post<{ command_id: string }>('/probes/sessions/' + session.session_id + '/confirm-login')).command_id) }
    catch (e) { message.error((e as Error).message) } finally { setBusy(false) }
  }
  async function close() {
    if (session && !saved) {
      try { await post('/probes/sessions/' + session.session_id + '/close') }
      catch (e) { message.warning((e as Error).message) }
    }
    setConfirmation(null); onClose()
  }
  return <Modal title="在采集设备的Chrome中登录" open={!!session} onCancel={close} footer={<Space>
    <Button onClick={close}>关闭引导</Button>
    <Button type="primary" loading={busy} disabled={command.data?.status !== 'succeeded' || saved} onClick={confirm}>我已在Chrome完成登录</Button>
  </Space>}>
    <Steps direction="vertical" current={saved ? 3 : command.data?.status === 'succeeded' ? 1 : 0} items={[
      { title: '探针在设备端打开Chrome' }, { title: '在真实Chrome中扫码或处理验证码' },
      { title: '确认完成，保留本机登录记录' },
    ]} />
    <p>此处不再转发登录截图。请到该账号绑定设备上的Chrome窗口操作，或使用探针自己的登录引导。</p>
    {command.error && <Alert type="error" message={command.error.message} />}
    {command.data?.error_code && <Alert type="warning" message={errors[command.data.error_code] || '设备操作未完成，请查看探针和Chrome'} />}
    {saved && <Alert type="success" message="本机登录记录已确认" description="会话保留在设备上，首次有限采集仍会核对来源是否可读。" />}
  </Modal>
}
