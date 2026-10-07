import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Alert, Button, Descriptions, Drawer, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag } from 'antd'
import { api, post } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText, stateTag } from './ModuleUI'
import { beijingDateTime as beijing } from './date'

interface Channel { id: string; name: string; kind: string; destination_hint: string; enabled: boolean }
interface NewsFilters { regions: string[]; industries: string[]; topics: string[] }
interface Rule { id: string; name: string; channel_id: string; event_types: string[]; subject_ids: string[]; keywords: string[]; news_filters: NewsFilters; cooldown_seconds: number; enabled: boolean; delivery_mode: 'immediate' | 'daily_digest'; digest_time: string; timezone: 'Asia/Shanghai' }
interface PolicyReminder { id: string; item_id: string; title: string; url: string; effective_date: string; scheduled_for: string; status: string; error_code: string | null; event_id: string | null }
interface Digest { rule_name: string; window_start: string; window_end: string; scheduled_for: string; item_count: number; total_items: number; range_start: number; range_end: number; items?: { event_id: string; title: string; summary: string; url: string; event_type: string; observed_at: string; included_at: string }[] }
interface Delivery { id: string; title: string; channel_name: string; event_type: string; status: string; attempts: number; error_code: string | null; created_at: string; sent_at: string | null; text?: string; url?: string; digest: Digest | null }
interface PendingDigest { rule_id: string; rule_name: string; channel_name: string; window_start: string; window_end: string; scheduled_for: string; event_count: number }
const names: Record<string, string> = {
  email: '邮件', webhook: '通用 Webhook', feishu: '飞书机器人',
  'recruitment.first_seen': '岗位首次发现', 'recruitment.content_changed': '岗位内容变化',
  'recruitment.salary_changed': '薪资变化', 'recruitment.suspected_removed': '岗位疑似下架',
  'recruitment.reappeared': '岗位重新出现', 'source.attention': '来源需要处理',
  'sentiment.negative': '负面舆情线索', 'sentiment.new': '新增舆情',
  'news.new': '新增行业资讯', 'news.policy': '政策资讯', 'news.policy_effective': '政策生效日提醒', 'report.ready': '报告生成',
}
const emptyNewsFilters: NewsFilters = { regions: [], industries: [], topics: [] }

export default function NotificationsPage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const channels = useQuery({ queryKey: ['notification-channels'], queryFn: () => api<Channel[]>('/notification-channels') })
  const rules = useQuery({ queryKey: ['notification-rules'], queryFn: () => api<Rule[]>('/notification-rules') })
  const deliveries = useQuery({ queryKey: ['notification-deliveries'], queryFn: () => api<Delivery[]>('/notification-deliveries'), refetchInterval: 10000 })
  const pending = useQuery({ queryKey: ['notification-digest-pending'], queryFn: () => api<PendingDigest[]>('/notification-digest-pending'), refetchInterval: 15000 })
  const policies = useQuery({ queryKey: ['news-policy-reminders'], queryFn: () => api<PolicyReminder[]>('/news-policy-reminders'), refetchInterval: 15000 })
  const subjects = useQuery({ queryKey: ['all-subjects'], queryFn: () => api<{ id: string; name: string }[]>('/subjects') })
  const [channelOpen, setChannelOpen] = useState(false), [ruleOpen, setRuleOpen] = useState(false)
  const [editing, setEditing] = useState<Rule | null>(null), [saving, setSaving] = useState(false)
  const [deliveryId, setDeliveryId] = useState<string | null>(null)
  const detail = useQuery({ queryKey: ['notification-delivery', deliveryId], queryFn: () => api<Delivery>('/notification-deliveries/' + deliveryId), enabled: !!deliveryId })
  const [channelForm] = Form.useForm(), [ruleForm] = Form.useForm()
  const kind = Form.useWatch('kind', channelForm) || 'feishu'
  const deliveryMode = Form.useWatch('delivery_mode', ruleForm) || 'immediate'
  const selectedEvents: string[] = Form.useWatch('event_types', ruleForm) || []
  const policySelected = selectedEvents.includes('news.policy_effective')
  const channelNames = Object.fromEntries((channels.data || []).map(c => [c.id, c.name]))
  async function action(path: string, body?: unknown, method = 'POST') {
    try {
      await api(path, { method, body: JSON.stringify(body || {}) })
      cache.invalidateQueries({ queryKey: ['notification-channels'] })
      cache.invalidateQueries({ queryKey: ['notification-rules'] })
      cache.invalidateQueries({ queryKey: ['notification-deliveries'] })
      cache.invalidateQueries({ queryKey: ['notification-digest-pending'] })
      cache.invalidateQueries({ queryKey: ['news-policy-reminders'] })
      message.success('已更新')
    } catch (error) { message.error((error as Error).message) }
  }
  async function saveChannel(values: Record<string, unknown>) {
    setSaving(true)
    try {
      const data = { name: values.name, kind: values.kind, enabled: values.enabled,
        ...(kind === 'email' ? { recipient: values.recipient } : { url: values.url, secret: values.secret || '' }) }
      await post('/notification-channels', data)
      setChannelOpen(false); channelForm.resetFields(); cache.invalidateQueries({ queryKey: ['notification-channels'] })
    } catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
  }
  async function saveRule(values: Record<string, unknown>) {
    setSaving(true)
    try {
      const saved = await api<Rule & { cancelled_pending_digest_items?: number; cancelled_pending_news_deliveries?: number }>('/notification-rules' + (editing ? '/' + editing.id : ''), {
        method: editing ? 'PUT' : 'POST', body: JSON.stringify({ ...values, timezone: 'Asia/Shanghai', cooldown_seconds: values.delivery_mode === 'daily_digest' ? 0 : values.cooldown_seconds }),
      })
      setRuleOpen(false); cache.invalidateQueries({ queryKey: ['notification-rules'] })
      cache.invalidateQueries({ queryKey: ['notification-digest-pending'] })
      cache.invalidateQueries({ queryKey: ['notification-deliveries'] })
      if (saved.cancelled_pending_digest_items) message.info(`已取消 ${saved.cancelled_pending_digest_items} 条旧的待汇总事件，后续新事件采用新规则。`)
      if (saved.cancelled_pending_news_deliveries) message.info(`已取消 ${saved.cancelled_pending_news_deliveries} 条不再有有效规则支持的待发送资讯。`)
    } catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
  }
  return <>
    <ModuleTitle title="通知设置" description="按关注对象和事件订阅提醒，查看每一次投递结果。" />
    <ModuleError error={channels.error || rules.error || deliveries.error || subjects.error || pending.error || policies.error} />
    <Tabs items={[
      { key: 'channels', label: '通知通道', children: <ModulePanel title="邮件与 Webhook" extra={<Button type="primary" onClick={() => setChannelOpen(true)}>新增通道</Button>}>
        <Table rowKey="id" dataSource={channels.data} loading={channels.isPending} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '类型', dataIndex: 'kind', render: v => names[v] },
          { title: '目标', dataIndex: 'destination_hint' },
          { title: '启用', render: (_, c: Channel) => <Switch checked={c.enabled} onChange={enabled => action('/notification-channels/' + c.id, { enabled }, 'PATCH')} /> },
          { title: '操作', render: (_, c: Channel) => <Popconfirm title="向这个通道发送一条测试通知？" onConfirm={() => action('/notification-channels/' + c.id + '/test')}><Button disabled={!c.enabled}>测试发送</Button></Popconfirm> },
        ]} /><div className="note-box">新通道默认关闭。Webhook 地址与签名密钥加密保存；邮件服务器由部署配置提供。</div>
      </ModulePanel> },
      { key: 'rules', label: '订阅规则', children: <ModulePanel title="事件订阅" extra={<Button type="primary" onClick={() => { setEditing(null); ruleForm.resetFields(); setRuleOpen(true) }}>新增规则</Button>}>
        <Table rowKey="id" dataSource={rules.data} loading={rules.isPending} columns={[
          { title: '规则', dataIndex: 'name' }, { title: '通道', dataIndex: 'channel_id', render: v => channelNames[v] || '—' },
          { title: '事件', dataIndex: 'event_types', render: (values: string[]) => values.map(v => <Tag key={v}>{names[v] || v}</Tag>) },
          { title: '资讯标签', render: (_, r: Rule) => (['regions', 'industries', 'topics'] as const).map((key, i) => r.news_filters?.[key]?.length ? <div key={key}>{['地区', '行业', '主题'][i]}：{r.news_filters[key].join('、')}</div> : null) },
          { title: '提醒方式', render: (_, r: Rule) => r.delivery_mode === 'daily_digest' ? <span>每日 {r.digest_time}<br /><small>北京时间 · 汇总前一天</small></span> : <Tag>即时提醒</Tag> },
          { title: '启用', render: (_, r: Rule) => <Switch checked={r.enabled} onChange={enabled => action('/notification-rules/' + r.id, { enabled }, 'PATCH')} /> },
          { title: '操作', render: (_, r: Rule) => <Button onClick={() => { setEditing(r); ruleForm.resetFields(); ruleForm.setFieldsValue({ ...r, news_filters: r.news_filters || emptyNewsFilters }); setRuleOpen(true) }}>编辑</Button> },
        ]} /><div className="note-box">首次采集建立岗位基线，不发“新增岗位”通知。即时规则命中同一通道时只生成一条投递；日报按规则分别汇总，没有匹配事件时不发送。</div>
      </ModulePanel> },
      { key: 'policies', label: '政策日期提醒', children: <ModulePanel title="政策提醒计划与历史">
        <Alert type="info" message="仅对官方政策全文中的唯一明确日期登记；生效日北京时间09:00处理，需有启用的即时规则与通道才发送。" />
        <Table rowKey="id" dataSource={policies.data} loading={policies.isPending} columns={[
          { title: '政策原文', render: (_, p: PolicyReminder) => <a href={p.url} target="_blank" rel="noreferrer">{p.title}</a> },
          { title: '原文标示施行日', dataIndex: 'effective_date' },
          { title: '计划处理（北京时间）', dataIndex: 'scheduled_for', render: value => beijing(value) },
          { title: '状态', render: (_, p: PolicyReminder) => <Tag>{({ pending: '等待生效日', emitted: '已生成通知事件', cancelled: '已取消' } as Record<string, string>)[p.status] || p.status}</Tag> },
          { title: '说明', render: (_, p: PolicyReminder) => p.error_code === 'policy_changed' ? '日期或依据已变化，旧提醒不再发送' : p.event_id ? '实际发送结果见投递记录' : '未生成通知事件' },
        ]} /><div className="note-box">首次发现已过去的政策不补发；同一政策同一生效日已处理后不自动复活。提醒不判断政策是否适用于本公司。</div>
      </ModulePanel> },
      { key: 'digests', label: '待汇总日报', children: <ModulePanel title="已纳入摘要的事件">
        <Table rowKey={r => r.rule_id + r.scheduled_for} dataSource={pending.data} loading={pending.isPending} columns={[
          { title: '规则', dataIndex: 'rule_name' }, { title: '通道', dataIndex: 'channel_name' },
          { title: '事件范围（北京时间）', render: (_, d: PendingDigest) => `${beijing(d.window_start)} 至 ${beijing(d.window_end)}（不含结束）` },
          { title: '事件数', dataIndex: 'event_count' }, { title: '预计汇总时间', dataIndex: 'scheduled_for', render: value => beijing(value) },
        ]} /><div className="note-box">按平台接收事件的自然日汇总，不是文章发布日期。到时由后台生成摘要并投递；处理延迟会保留原窗口，较多事件分片发送，每片带范围和原文链接。没有旧事件补发。</div>
      </ModulePanel> },
      { key: 'deliveries', label: '发送记录', children: <ModulePanel title="投递历史">
        <Table rowKey="id" dataSource={deliveries.data} loading={deliveries.isPending} columns={[
          { title: '内容', render: (_, d: Delivery) => <Button type="link" style={{ height: 'auto', whiteSpace: 'normal', textAlign: 'left' }} onClick={() => setDeliveryId(d.id)}>{d.title}</Button> }, { title: '通道', dataIndex: 'channel_name' },
          { title: '状态', dataIndex: 'status', render: stateTag }, { title: '尝试次数', dataIndex: 'attempts' },
          { title: '时间', dataIndex: 'created_at', render: dateText }, { title: '原因', dataIndex: 'error_code', render: v => v || '—' },
          { title: '操作', render: (_, d: Delivery) => ['failed', 'unknown', 'cancelled'].includes(d.status) ? <Popconfirm title={d.status === 'unknown' ? '收件方可能已收到，再发可能重复。仍要重发？' : '重新发送这条通知？'} onConfirm={() => action('/notification-deliveries/' + d.id + '/retry', { acknowledge_duplicate: d.status === 'unknown' })}><Button size="small">重发</Button></Popconfirm> : null },
        ]} /><div className="note-box">连接建立失败或限流最多自动尝试 3 次。读写超时和工作进程中断保留为“结果未知”，由管理员决定是否重发。</div>
      </ModulePanel> },
    ]} />
    <Modal title="新增通知通道" open={channelOpen} onCancel={() => setChannelOpen(false)} onOk={() => channelForm.submit()} confirmLoading={saving} okText="保存通道" destroyOnHidden>
      <Form form={channelForm} layout="vertical" initialValues={{ kind: 'feishu', enabled: false }} onFinish={saveChannel}>
        <Form.Item label="名称" name="name" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item label="类型" name="kind"><Select options={['feishu', 'webhook', 'email'].map(v => ({ value: v, label: names[v] }))} /></Form.Item>
        {kind === 'email' ? <Form.Item label="收件邮箱" name="recipient" rules={[{ required: true }, { type: 'email' }]}><Input /></Form.Item> : <>
          <Form.Item label="Webhook 地址" name="url" rules={[{ required: true }, { type: 'url' }]}><Input.Password /></Form.Item>
          <Form.Item label="签名密钥（可选）" name="secret"><Input.Password autoComplete="new-password" /></Form.Item>
        </>}
        <Form.Item label="启用通道" name="enabled" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Modal title={editing ? '编辑订阅规则' : '新增订阅规则'} open={ruleOpen} onCancel={() => setRuleOpen(false)} onOk={() => ruleForm.submit()} confirmLoading={saving} okText="保存规则" width={700} styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}>
      <Form form={ruleForm} layout="vertical" initialValues={{ enabled: false, subject_ids: [], keywords: [], news_filters: emptyNewsFilters, cooldown_seconds: 0, delivery_mode: 'immediate', digest_time: '09:00' }} onFinish={saveRule}>
        <Form.Item name="name" label="规则名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="channel_id" label="通知通道" rules={[{ required: true }]}><Select options={channels.data?.map(c => ({ value: c.id, label: c.name }))} /></Form.Item>
        <Form.Item name="event_types" label="关注事件" dependencies={(['regions', 'industries', 'topics'] as const).map(key => ['news_filters', key])}
          rules={[{ required: true }, { validator: (_, values: string[] = []) => {
            const filters: NewsFilters = ruleForm.getFieldValue('news_filters') || emptyNewsFilters
            return Object.values(filters).some(tags => tags.length) && values.some(value => !value.startsWith('news.'))
              ? Promise.reject(new Error('资讯标签条件只能搭配资讯事件；其他事件请单独建规则')) : Promise.resolve()
          } }]}><Select mode="multiple" optionFilterProp="label" onChange={(values: string[]) => {
            if (values.includes('news.policy_effective')) {
              ruleForm.setFieldValue('delivery_mode', 'immediate')
              void ruleForm.validateFields(['delivery_mode']).catch(() => undefined)
            }
          }} options={Object.keys(names).filter(v => v.includes('.')).map(v => ({ value: v, label: names[v] }))} /></Form.Item>
        {(['regions', 'industries', 'topics'] as const).map((key, i) => <Form.Item key={key} name={['news_filters', key]} label={['资讯地区标签', '资讯行业标签', '资讯主题标签'][i]}
          rules={[{ type: 'array', max: 30, message: '每类最多30个标签' }, { validator: (_, values: string[] = []) => values.some(value => !value.trim() || value.length > 150) ? Promise.reject(new Error('标签不能为空白且每项最多150字')) : Promise.resolve() }]}>
          <Select mode="tags" allowClear tokenSeparators={['，', ',']} /></Form.Item>)}
        <div className="note-box">每类标签内满足任一项，非空类别之间需全部满足；留空不限。标签按规范化后的精确值匹配，不推断地区或行业层级，未知标签不命中。</div>
        {policySelected && <Alert type="info" message="政策生效日提醒使用即时模式，于原文生效日北京时间09:00处理；仍需启用规则和通道，不自动启用外发。" />}
        <Form.Item name="subject_ids" label="关注对象（留空为全部）"><Select mode="multiple" options={subjects.data?.map(s => ({ value: s.id, label: s.name }))} /></Form.Item>
        <Form.Item name="keywords" label="至少命中一个关键词（留空为不限）"><Select mode="tags" tokenSeparators={['，', ',']} /></Form.Item>
        <Form.Item name="delivery_mode" label="提醒方式" dependencies={['event_types']} rules={[{ required: true }, { validator: () => (ruleForm.getFieldValue('event_types') || []).includes('news.policy_effective') && ruleForm.getFieldValue('delivery_mode') !== 'immediate' ? Promise.reject(new Error('生效日提醒只能使用即时模式')) : Promise.resolve() }]}><Select options={[{ value: 'immediate', label: '即时提醒' }, { value: 'daily_digest', label: '每日定时摘要', disabled: policySelected }]} /></Form.Item>
        {deliveryMode === 'daily_digest' ? <><Form.Item name="digest_time" label="每天发送时间（北京时间）" rules={[{ required: true }, { pattern: /^(?:[01]\d|2[0-3]):[0-5]\d$/, message: '请输入 HH:MM' }]}><Input type="time" step={60} /></Form.Item><p className="subtle">汇总前一天 00:00 至次日 00:00 收到的全部匹配事件，启用之前的历史事件不补发，不使用即时冷却。</p></> : <Form.Item name="cooldown_seconds" label="同规则提醒间隔 / 秒"><InputNumber min={0} max={86400} /></Form.Item>}
        <Space align="start">
          <Form.Item name="enabled" label="启用规则" valuePropName="checked"><Switch /></Form.Item></Space>
        <p className="subtle">修改规则会取消旧的待发摘要；资讯即时提醒只在同通道仍有其他有效规则支持时保留。正在发送或结果未知的消息不能撤回，不自动补发历史事件。</p>
      </Form>
    </Modal>
    <Drawer title="通知内容与来源" open={!!deliveryId} onClose={() => setDeliveryId(null)} width={760}>
      <ModuleError error={detail.error} />
      {detail.data && <><h2>{detail.data.title}</h2><Descriptions column={1} items={[
        { label: '通道', children: detail.data.channel_name }, { label: '状态', children: stateTag(detail.data.status) },
        ...(detail.data.digest ? [{ label: '北京时间窗口', children: `${beijing(detail.data.digest.window_start)} 至 ${beijing(detail.data.digest.window_end)}（不含结束）` }, { label: '事件范围', children: `${detail.data.digest.range_start}–${detail.data.digest.range_end} / ${detail.data.digest.total_items}` }] : []),
      ]} />{detail.data.digest?.items ? detail.data.digest.items.map(item => <div className="source-result" key={item.event_id}><strong>{item.title}</strong><p>{item.summary}</p><p>{names[item.event_type] || item.event_type} · 纳入摘要 {beijing(item.included_at)}</p>{item.url && <a href={item.url} target="_blank" rel="noreferrer">查看来源原文</a>}</div>) : <p style={{ whiteSpace: 'pre-wrap' }}>{detail.data.text}</p>}</>}
    </Drawer>
  </>
}
