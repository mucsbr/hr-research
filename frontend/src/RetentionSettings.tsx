import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Alert, Button, Checkbox, Descriptions, Drawer, Form, Input, Modal, Space, Switch, Table, Tag } from 'antd'
import { api, ApiError } from './api'
import { ModuleError, ModulePanel, stateTag } from './ModuleUI'
import { beijingDateTime } from './date'

const categories = ['raw_documents', 'reports', 'task_logs', 'expired_sessions'] as const
type Category = typeof categories[number]
const names: Record<Category, string> = { raw_documents: '原始网页与资料', reports: '报告正文与文件', task_logs: '任务运行日志', expired_sessions: '过期登录会话' }
interface Policy { category: Category; retention_days: number; enabled: boolean; revision: number; grace_until: string | null; updated_at: string }
interface Policies {
  policies: Policy[]; can_manage: boolean
  limits: { retention_days_min: number; retention_days_max: number; batch_size_max: number; preview_ttl_seconds: number }
  scheduler: { enabled: boolean; batch_size: number; max_seconds: number }; protected: string[]
}
interface Preview {
  id: string; expires_at: string; can_execute: boolean; eligible_count: number; estimated_bytes: number | null
  categories: { category: Category; retention_days: number; cutoff_at: string; grace_until: string | null; state: 'ready' | 'disabled' | 'grace_period'; eligible_count: number; has_more: boolean }[]
  items: { category: Category; target_id: string; task_id?: string; title?: string; url?: string; file_state?: string }[]
  limitations: string[]
}
interface Run {
  id: string; status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed'; trigger: 'manual' | 'scheduled'
  preview_id: string; idempotency_key: string; created_at: string; started_at: string | null; finished_at: string | null
  summary: { selected?: number; cleared?: number; skipped?: number; failed?: number; files_deleted?: number; bytes_deleted?: number }
  items: { category: Category; target_id: string; status: string; reason?: string }[]; error_message?: string | null
}
interface Intent { version: 1; preview_id: string; idempotency_key: string; created_at: string; run_id?: string }
type PolicyForm = Record<Category, { retention_days: string; enabled: boolean }>
const terminal = new Set(['succeeded', 'partial', 'failed'])
const time = (value?: string | null) => beijingDateTime(value)
const count = (value?: number | null) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'
const size = (value?: number | null) => typeof value !== 'number' || !Number.isFinite(value) ? '未知'
  : value >= 1048576 ? `${(value / 1048576).toFixed(2)} MiB` : value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`
function integer(value: unknown, minimum: number, maximum: number) {
  if (typeof value !== 'string' || !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < minimum || Number(value) > maximum) {
    throw new Error(`请输入 ${minimum} 至 ${maximum} 的整数`)
  }
  return Number(value)
}
const isValidation = (error: unknown) => typeof error === 'object' && error !== null && 'errorFields' in error
const asError = (error: unknown) => error instanceof Error ? error : new Error('请求未完成，请检查后重试')

export function ReportRetentionNotice({ clearedAt }: { clearedAt: string }) {
  return <Alert type="warning" showIcon message="正文已按保留策略清理"
    description={`清理时间（北京时间）：${time(clearedAt)}。报告记录与来源链接仍可查看。`} />
}

export function ReportRetentionState({ clearedAt, hasContent }: { clearedAt?: string | null; hasContent: boolean }) {
  return clearedAt ? <Space direction="vertical" size={2}><Tag color="orange">正文已按保留策略清理</Tag>
    <span className="subtle">{time(clearedAt)}（北京时间）</span></Space> : hasContent ? '可查看' : '暂无正文'
}

export default function RetentionSettings() {
  const { message } = App.useApp(), cache = useQueryClient()
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<{ id?: string; username: string; role: string }>('/auth/me') })
  const policies = useQuery({ queryKey: ['retention', 'policies'], queryFn: () => api<Policies>('/retention/policies') })
  const runs = useQuery({ queryKey: ['retention', 'runs'], queryFn: () => api<Run[]>('/retention/runs?limit=20') })
  const [editing, setEditing] = useState(false), [baseline, setBaseline] = useState<Policy[]>([])
  const [policyForm] = Form.useForm<PolicyForm>(), [previewForm] = Form.useForm<{ categories: Category[]; limit: string }>()
  const [preview, setPreview] = useState<Preview | null>(null), [confirming, setConfirming] = useState(false)
  const [detail, setDetail] = useState<Run | null>(null), [intent, setIntent] = useState<Intent | null>(null)
  const [error, setError] = useState<Error | null>(null), [storageError, setStorageError] = useState<string | null>(null)
  const [saveUncertain, setSaveUncertain] = useState(false), [busy, setBusy] = useState<string | null>(null)
  const active = useRef(false), intentRef = useRef<Intent | null>(null)
  const [clock, setClock] = useState(Date.now())
  const storageKey = me.data ? 'hr-retention-intent:' + (me.data.id || me.data.username) : null
  const canManage = me.data?.role === 'admin' && policies.data?.can_manage === true
  const limits = policies.data?.limits
  const expired = !!preview && (!Number.isFinite(Date.parse(preview.expires_at)) || Date.parse(preview.expires_at) <= clock)
  const locked = !!busy || !!intent || !!storageError || saveUncertain

  useEffect(() => {
    if (!preview) return
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [preview])
  useEffect(() => {
    intentRef.current = null; setIntent(null); setStorageError(null)
    if (!storageKey) return
    try {
      const raw = sessionStorage.getItem(storageKey)
      if (!raw) return
      const value: Intent = JSON.parse(raw)
      if (value.version !== 1 || typeof value.preview_id !== 'string' || !value.preview_id || typeof value.idempotency_key !== 'string' || !value.idempotency_key) throw new Error('invalid intent')
      intentRef.current = value; setIntent(value)
    } catch { setStorageError('无法读取本次清理请求标识，执行已暂停；请核对运行记录后处理浏览器存储。') }
  }, [storageKey])

  function store(value: Intent | null) {
    if (!storageKey) throw new Error('尚未读取当前用户，未发送清理请求')
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value))
    else sessionStorage.removeItem(storageKey)
    intentRef.current = value; setIntent(value)
  }
  async function action(name: string, operation: () => Promise<void>) {
    if (active.current) return
    active.current = true; setBusy(name); setError(null)
    try { await operation() } catch (failure) { if (!isValidation(failure)) setError(asError(failure)) }
    finally { active.current = false; setBusy(null) }
  }
  function edit() {
    if (!canManage || !policies.data || locked) return
    setBaseline(policies.data.policies.map(item => ({ ...item })))
    policyForm.resetFields()
    policyForm.setFieldsValue(Object.fromEntries(policies.data.policies.map(item => [item.category, { retention_days: String(item.retention_days), enabled: item.enabled }])) as PolicyForm)
    setEditing(true)
  }
  async function savePolicies(values: PolicyForm) {
    if (!canManage || !limits || intentRef.current || saveUncertain) return
    await action('save', async () => {
      const changed = baseline.map(item => ({ category: item.category,
        retention_days: integer(values[item.category]?.retention_days, limits.retention_days_min, limits.retention_days_max),
        enabled: values[item.category]?.enabled, expected_revision: item.revision }))
        .filter(item => baseline.some(old => old.category === item.category && (old.retention_days !== item.retention_days || old.enabled !== item.enabled)))
      if (changed.some(item => typeof item.enabled !== 'boolean')) throw new Error('启停值无效，未提交设置')
      if (!changed.length) { setEditing(false); message.info('没有变更，未发送设置请求'); return }
      try {
        const next = await api<Policies>('/retention/policies', { method: 'PUT', body: JSON.stringify({ policies: changed }) })
        cache.setQueryData(['retention', 'policies'], next)
        setPreview(null); setEditing(false); message.success('保留策略已更新，请按新的策略重新预览')
      } catch (failure) {
        if (!(failure instanceof ApiError) || failure.status >= 500 || [408, 425, 429].includes(failure.status)) setSaveUncertain(true)
        setPreview(null)
        throw failure
      }
    })
  }
  async function refreshPolicy() {
    await action('refresh', async () => {
      const next = await policies.refetch()
      if (next.error) throw next.error
      if (!next.data) throw new Error('未能核对当前策略')
      setSaveUncertain(false); setEditing(false); setPreview(null)
    })
  }
  async function createPreview() {
    if (!canManage || !limits || intentRef.current || storageError || saveUncertain) return
    await action('preview', async () => {
      const values = await previewForm.validateFields()
      const selected = categories.filter(category => values.categories.includes(category))
      if (!selected.length || selected.length !== values.categories.length) throw new Error('至少选择一个有效分类')
      const limit = integer(values.limit, 1, limits.batch_size_max)
      setPreview(null)
      const next = await api<Preview>('/retention/preview', { method: 'POST', body: JSON.stringify({ categories: selected, limit }) })
      setPreview(next); setClock(Date.now())
    })
  }
  function applyRun(run: Run, pending: Intent) {
    if (run.preview_id !== pending.preview_id || run.idempotency_key !== pending.idempotency_key) throw new Error('服务器记录与本次请求不一致，保留原标识等待核对')
    setDetail(run)
    if (terminal.has(run.status)) store(null)
    else store({ ...pending, run_id: run.id })
    cache.invalidateQueries({ queryKey: ['retention', 'runs'] })
  }
  async function execute() {
    if (!canManage || !preview || expired || !preview.can_execute || preview.eligible_count <= 0 || intentRef.current || storageError) return
    await action('execute', async () => {
      const pending: Intent = { version: 1, preview_id: preview.id, idempotency_key: crypto.randomUUID(), created_at: new Date().toISOString() }
      try { store(pending) } catch { setStorageError('无法保存清理请求标识，未发送请求；请检查浏览器存储。'); return }
      setConfirming(false)
      try {
        const run = await api<Run>('/retention/runs', { method: 'POST', body: JSON.stringify({ preview_id: pending.preview_id, idempotency_key: pending.idempotency_key }), headers: { 'Idempotency-Key': pending.idempotency_key } })
        applyRun(run, pending); setPreview(null)
        message.info(terminal.has(run.status) ? '已读取清理结果，请查看运行记录' : '清理请求已登记，请查询运行结果')
      } catch (failure) {
        if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && ![408, 425, 429].includes(failure.status)) {
          store(null); setPreview(null)
          throw new Error(failure.message + '；未继续执行，请重新预览。')
        }
        throw new Error('执行结果尚未确认，原请求标识已保留。请查询清理结果，不会自动重发。')
      }
    })
  }
  async function lookup() {
    const pending = intentRef.current
    if (!pending) return
    await action('lookup', async () => {
      try { applyRun(await api<Run>('/retention/runs/by-key?idempotency_key=' + encodeURIComponent(pending.idempotency_key)), pending) }
      catch (failure) {
        if (failure instanceof ApiError && failure.status === 404) throw new Error('暂未找到原请求记录，仍保留该标识；请稍后继续查询，不要新建重复请求。')
        throw failure
      }
    })
  }

  return <><div style={{ height: 22 }} /><ModulePanel title="分类数据保留" extra={<Space>
    <Button disabled={!!busy} onClick={refreshPolicy}>刷新保留状态</Button>
    {canManage && <Button onClick={edit} disabled={locked || !policies.data}>修改保留策略</Button>}
  </Space>}>
    <ModuleError error={policies.error || runs.error || error} />
    {!canManage && <p className="subtle">当前为只读查看；仅管理员可调整保留策略、预览和执行清理。</p>}
    {storageError && <Alert type="error" showIcon message={storageError} />}
    {saveUncertain && <Alert type="warning" showIcon message="设置保存结果待核对，暂不重复提交。请刷新保留状态后重新检查。" />}
    {intent && <Alert type="warning" showIcon message={intent.run_id ? '已有清理请求等待核对运行结果' : '清理结果未知，原请求标识已保留'}
      description={<Space wrap><span>不会自动重发或新建清理请求。</span><Button onClick={lookup} loading={busy === 'lookup'} disabled={!!busy && busy !== 'lookup'}>查询清理结果</Button></Space>} />}
    <Table rowKey="category" dataSource={policies.data?.policies} loading={policies.isPending} pagination={false} columns={[
      { title: '分类', dataIndex: 'category', render: (value: Category) => names[value] || value },
      { title: '保留期限', dataIndex: 'retention_days', render: value => `${value} 天` },
      { title: '策略', dataIndex: 'enabled', render: value => <Tag color={value ? 'green' : undefined}>{value ? '已启用' : '已停用'}</Tag> },
      { title: '宽限截至（北京时间）', dataIndex: 'grace_until', render: time },
      { title: '修订', dataIndex: 'revision' }, { title: '更新于（北京时间）', dataIndex: 'updated_at', render: time },
    ]} />
    <p className="subtle">首次启用、缩短期限或重新开启分类时，服务端保留七天宽限；宽限内不会清理。所有计数按本批可处理范围展示。</p>
    {policies.data && <><Descriptions column={2} size="small" items={[
      { label: '小时调度', children: policies.data.scheduler.enabled ? '按已启用策略检查' : '全部策略停用' },
      { label: '调度批次预算', children: `最多 ${policies.data.scheduler.batch_size} 项 / ${policies.data.scheduler.max_seconds} 秒` },
    ]} />{!!policies.data.protected.length && <div className="note-box">{policies.data.protected.map((item, index) => <p key={index}>{item}</p>)}</div>}</>}
    {canManage && limits && <><div style={{ height: 16 }} /><Form form={previewForm} layout="vertical" initialValues={{ categories: [...categories], limit: '100' }}>
      <Form.Item name="categories" label="预览分类" rules={[{ type: 'array', min: 1, required: true, message: '至少选择一个分类' }]}><Checkbox.Group options={categories.map(value => ({ value, label: names[value] }))} disabled={locked} /></Form.Item>
      <Space align="start" wrap><Form.Item name="limit" label={`本批最多项数（1–${limits.batch_size_max}）`} rules={[{ validator: (_, value) => {
        try { integer(value, 1, limits.batch_size_max); return Promise.resolve() } catch (failure) { return Promise.reject(failure) }
      } }]}><Input inputMode="numeric" autoComplete="off" disabled={locked} style={{ width: 150 }} /></Form.Item>
      <Button style={{ marginTop: 30 }} onClick={createPreview} loading={busy === 'preview'} disabled={locked && busy !== 'preview'}>预览本批影响</Button></Space>
    </Form></>}
  </ModulePanel>
    {preview && <><div style={{ height: 22 }} /><ModulePanel title="本批清理预览" extra={canManage && <Button danger type="primary" style={{ paddingInline: 12, color: locked || expired || !preview.can_execute || preview.eligible_count <= 0 ? undefined : '#fff' }} onClick={() => setConfirming(true)} disabled={locked || expired || !preview.can_execute || preview.eligible_count <= 0}>确认执行本批</Button>}>
      <Descriptions column={3} items={[{ label: '本批可清理', children: count(preview.eligible_count) + ' 项' }, { label: '预计影响数据量', children: size(preview.estimated_bytes) }, { label: '预览有效至（北京时间）', children: time(preview.expires_at) }]} />
      {expired && <Alert type="warning" message="此预览已过期，请重新预览后再执行。" />}
      {!preview.can_execute && !expired && <Alert type="info" message="本批暂不能执行，请检查分类启停、宽限和候选数量。" />}
      <Table rowKey="category" dataSource={preview.categories} pagination={false} columns={[
        { title: '分类', dataIndex: 'category', render: (value: Category) => names[value] || value },
        { title: '本批项数', dataIndex: 'eligible_count', render: count },
        { title: '状态', dataIndex: 'state', render: value => <Tag color={value === 'ready' ? 'green' : undefined}>{({ ready: '可处理', disabled: '已停用', grace_period: '宽限中' } as Record<string, string>)[value] || value}</Tag> },
        { title: '截止时间（北京时间）', dataIndex: 'cutoff_at', render: time },
        { title: '还有更多', dataIndex: 'has_more', render: value => value ? '有，未计入本批' : '未发现' },
      ]} />
      {preview.limitations.map((item, index) => <p className="subtle" key={index}>{item}</p>)}
      <Table rowKey={item => item.category + ':' + item.target_id} size="small" dataSource={preview.items} pagination={{ pageSize: 5 }} columns={[
        { title: '分类', dataIndex: 'category', render: (value: Category) => names[value] || value },
        { title: '候选记录', render: (_, item: Preview['items'][number]) => item.title || item.target_id },
        { title: '文件状态', dataIndex: 'file_state', render: value => value || '—' },
      ]} />
    </ModulePanel></>}
    <div style={{ height: 22 }} /><ModulePanel title="保留策略运行记录" extra={<Button onClick={() => runs.refetch()} loading={runs.isFetching}>刷新运行记录</Button>}>
      <Table rowKey="id" dataSource={runs.data} loading={runs.isPending} pagination={{ pageSize: 5 }} columns={[
        { title: '请求时间（北京时间）', dataIndex: 'created_at', render: time },
        { title: '触发', dataIndex: 'trigger', render: value => value === 'scheduled' ? '定时' : '人工确认' },
        { title: '状态', dataIndex: 'status', render: stateTag },
        { title: '清理 / 跳过 / 失败', render: (_, item: Run) => `${count(item.summary.cleared)} / ${count(item.summary.skipped)} / ${count(item.summary.failed)}` },
        { title: '操作', render: (_, item: Run) => <Button size="small" onClick={() => action('detail', async () => setDetail(await api<Run>('/retention/runs/' + item.id)))}>查看清理记录</Button> },
      ]} />
    </ModulePanel>
    <Modal title="配置分类保留期限" open={editing} onCancel={() => setEditing(false)} onOk={() => policyForm.submit()} okText="保存保留策略" confirmLoading={busy === 'save'} okButtonProps={{ disabled: !!busy || saveUncertain || !!intent || !canManage }} width={720}>
      <Form form={policyForm} layout="vertical" onFinish={savePolicies}>
        {categories.map(category => <Space key={category} align="start" size="large" wrap>
          <Form.Item name={[category, 'retention_days']} label={`${names[category]}保留天数`} rules={[{ validator: (_, value) => {
            try { if (!limits) throw new Error('尚未读取期限限制'); integer(value, limits.retention_days_min, limits.retention_days_max); return Promise.resolve() } catch (failure) { return Promise.reject(failure) }
          } }]}><Input inputMode="numeric" autoComplete="off" style={{ width: 225 }} /></Form.Item>
          <Form.Item name={[category, 'enabled']} label="启用此分类" valuePropName="checked"><Switch /></Form.Item>
        </Space>)}
        <p className="subtle">保存后预览失效；首次启用、缩短期限和重新开启均以服务端返回的宽限时间为准。</p>
      </Form>
    </Modal>
    <Modal title="确认清理本批历史资料" open={confirming} onCancel={() => setConfirming(false)} onOk={execute} okText="执行已预览清理" okButtonProps={{ danger: true, disabled: locked || expired || !preview?.can_execute }} confirmLoading={busy === 'execute'}>
      <p>本次最多处理 {count(preview?.eligible_count)} 项，预计影响 {size(preview?.estimated_bytes)}。超出本批的资料不包含在这个计数中。</p>
      <p>清理完成后不能在平台内恢复正文或文件，请核对预览与需要保留的资料。运行结果以服务器记录为准。</p>
    </Modal>
    <Drawer title="数据清理运行详情" open={!!detail} onClose={() => setDetail(null)} width={720}>
      {detail && <>{stateTag(detail.status)}<p className="subtle">请求：{detail.id} · {time(detail.created_at)}（北京时间）</p>
        {detail.error_message && <Alert type="warning" message={detail.error_message} />}
        <Descriptions column={2} items={[{ label: '本批选择', children: count(detail.summary.selected) }, { label: '已清理', children: count(detail.summary.cleared) },
          { label: '跳过', children: count(detail.summary.skipped) }, { label: '失败', children: count(detail.summary.failed) },
          { label: '删除文件', children: count(detail.summary.files_deleted) }, { label: '文件字节', children: size(detail.summary.bytes_deleted) }]} />
        <Table rowKey={item => item.category + ':' + item.target_id} size="small" dataSource={detail.items} pagination={{ pageSize: 10 }} columns={[
          { title: '分类', dataIndex: 'category', render: (value: Category) => names[value] || value }, { title: '记录', dataIndex: 'target_id', ellipsis: true },
          { title: '状态', dataIndex: 'status', render: value => value === 'cleared' ? <Tag color="green">已清理</Tag> : value === 'skipped' ? <Tag>已跳过</Tag> : stateTag(value) }, { title: '原因', dataIndex: 'reason', render: value => value || '—' },
        ]} />
      </>}
    </Drawer>
  </>
}
