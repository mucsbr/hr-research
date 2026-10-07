import { useEffect, useRef, useState } from 'react'
import { Alert, App, Button, Input, Modal, Space, Spin } from 'antd'
import { api, post } from './api'
import type { LoginSession } from './api'

export default function LoginSessionView({ session, onClose, onSaved }: {
  session: LoginSession | null; onClose: () => void; onSaved: () => void
}) {
  const { message } = App.useApp()
  const [frame, setFrame] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [current, setCurrent] = useState<LoginSession | null>(session)
  const image = useRef<HTMLImageElement>(null)
  const drag = useRef<{ x: number; y: number } | null>(null)

  useEffect(() => {
    setCurrent(session); setError(''); setText(''); setFrame(null)
    if (!session) return
    let stopped = false, objectUrl = '', timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    async function tick() {
      try {
        const state = await api<LoginSession>('/login-sessions/' + session!.id)
        if (stopped) return
        setCurrent(state)
        if (!['active', 'starting'].includes(state.status)) {
          setError('登录会话已结束，请关闭窗口或重新创建。')
          return
        }
        const response = await fetch('/api/v1/login-sessions/' + session!.id + '/frame', {
          credentials: 'include', cache: 'no-store', signal: controller.signal,
        })
        if (!response.ok) {
          const data = await response.json()
          throw new Error(data.detail || '暂时无法获取登录画面')
        }
        const next = URL.createObjectURL(await response.blob())
        if (stopped) { URL.revokeObjectURL(next); return }
        if (objectUrl) URL.revokeObjectURL(objectUrl)
        objectUrl = next
        setFrame(next)
      } catch (e) {
        if (!stopped) setError((e as Error).message)
      }
      if (!stopped) timer = setTimeout(tick, 1200)
    }
    tick()
    return () => { stopped = true; controller.abort(); clearTimeout(timer); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [session])

  function coordinates(event: React.PointerEvent<HTMLImageElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(1279, (event.clientX - rect.left) / rect.width * 1280)),
      y: Math.max(0, Math.min(819, (event.clientY - rect.top) / rect.height * 820)),
    }
  }
  async function action(data: Record<string, unknown>) {
    if (!session) return
    try { await post('/login-sessions/' + session.id + '/actions', data); setError('') }
    catch (e) { setError((e as Error).message) }
  }
  async function sendText() {
    if (!text) return
    await action({ action: 'type', text })
    setText('')
  }
  async function close() {
    if (!session) return
    try { await post('/login-sessions/' + session.id + '/close') }
    catch (e) { message.warning((e as Error).message) }
    onClose()
  }
  async function save() {
    if (!session) return
    setBusy(true)
    try {
      await post('/login-sessions/' + session.id + '/save')
      message.success('会话已加密保存，首次采集将验证是否可用')
      onSaved(); onClose()
    } catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  return <Modal title="数据源登录会话" open={!!session} width={1060} onCancel={close}
    maskClosable={false} footer={<Space><Button onClick={close}>结束会话</Button>
      <Button type="primary" loading={busy} onClick={save} disabled={current?.status !== 'active'}>保存登录会话</Button></Space>}>
    <p className="subtle">目标：{current?.url || session?.url} · 点击或拖动画面操作，也可以扫码登录。画面和输入不会交给模型。</p>
    {error && <Alert type="warning" message={error} showIcon style={{ marginBottom: 12 }} />}
    <div className="login-frame">
      {frame ? <img ref={image} src={frame} alt="仅当前用户可见的数据源登录画面"
        draggable={false} style={{ width: '100%', display: 'block', touchAction: 'none' }}
        onPointerDown={event => {
          drag.current = coordinates(event)
          event.currentTarget.setPointerCapture(event.pointerId)
        }}
        onPointerUp={event => {
          if (!drag.current) return
          const start = drag.current, end = coordinates(event)
          drag.current = null
          if (Math.abs(end.x - start.x) + Math.abs(end.y - start.y) > 6) {
            action({ action: 'drag', ...start, to_x: end.x, to_y: end.y })
          } else action({ action: 'click', ...end })
        }} /> : <div style={{ padding: 100, textAlign: 'center' }}><Spin tip="正在准备登录画面" /></div>}
    </div>
    <Space wrap style={{ marginTop: 14, marginBottom: 12 }}>
      <Button size="small" onClick={() => action({ action: 'press', key: 'Tab' })}>下一输入框</Button>
      <Button size="small" onClick={() => action({ action: 'press', key: 'Enter' })}>确认 / 回车</Button>
      <Button size="small" onClick={() => action({ action: 'press', key: 'Backspace' })}>退格</Button>
      <Button size="small" onClick={() => action({ action: 'scroll', delta: -500 })}>向上滚动</Button>
      <Button size="small" onClick={() => action({ action: 'scroll', delta: 500 })}>向下滚动</Button>
      <Button size="small" onClick={() => action({ action: 'back' })}>返回</Button>
    </Space>
    <Space.Compact style={{ width: '100%' }}>
      <Input.Password value={text} onChange={event => setText(event.target.value)}
        placeholder="先点击画面中的输入框，再在这里输入文字或密码" autoComplete="off"
        onPressEnter={sendText} maxLength={500} />
      <Button onClick={sendText}>发送到当前输入框</Button>
    </Space.Compact>
  </Modal>
}
