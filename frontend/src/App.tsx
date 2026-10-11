import { lazy, Suspense, useRef, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  App as AntApp, Alert, Button, Descriptions, Drawer, Empty, Form, Input, InputNumber, Modal,
  Progress, Select, Space, Spin, Switch, Table, Tag, Timeline,
} from 'antd'
import {
  Activity, ArrowRight, Building2, CalendarClock, ChevronRight, Database, ExternalLink,
  LayoutDashboard, LogOut, Plus, Radar, RefreshCw, Search, Settings2, BriefcaseBusiness, FileText,
  Bell, MessageSquare, Newspaper, ChartNoAxesCombined, Bot, Wrench,
} from 'lucide-react'
import { beijingCalendarDate, beijingDateTime } from './date'
import { api, ApiError, IdempotentActions, post } from './api'
import LoginSessionView from './LoginSessionView'
import ProbeLoginView from './ProbeLoginView'
import type { ProbeLogin } from './ProbeLoginView'
import { stateTag } from './ModuleUI'
import RetentionSettings, { ReportRetentionNotice, ReportRetentionState } from './RetentionSettings'
import ModelSettingsPanel from './ModelSettingsPanel'
const SentimentPage = lazy(() => import('./SentimentPage'))
const NotificationsPage = lazy(() => import('./NotificationsPage'))
const NewsPage = lazy(() => import('./NewsPage'))
const CompanyWorkspace = lazy(() => import('./CompanyWorkspace'))
const AdminSettings = lazy(() => import('./AdminSettings'))
const RecruitmentAnalytics = lazy(() => import('./RecruitmentAnalytics'))
const AssistantPage = lazy(() => import('./AssistantPage'))
const MaintenancePage = lazy(() => import('./MaintenancePage'))
const DevicesPage = lazy(() => import('./DevicesPage'))
import type {
  Change, Company, Dashboard, Job, JobDetail, LoginSession, Plan, Report, Source, SourceAccount, SystemStatus, Task, TaskDetail,
} from './api'

const labels: Record<string, string> = {
  queued: '排队中', running: '运行中', succeeded: '已完成', partial: '部分完成',
  failed: '失败', cancelled: '已取消', blocked_configuration: '待配置',
  waiting_login: '待登录', waiting_verification: '待验证', unknown: '待检查',
  waiting_account: '等待账号空闲', verification_required: '需要人工验证',
  waiting_device: '等待采集设备',
  ready: '可用', observed: '已观察到', suspected_removed: '疑似下架',
  saved: '会话已保存 · 待验证', logged_out: '未登录', expired: '已失效',
  complete: '范围完整', first_seen: '首次发现', baseline_seen: '基线采集',
  content_changed: '内容变化', salary_changed: '薪资变化', reappeared: '重新出现',
  adaptation_required: '需要适配', careers: '招聘官网', boss: 'BOSS 直聘', liepin: '猎聘',
  zhaopin: '智联招聘', '51job': '前程无忧', xhs: '小红书', dy: '抖音', wb: '微博',
  competitor: '竞品公司', own: '自有公司', other: '其他关注',
}
const colors: Record<string, string> = {
  running: 'processing', succeeded: 'success', ready: 'success', observed: 'default',
  partial: 'warning', failed: 'error', waiting_login: 'orange', blocked_configuration: 'default',
  first_seen: 'green', content_changed: 'blue', suspected_removed: 'orange',
}
const formatDate = (value?: string | null) => beijingDateTime(value, true)
const status = (value: string) => <Tag color={colors[value]}>{labels[value] || value}</Tag>
const taskName = (task: Task) => task.config_snapshot.company_name || task.config_snapshot.subject?.name || task.config_snapshot.name || '情报任务'
const taskKind = (task: Task) => ({ company_collection: '独立公司采集', company_analysis: '公司招聘分析', company_comparison: '公司招聘比较',
  sentiment_collect: '舆情采集', sentiment_collection: '舆情采集', sentiment_analyze: '舆情分析', sentiment_analysis: '舆情分析', sentiment_bettafish: 'BettaFish 报告',
  sentiment_import: '舆情资料导入', news_collection: 'HR 资讯采集', adapter_json_repair: '适配器维护' } as Record<string, string>)[task.kind] || '情报任务'
function PageTitle({ eyebrow, title, description, action }: {
  eyebrow: string; title: string; description: string; action?: React.ReactNode
}) {
  return <div className="page-title"><div><div className="eyebrow">{eyebrow}</div>
    <h1>{title}</h1><p>{description}</p></div>{action}</div>
}
function LoadError({ error }: { error: Error | null }) {
  return error ? <Alert type="error" showIcon message={error.message} className="load-error" /> : null
}
function Panel({ title, extra, children }: { title: string; extra?: React.ReactNode; children: React.ReactNode }) {
  return <section className="panel"><div className="panel-header"><h2>{title}</h2>{extra}</div>{children}</section>
}
function useCompanies() {
  return useQuery({ queryKey: ['companies'], queryFn: () => api<Company[]>('/companies') })
}

function Login({ onSuccess }: { onSuccess: () => void }) {
  const { message } = AntApp.useApp()
  const login = useMutation({ mutationFn: (data: { username: string; password: string }) => post('/auth/login', data),
    onSuccess, onError: (error: Error) => message.error(error.message) })
  return <div className="login-screen"><div className="login-story">
    <div className="brand"><Radar size={30} /><span>观势<span className="brand-dot">.</span></span></div>
    <div><div className="eyebrow">COMPANY INTELLIGENCE</div><h1>看见变化，<br />理解变化。</h1>
      <p>招聘动向、公司声誉与行业信息，<br />在同一处持续积累。</p></div>
    <span className="login-foot">企业内部工作空间 · V1</span>
  </div><div className="login-form"><div className="eyebrow">WELCOME BACK</div><h2>进入情报工作台</h2>
    <p>使用内部账号登录，继续你的观察。</p>
    <Form layout="vertical" onFinish={data => login.mutate(data)} initialValues={{ username: 'admin' }}>
      <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input size="large" autoComplete="username" /></Form.Item>
      <Form.Item name="password" label="密码" rules={[{ required: true }]}><Input.Password size="large" autoComplete="current-password" /></Form.Item>
      <Button block type="primary" size="large" htmlType="submit" loading={login.isPending}>登录工作台 <ArrowRight size={17} /></Button>
    </Form><p className="subtle">账号由管理员管理；首次本地启动的访问资料保存在项目私有运行目录。</p>
  </div></div>
}

function Overview() {
  const navigate = useNavigate()
  const summary = useQuery({ queryKey: ['dashboard'], queryFn: () => api<Dashboard>('/dashboard'), refetchInterval: 10000 })
  const tasks = useQuery({ queryKey: ['tasks', 'recent'], queryFn: () => api<Task[]>('/tasks?limit=5'), refetchInterval: 10000 })
  const changes = useQuery({ queryKey: ['changes', 'recent'], queryFn: () => api<Change[]>('/job-changes?limit=6') })
  const data = summary.data
  return <>
    <PageTitle eyebrow="WORKSPACE OVERVIEW" title="情报总览" description="把分散的外部信息，变成有迹可循的企业观察。"
      action={<Button icon={<Plus size={16} />} type="primary" onClick={() => navigate('/companies')}>添加关注公司</Button>} />
    <LoadError error={summary.error} />
    <div className="metrics">{[
      ['关注公司', data?.companies, '明确对象，持续跟踪', Building2],
      ['已观察岗位', data?.jobs, '保留来源和历史版本', BriefcaseBusiness],
      ['近 7 日变化', data?.changes_7d, '不包含首次基线采集', Activity],
      ['待处理任务', data?.needs_action, '登录、验证或来源配置', Database],
      ['舆情资料', data?.sentiment_items, '已采集且未忽略的样本', MessageSquare],
      ['未读行业资讯', data?.unread_news, '保留发布与生效日期', Newspaper],
      ['分析报告', data?.reports, '招聘、舆情与行业资料', FileText],
      ['启用关注对象', data?.active_subjects, '公司、品牌和产品', Radar],
    ].map(([title, value, caption, Icon], index) => {
      const Component = Icon as typeof Building2
      return <div className="metric" key={index}><div className="metric-top"><span>{String(title)}</span><Component size={19} /></div>
        <strong>{value === undefined ? '—' : String(value)}</strong><span className="metric-caption">{String(caption)}</span></div>
    })}</div>
    {!data?.companies && !summary.isPending && <div className="start-card"><div className="start-icon"><Radar size={30} /></div>
      <div><h2>从第一家关注公司开始</h2><p>添加公司与招聘入口，发起一次采集。首轮建立基线，后续记录每一次变化。</p>
        <div className="step-row"><span>01 添加公司</span><ChevronRight size={15} /><span>02 绑定来源</span><ChevronRight size={15} /><span>03 查看变化</span></div></div>
      <Button onClick={() => navigate('/companies')} icon={<ArrowRight size={16} />}>开始关注</Button></div>}
    <div className="two-columns"><Panel title="最近任务" extra={<Button type="link" onClick={() => navigate('/tasks')}>全部任务 <ArrowRight size={14} /></Button>}>
      {tasks.data?.length ? tasks.data.map(t => <div className="activity-row" key={t.id}><div className="activity-icon"><Building2 size={18} /></div>
        <div className="grow"><strong>{taskName(t)}</strong><small>{formatDate(t.created_at)} · {taskKind(t)}</small></div>{status(t.status)}</div>)
        : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有执行记录" />}
    </Panel><Panel title="招聘变化" extra={<span className="subtle">按实际观察记录</span>}>
      {changes.data?.length ? changes.data.map(c => <div className="activity-row" key={c.id}><div className="grow"><strong>{c.title}</strong><small>{formatDate(c.created_at)}</small></div>{status(c.type)}</div>)
        : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="建立基线后，岗位变化会出现在这里" />}
    </Panel></div>
    <div className="data-foot"><span className="status-dot" /> 最近完成采集：{formatDate(data?.latest_completed_at)}<span>统计仅覆盖已接入来源</span></div>
  </>
}

function Companies() {
  const { message } = AntApp.useApp(), client = useQueryClient(), navigate = useNavigate()
  const list = useCompanies()
  const runRequests = useRef(new IdempotentActions()), runInFlight = useRef(new Set<string>())
  const [visible, setVisible] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false), [bulkNames, setBulkNames] = useState(''), [bulkSaving, setBulkSaving] = useState(false)
  const [form] = Form.useForm()
  const add = useMutation({
    mutationFn: (values: Record<string, string>) => post<Company>('/companies', {
      ...values, aliases: values.aliases ? values.aliases.split(/[，,]/).map(x => x.trim()).filter(Boolean) : [],
    }),
    onSuccess: () => { setVisible(false); form.resetFields(); client.invalidateQueries({ queryKey: ['companies'] }); client.invalidateQueries({ queryKey: ['dashboard'] }); message.success('公司已添加，接下来绑定招聘来源') },
    onError: (e: Error) => message.error(e.message),
  })
  async function run(id: string) {
    if (runInFlight.current.has(id)) return
    runInFlight.current.add(id)
    try { await runRequests.current.post('/companies/' + id + '/recruitment-runs'); client.invalidateQueries({ queryKey: ['tasks'] }); navigate('/tasks') }
    catch (e) { message.error((e as Error).message) }
    finally { runInFlight.current.delete(id) }
  }
  async function analyze(id: string) {
    try { await post('/companies/' + id + '/analysis'); message.success('分析已排队，将使用一次受预算限制的模型请求'); navigate('/tasks') }
    catch (e) { message.error((e as Error).message) }
  }
  return <>
    <PageTitle eyebrow="WATCHLIST" title="关注公司" description="明确关注对象，给每家公司建立独立的观察记录。"
      action={<Space><Button onClick={() => setBulkOpen(true)}>批量添加</Button><Button type="primary" icon={<Plus size={16} />} onClick={() => setVisible(true)}>添加公司</Button></Space>} />
    <LoadError error={list.error} /><Panel title="公司清单" extra={<span className="subtle">{list.data?.length || 0} 家公司</span>}>
      <Table rowKey="id" dataSource={list.data} loading={list.isPending} pagination={{ pageSize: 10 }} locale={{ emptyText: '尚未添加公司。平台不会替你假定竞品名单。' }}
        columns={[
          { title: '公司', dataIndex: 'name', render: (name, c: Company) => <div><Button type="link" onClick={() => navigate('/companies/' + c.id)}><strong>{name}</strong></Button><div className="subtle">{c.official_name || '用户指定主体'}</div></div> },
          { title: '类型', dataIndex: 'role', render: (v: string) => status(v) },
          { title: '官网', dataIndex: 'website', render: (v?: string) => v ? <a href={v} target="_blank" rel="noreferrer" className="external-link">{new URL(v).hostname} <ExternalLink size={12} /></a> : <span className="subtle">待补充</span> },
          { title: '备注', dataIndex: 'notes', ellipsis: true },
          { title: '操作', render: (_, c: Company) => <Space wrap><Button size="small" onClick={() => navigate('/companies/' + c.id)}>公司档案</Button><Button size="small" onClick={() => navigate('/sources?company=' + c.id)}>配置来源</Button><Button size="small" onClick={() => run(c.id)}>立即采集</Button><Button size="small" onClick={() => analyze(c.id)}>生成简报</Button></Space> },
        ]} />
    </Panel>
    <Modal title="添加关注公司" open={visible} onCancel={() => setVisible(false)} onOk={() => form.submit()} confirmLoading={add.isPending} okText="保存公司">
      <Form layout="vertical" form={form} onFinish={v => add.mutate(v)} initialValues={{ role: 'competitor' }}>
        <Form.Item name="name" label="公司名称" rules={[{ required: true }]}><Input placeholder="输入明确的公司名称" /></Form.Item>
        <Form.Item name="role" label="关注类型"><Select options={['competitor', 'own', 'other'].map(x => ({ value: x, label: labels[x] }))} /></Form.Item>
        <Form.Item name="website" label="公司官网" rules={[{ type: 'url' }]}><Input placeholder="https://..." /></Form.Item>
        <Form.Item name="aliases" label="简称或品牌别名"><Input placeholder="多个别名用逗号分隔" /></Form.Item>
        <Form.Item name="notes" label="备注"><Input.TextArea rows={2} placeholder="主体范围、业务线等" /></Form.Item>
      </Form>
    </Modal>
    <Modal title="批量添加关注公司" open={bulkOpen} onCancel={() => setBulkOpen(false)} confirmLoading={bulkSaving} okText="添加名单" onOk={async () => {
      setBulkSaving(true)
      try {
        const result = await post<{ created: number; review: number }>('/companies/bulk', { names: bulkNames })
        message.success(`新增 ${result.created} 家；${result.review} 家同名对象需核对`)
        setBulkOpen(false); setBulkNames(''); client.invalidateQueries({ queryKey: ['companies'] })
      } catch (error) { message.error((error as Error).message) } finally { setBulkSaving(false) }
    }}><p>每行一个公司名，单次最多 100 家。名称相同的已有记录会提示核对。</p><Input.TextArea rows={8} value={bulkNames} onChange={e => setBulkNames(e.target.value)} /></Modal>
  </>
}

function Sources() {
  const { message } = AntApp.useApp(), cache = useQueryClient(), companies = useCompanies()
  const list = useQuery({ queryKey: ['sources'], queryFn: () => api<Source[]>('/source-bindings') })
  const [visible, setVisible] = useState(false), [form] = Form.useForm()
  const [editingSource, setEditingSource] = useState<Source | null>(null)
  const [accountVisible, setAccountVisible] = useState(false), [accountForm] = Form.useForm()
  const [loginSession, setLoginSession] = useState<LoginSession | null>(null)
  const [probeLogin, setProbeLogin] = useState<ProbeLogin | null>(null)
  const probeProfiles = useQuery({ queryKey: ['probe-profiles'], queryFn: () => api<{ id: string; device_name: string; name: string; enabled: boolean }[]>('/probes/profiles') })
  const [opening, setOpening] = useState<string | null>(null)
  const [showArchivedSources, setShowArchivedSources] = useState(false)
  const archivedCompanies = useQuery({ queryKey: ['companies', 'archived'], queryFn: () => api<Company[]>('/companies?archived=true'), enabled: showArchivedSources })
  const accounts = useQuery({ queryKey: ['source-accounts'], queryFn: () => api<SourceAccount[]>('/source-accounts') })
  const companyNames = Object.fromEntries([...(companies.data || []), ...(archivedCompanies.data || [])].map(c => [c.id, c.name]))
  const activeCompanyIds = new Set((companies.data || []).map(c => c.id))
  const visibleSources = (list.data || []).filter(source => showArchivedSources || activeCompanyIds.has(source.company_id))
  const sourceKind = Form.useWatch('kind', form) || editingSource?.kind || 'careers'
  const add = useMutation({
    mutationFn: (v: { company_id: string; kind: string; entry_url: string; account_id?: string; external_company_id?: string; enabled?: boolean }) => editingSource
      ? api('/source-bindings/' + editingSource.id, { method: 'PATCH', body: JSON.stringify({ entry_url: v.entry_url, account_id: v.account_id || null, external_company_id: v.external_company_id || null, enabled: v.enabled }) })
      : post('/source-bindings', { company_id: v.company_id, kind: v.kind, entry_url: v.entry_url, account_id: v.account_id || null, external_company_id: v.external_company_id || null,
        adapter: new URL(v.entry_url).hostname === 'careers.tencent.com' && v.kind === 'careers' ? 'tencent' : 'generic' }),
    onSuccess: () => { setVisible(false); form.resetFields(); cache.invalidateQueries({ queryKey: ['sources'] }); message.success('来源已保存') },
    onError: (e: Error) => message.error(e.message),
  })
  const addAccount = useMutation({
    mutationFn: (v: { kind: string; name: string }) => post('/source-accounts', v),
    onSuccess: (_, values) => { setAccountVisible(false); accountForm.resetFields(); cache.invalidateQueries({ queryKey: ['source-accounts'] }); message.success(`账号记录已创建，请在下方点击“登录${labels[values.kind] || values.kind}”完成认证`) },
    onError: (e: Error) => message.error(e.message),
  })
  async function openLogin(id: string) {
    setOpening(id)
    try {
      if (accounts.data?.find(a => a.id === id)?.probe) setProbeLogin(await post<ProbeLogin>('/probes/accounts/' + id + '/login'))
      else setLoginSession(await post<LoginSession>('/source-accounts/' + id + '/login-sessions'))
    }
    catch (e) { message.error((e as Error).message) }
    finally { setOpening(null) }
  }
  return <>
    <PageTitle eyebrow="DATA CONNECTIONS" title="数据源与登录" description="管理来源入口与独立账号会话，需要登录时由你完成。"
      action={<Space><Button onClick={() => setAccountVisible(true)}>添加平台账号</Button><Button type="primary" icon={<Plus size={16} />} onClick={() => { setEditingSource(null); form.resetFields(); setVisible(true) }}>绑定招聘来源</Button></Space>} />
    <Alert type="info" showIcon style={{ marginBottom: 20 }} message="需要登录的平台推荐使用桌面探针"
      description="先到“采集设备”下载并配对探针，在设备的Chrome中登录。下方账号绑定该采集浏览器后，招聘和舆情都复用同一个本机登录环境；同平台账号可用于多家公司来源。" />
    <LoadError error={list.error || companies.error || archivedCompanies.error || accounts.error} />
    <Panel title="招聘来源" extra={<Space><Switch checked={showArchivedSources} onChange={setShowArchivedSources} aria-label="显示归档公司来源" /><span>显示归档公司来源</span></Space>}><Table rowKey="id" dataSource={visibleSources} loading={list.isPending || companies.isPending || (showArchivedSources && archivedCompanies.isPending)} columns={[
      { title: '公司', dataIndex: 'company_id', render: (v: string) => <>{companyNames[v] || v}{!activeCompanyIds.has(v) && <Tag>已归档</Tag>}</> },
      { title: '渠道', dataIndex: 'kind', render: (v: string) => labels[v] },
      { title: '入口', dataIndex: 'entry_url', ellipsis: true, render: (v: string) => <a href={v} target="_blank" rel="noreferrer">{v}</a> },
      { title: '状态', render: (_, source: Source) => source.enabled ? status(source.health) : <Tag>已停用</Tag> },
      { title: '最近成功', dataIndex: 'last_success_at', render: (v: string) => formatDate(v) },
      { title: '操作', render: (_, source: Source) => <Button size="small" onClick={() => { setEditingSource(source); form.setFieldsValue(source); setVisible(true) }}>编辑来源</Button> },
    ]} /></Panel>
    <div style={{ height: 22 }} /><Panel title="平台账号与登录"><Table rowKey="id" dataSource={accounts.data} loading={accounts.isPending} columns={[
      { title: '账号备注', dataIndex: 'name' },
      { title: '平台', dataIndex: 'kind', render: (v: string) => labels[v] || v },
      { title: '采集浏览器', render: (_, a: SourceAccount) => <Select style={{ minWidth: 200 }} value={a.probe?.profile_id || 'legacy'} onChange={async value => {
        try { await api('/probes/accounts/' + a.id, { method: 'PUT', body: JSON.stringify({ profile_id: value === 'legacy' ? null : value }) }); cache.invalidateQueries({ queryKey: ['source-accounts'] }) }
        catch (e) { message.error((e as Error).message) }
      }} options={[{ value: 'legacy', label: '服务器浏览器（既有方式）' }, ...(probeProfiles.data || []).map(p => ({ value: p.id, label: p.device_name + ' · ' + p.name, disabled: !p.enabled }))]} /> },
      { title: '状态', dataIndex: 'health', render: (v: string) => status(v) },
      { title: '最近验证', dataIndex: 'last_verified_at', render: (v: string) => formatDate(v) },
      { title: '操作', render: (_, account: SourceAccount) => <Button size="small" loading={opening === account.id} onClick={() => openLogin(account.id)}>{account.probe ? '在设备Chrome登录' : '登录' + (labels[account.kind] || account.kind)}</Button> },
    ]} /></Panel>
    <div className="note-box">扫码、验证码和密码由你在独立会话中完成。保存会话不等于已经采集成功，平台会在首次采集时验证可用性。</div>
    <Modal title={editingSource ? '编辑招聘来源' : '绑定招聘来源'} open={visible} onCancel={() => setVisible(false)} onOk={() => form.submit()} confirmLoading={add.isPending} okText="保存来源">
      <Form layout="vertical" form={form} initialValues={{ kind: 'careers', company_id: new URLSearchParams(location.search).get('company') || undefined }} onFinish={v => add.mutate(v)}>
        <Form.Item name="company_id" label="关注公司" rules={[{ required: true }]}><Select disabled={!!editingSource} placeholder="选择公司" options={companies.data?.map(c => ({ value: c.id, label: c.name }))} /></Form.Item>
        <Form.Item name="kind" label="来源渠道"><Select disabled={!!editingSource} onChange={() => form.setFieldValue('account_id', undefined)} options={['careers', 'boss', 'liepin', 'zhaopin', '51job'].map(v => ({ value: v, label: labels[v] }))} /></Form.Item>
        <Form.Item name="entry_url" label="招聘列表或公司招聘页" rules={[{ required: true }, { type: 'url' }]}><Input placeholder="https://..." /></Form.Item>
        <Form.Item name="account_id" label="使用账号（官网可留空）" extra="请先在下方添加平台账号并登录，再选择与来源渠道相同的平台账号。"><Select allowClear options={accounts.data?.filter(a => a.kind === sourceKind).map(a => ({ value: a.id, label: (labels[a.kind] || a.kind) + ' · ' + a.name }))} /></Form.Item>
        <Form.Item name="external_company_id" label="平台公司标识（可选）" extra="可从公司页链接识别；无法匹配时再补充"><Input /></Form.Item>
        {editingSource && <Form.Item name="enabled" label="启用来源" valuePropName="checked"><Switch /></Form.Item>}
      </Form>
    </Modal>
    <Modal title="添加平台账号" open={accountVisible} onCancel={() => setAccountVisible(false)}
      onOk={() => accountForm.submit()} confirmLoading={addAccount.isPending} okText="创建账号记录">
      <Form layout="vertical" form={accountForm} initialValues={{ kind: 'boss' }} onFinish={v => addAccount.mutate(v)}>
        <Form.Item name="kind" label="平台"><Select options={['boss', 'liepin', 'zhaopin', '51job', 'xhs', 'dy', 'wb'].map(v => ({ value: v, label: labels[v] }))} /></Form.Item>
        <Form.Item name="name" label="账号备注" rules={[{ required: true }]}><Input placeholder="例如：招聘信息浏览账号" /></Form.Item>
      </Form>
      <p className="subtle">账号备注只是方便识别的名称。创建记录后，在下方点击对应平台的登录按钮；此处不填写平台密码。</p>
    </Modal>
    <LoginSessionView session={loginSession} onClose={() => setLoginSession(null)}
      onSaved={() => cache.invalidateQueries({ queryKey: ['source-accounts'] })} />
    <ProbeLoginView session={probeLogin} onClose={() => setProbeLogin(null)} />
  </>
}

function Tasks() {
  const { message } = AntApp.useApp(), cache = useQueryClient()
  const list = useQuery({ queryKey: ['tasks'], queryFn: () => api<Task[]>('/tasks'), refetchInterval: 4000 })
  const [selected, setSelected] = useState<string | null>(null)
  const detail = useQuery({ queryKey: ['task', selected], queryFn: () => api<TaskDetail>('/tasks/' + selected),
    enabled: !!selected, refetchInterval: selected ? 4000 : false })
  async function act(id: string, action: string) {
    try { const result = await post<{ created?: boolean }>('/tasks/' + id + '/' + action); cache.invalidateQueries({ queryKey: ['tasks'] }); message.success(action === 'cancel' ? '已请求取消' : result.created === false ? '已复用同范围的运行任务' : '已创建重试任务') }
    catch (e) { message.error((e as Error).message) }
  }
  return <>
    <PageTitle eyebrow="EXECUTION CENTER" title="任务中心" description="按公司独立执行，清楚看到进度、覆盖范围和需要处理的事项。"
      action={<Button icon={<RefreshCw size={15} />} onClick={() => list.refetch()}>刷新</Button>} />
    <LoadError error={list.error} />
    <Panel title="运行任务"><Table rowKey="id" dataSource={list.data} loading={list.isPending} columns={[
      { title: '关注对象', render: (_, t: Task) => <Button type="link" onClick={() => setSelected(t.id)}>{taskName(t)}</Button> },
      { title: '状态', dataIndex: 'status', render: (v: string) => status(v) },
      { title: '进度', dataIndex: 'progress', width: 160, render: (v: number, t: Task) => <Progress percent={v} size="small" status={t.status === 'failed' ? 'exception' : undefined} /> },
      { title: '开始时间', dataIndex: 'created_at', render: (v: string) => formatDate(v) },
      { title: '已观察岗位', render: (_, t: Task) => t.summary.counts?.observed ?? '—' },
      { title: '运行说明', dataIndex: 'error_message', render: (v: string | null) => v || '—' },
      { title: '操作', render: (_, t: Task) => <Space><Button size="small" onClick={() => setSelected(t.id)}>详情</Button>
        {['queued', 'running'].includes(t.status) ? <Button size="small" onClick={() => act(t.id, 'cancel')}>取消</Button>
          : <><Button size="small" onClick={() => act(t.id, 'retry')}>{t.kind === 'company_collection' ? '接续重试' : '重试'}</Button>{t.kind === 'company_collection' && <Button size="small" onClick={() => act(t.id, 'retry?resume=false')}>从头采集</Button>}</>}</Space> },
    ]} /></Panel>
    <Drawer title={detail.data ? taskName(detail.data) : '任务详情'} open={!!selected} onClose={() => setSelected(null)} width={620}>
      <LoadError error={detail.error} />{detail.data ? <>
        <Space className="drawer-space">{status(detail.data.status)}<span className="subtle">{detail.data.id.slice(0, 8)}</span></Space>
        {detail.data.error_message && <Alert type="warning" message={detail.data.error_message} />}
        <h3>来源执行情况</h3>{detail.data.sources.map(s => <div className="source-result" key={s.id}>
          <Space>{status(s.status)}{status(s.coverage)}</Space><p>{s.error_message || String(s.counts.stop_reason || '来源处理中')}</p>
          {s.checkpoint && <p>已保留 {s.checkpoint.saved_pages} 个页面证据 · {(s.checkpoint.raw_bytes / 1024 / 1024).toFixed(1)} MB。{s.checkpoint.status === 'complete' ? '本轮读取已结束。' : s.checkpoint.within_time_limit ? `可在 ${formatDate(s.checkpoint.expires_at)} 前接续，届时会复核来源配置。` : '检查点已过期，请选择从头采集。'}</p>}
        </div>)}
        <h3>执行记录</h3><Timeline items={detail.data.events.map(e => ({ color: e.level === 'warning' ? '#d79035' : '#638d83',
          children: <div><small className="subtle">{formatDate(e.created_at)}</small><p>{e.message}</p></div> }))} />
      </> : <Spin />}</Drawer>
  </>
}

function Jobs() {
  const companies = useCompanies(), [company, setCompany] = useState<string | undefined>(), [q, setQ] = useState('')
  const [page, setPage] = useState(1), [selected, setSelected] = useState<string | null>(null)
  const params = new URLSearchParams({ page: String(page), page_size: '20', q })
  if (company) params.set('company_id', company)
  const list = useQuery({ queryKey: ['jobs', company, q, page], queryFn: () => api<{ items: Job[]; total: number }>('/jobs?' + params) })
  const detail = useQuery({ queryKey: ['job', selected], queryFn: () => api<JobDetail>('/jobs/' + selected), enabled: !!selected })
  const names = Object.fromEntries((companies.data || []).map(c => [c.id, c.name]))
  return <>
    <PageTitle eyebrow="RECRUITMENT INTELLIGENCE" title="招聘情报" description="保留每条岗位的来源与版本，让变化有据可查。" />
    <div className="filter-bar"><Input.Search placeholder="搜索岗位名称" prefix={<Search size={15} />} onSearch={v => { setQ(v); setPage(1) }} allowClear style={{ width: 300 }} />
      <Select placeholder="全部公司" allowClear style={{ width: 220 }} onChange={v => { setCompany(v); setPage(1) }} options={companies.data?.map(c => ({ value: c.id, label: c.name }))} /></div>
    <LoadError error={list.error} /><Panel title="岗位列表" extra={<span className="subtle">已观察到 {list.data?.total || 0} 个岗位</span>}>
      <Table rowKey="id" dataSource={list.data?.items} loading={list.isPending} pagination={{ current: page, pageSize: 20, total: list.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '岗位', dataIndex: 'title', render: (title, j: Job) => <Button type="link" style={{ padding: 0, height: 'auto', textAlign: 'left', whiteSpace: 'normal' }} onClick={() => setSelected(j.id)}><strong>{title}</strong></Button> },
        { title: '公司', dataIndex: 'company_id', render: (v: string) => names[v] || '—' },
        { title: '工作地点', dataIndex: 'locations', render: (v: string[]) => v.join(' / ') || '未披露' },
        { title: '职能', dataIndex: 'function', render: (v?: string) => v || '待归类' },
        { title: '状态', dataIndex: 'status', render: (v: string) => status(v) },
        { title: '最近观察', render: (_, j: Job) => formatDate(j.sources[0]?.last_seen_at) },
      ]} />
    </Panel><div className="note-box">“首次发现”是平台首次观察到的时间；岗位记录数不等于实际招聘人数。</div>
    <Drawer title={detail.data?.title || '岗位详情'} open={!!selected} onClose={() => setSelected(null)} width={760}>
      <LoadError error={detail.error} />
      {detail.data?.sources.map(s => <div key={s.id}><Space className="drawer-space">{status(s.status)}<a href={s.url} target="_blank" rel="noreferrer" className="external-link">查看原始岗位 <ExternalLink size={14} /></a></Space>
        <Descriptions column={2} size="small" items={[
          { label: '工作地点', children: detail.data?.locations.join(' / ') },
          { label: '招聘主体', children: s.data.recruiting_entity || '未披露' },
          { label: '经验', children: s.data.experience || '未披露' },
          { label: '薪资', children: s.data.salary_raw || '未披露' },
          { label: '首次发现', children: formatDate(s.first_seen_at) },
          { label: '最近发现', children: formatDate(s.last_seen_at) },
        ]} /><h3>岗位职责</h3><p className="job-description">{s.data.description || '来源未提供'}</p>
        <h3>任职要求</h3><p className="job-description">{s.data.requirements || '来源未提供'}</p></div>)}
      <h3>变化历史</h3>{detail.data?.changes.length ? <Timeline items={detail.data.changes.map(c => ({
        children: <Space>{status(c.type)}<span>{formatDate(c.created_at)}</span></Space>,
      }))} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无历史变化" />}
    </Drawer>
  </>
}

function Plans() {
  const { message } = AntApp.useApp(), cache = useQueryClient(), navigate = useNavigate(), companies = useCompanies()
  const list = useQuery({ queryKey: ['plans'], queryFn: () => api<Plan[]>('/plans') })
  const runRequests = useRef(new IdempotentActions()), actionInFlight = useRef(new Set<string>())
  const [visible, setVisible] = useState(false), [form] = Form.useForm()
  const [editing, setEditing] = useState<Plan | null>(null)
  const defaultFilters = { cities: [], keywords: [], exclusions: [], sources: [], max_pages: 10, max_details: 50 }
  const add = useMutation({ mutationFn: (data: Record<string, unknown> & { filters?: Record<string, unknown> }) => {
    const payload = { ...data, filters: { ...editing?.filters, ...data.filters } }
    return editing ? api('/plans/' + editing.id, { method: 'PUT', body: JSON.stringify(payload) }) : post('/plans', payload)
  },
    onSuccess: () => { setVisible(false); form.resetFields(); cache.invalidateQueries({ queryKey: ['plans'] }) },
    onError: (e: Error) => message.error(e.message) })
  async function action(p: Plan, verb: string) {
    const actionKey = p.id + ':' + verb
    if (actionInFlight.current.has(actionKey)) return
    actionInFlight.current.add(actionKey)
    try {
      if (verb === 'run') await runRequests.current.post('/plans/' + p.id + '/run')
      else await post('/plans/' + p.id + '/' + verb)
      cache.invalidateQueries({ queryKey: ['plans'] }); if (verb === 'run') navigate('/tasks')
    }
    catch (e) { message.error((e as Error).message) }
    finally { actionInFlight.current.delete(actionKey) }
  }
  return <>
    <PageTitle eyebrow="MONITORING PLANS" title="监测计划" description="批量管理关注公司，底层按公司拆分任务、独立运行。"
      action={<Button type="primary" icon={<Plus size={16} />} onClick={() => { setEditing(null); form.resetFields(); setVisible(true) }}>新建计划</Button>} />
    <LoadError error={list.error} /><Panel title="计划列表"><Table rowKey="id" dataSource={list.data} loading={list.isPending} columns={[
      { title: '计划名称', dataIndex: 'name' },
      { title: '关注公司', dataIndex: 'company_ids', render: (v: string[]) => v.length + ' 家' },
      { title: '执行方式', render: (_, p: Plan) => p.enabled ? <Tag color="green">定时 · {p.schedule}</Tag> : <Tag>手动运行</Tag> },
      { title: '下次执行', dataIndex: 'next_run_at', render: (v: string) => formatDate(v) },
      { title: '操作', render: (_, p: Plan) => <Space><Button size="small" onClick={() => { setEditing(p); form.resetFields(); form.setFieldsValue({ ...p, filters: { ...defaultFilters, ...p.filters } }); setVisible(true) }}>编辑</Button><Button size="small" onClick={() => action(p, 'run')}>立即执行</Button>{p.enabled && <Button size="small" onClick={() => action(p, 'pause')}>暂停定时</Button>}</Space> },
    ]} /></Panel>
    <Modal title={editing ? '编辑招聘监测计划' : '新建招聘监测计划'} open={visible} onCancel={() => setVisible(false)} onOk={() => form.submit()} okText="保存计划" confirmLoading={add.isPending} width={680} styles={{ body: { maxHeight: 'calc(100vh - 240px)', overflowY: 'auto', paddingRight: 8 } }}>
      <Form layout="vertical" form={form} initialValues={{ enabled: false, schedule: '0 9 * * *', timezone: 'Asia/Shanghai', filters: defaultFilters }} onFinish={v => add.mutate(v)}>
        <Form.Item name="name" label="计划名称" rules={[{ required: true }]}><Input placeholder="例如：重点竞品招聘观察" /></Form.Item>
        <Form.Item name="company_ids" label="关注公司" rules={[{ required: true }]}><Select mode="multiple" options={companies.data?.map(c => ({ value: c.id, label: c.name }))} /></Form.Item>
        <Form.Item name={['filters', 'cities']} label="关注城市" extra="匹配任一城市；留空表示不限城市。" rules={[{ type: 'array', max: 50, message: '最多填写 50 个城市' }]}>
          <Select mode="tags" allowClear tokenSeparators={['，', ',', '、']} maxTagCount="responsive" placeholder="输入城市后按回车，可用逗号分隔" />
        </Form.Item>
        <Form.Item name={['filters', 'keywords']} label="岗位关键词" extra="岗位名称、正文或地点中匹配任一关键词；留空表示不限。" rules={[{ type: 'array', max: 50, message: '最多填写 50 个关键词' }]}>
          <Select mode="tags" allowClear tokenSeparators={['，', ',', '、']} maxTagCount="responsive" placeholder="例如：研发、产品经理" />
        </Form.Item>
        <Form.Item name={['filters', 'exclusions']} label="排除词" extra="匹配任一排除词的岗位不纳入本次范围。" rules={[{ type: 'array', max: 50, message: '最多填写 50 个排除词' }]}>
          <Select mode="tags" allowClear tokenSeparators={['，', ',', '、']} maxTagCount="responsive" placeholder="例如：实习" />
        </Form.Item>
        <Form.Item name={['filters', 'sources']} label="招聘来源" extra="留空使用各公司已绑定且启用的全部来源。选择来源不会自动添加入口；需登录的来源仍等待人工登录。">
          <Select mode="multiple" allowClear placeholder="全部已启用来源" options={['careers', 'boss', 'liepin', 'zhaopin', '51job'].map(value => ({ value, label: labels[value] }))} />
        </Form.Item>
        <Space align="start" size="large" wrap>
          <Form.Item name={['filters', 'max_pages']} label="采集页数上限（1–100）" rules={[{ required: true, type: 'integer', min: 1, max: 100, message: '请填写 1 至 100 的整数' }]}>
            <InputNumber precision={0} changeOnBlur={false} aria-valuemin={1} aria-valuemax={100} style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name={['filters', 'max_details']} label="岗位详情上限（1–500）" rules={[{ required: true, type: 'integer', min: 1, max: 500, message: '请填写 1 至 500 的整数' }]}>
            <InputNumber precision={0} changeOnBlur={false} aria-valuemin={1} aria-valuemax={500} style={{ width: 160 }} />
          </Form.Item>
        </Space>
        <p className="subtle">各来源分别使用采集预算，并受系统上限与来源适配能力限制；达到预算时会说明覆盖不足。</p>
        <Form.Item name="enabled" label="启用定时执行" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="schedule" label="定时表达式" extra="默认每天 09:00，时区为上海"><Input /></Form.Item>
        <Form.Item name="timezone" label="时区"><Select options={[{ value: 'Asia/Shanghai', label: '中国标准时间' }, { value: 'UTC', label: 'UTC' }]} /></Form.Item>
      </Form>
    </Modal>
  </>
}

function Reports() {
  const list = useQuery({ queryKey: ['reports'], queryFn: () => api<Report[]>('/reports'), refetchInterval: 10000 })
  const [selected, setSelected] = useState<Report | null>(null)
  const visibleReport = selected && (list.data?.find(item => item.id === selected.id) || selected)
  return <>
    <PageTitle eyebrow="ANALYSIS & EVIDENCE" title="分析报告" description="统计由程序计算，模型归纳观察；每份简报保留可回溯的岗位证据。" />
    <LoadError error={list.error} /><Panel title="报告记录"><Table rowKey="id" dataSource={list.data} loading={list.isPending} columns={[
      { title: '报告', dataIndex: 'title', render: (title, report: Report) => <Button type="link" onClick={() => setSelected(report)}>{title}</Button> },
      { title: '模块', dataIndex: 'domain', render: (v: string) => ({ recruitment: '招聘情报', sentiment: '公司舆情', news: 'HR 资讯' }[v] || v) },
      { title: '模型', dataIndex: 'model', render: (v: string | null) => v || '—' },
      { title: '正文状态', render: (_, report: Report) => <ReportRetentionState clearedAt={report.cleared_at} hasContent={!!report.content} /> },
      { title: '生成时间', dataIndex: 'created_at', render: (v: string) => formatDate(v) },
    ]} /></Panel>
    <Drawer title={visibleReport?.title} open={!!selected} onClose={() => setSelected(null)} width={760}>
      <p className="subtle">{formatDate(visibleReport?.created_at)} · {visibleReport?.model}</p>
      {visibleReport?.cleared_at ? <ReportRetentionNotice clearedAt={visibleReport.cleared_at} />
        : visibleReport?.content ? <p className="job-description">{visibleReport.content}</p> : <Alert type="info" message="报告暂无可显示的正文，请查看保留的来源证据。" />}
      <h3>原文证据</h3>{visibleReport?.evidence.map((e, i) => <div className="source-result" key={(e.job_id || e.item_id || '') + i}>
        <a className="external-link" href={e.source_url || e.url} target="_blank" rel="noreferrer">{e.title || '查看原文'}<ExternalLink size={13} /></a>
      </div>)}
    </Drawer>
  </>
}

function SettingsPage() {
  const { message } = AntApp.useApp(), cache = useQueryClient(), [discoveryForm] = Form.useForm()
  const [searchEditing, setSearchEditing] = useState(false), [saving, setSaving] = useState(false)
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<{ role: string }>('/auth/me') })
  const isAdmin = me.data?.role === 'admin'
  const info = useQuery({ queryKey: ['system'], queryFn: () => api<SystemStatus>('/system/status') })
  const bridge = useQuery({ queryKey: ['sentiment', 'status'], queryFn: () => api<{ bettafish: { status: string; reason?: string } }>('/sentiment/status') })
  const discovery = useQuery({ queryKey: ['discovery-settings'], queryFn: () => api<{ configured: boolean; config: { endpoint_url: string; enabled: boolean; max_results: number } | null }>('/settings/recruitment-discovery'), enabled: isAdmin })
  const data = info.data
  return <>
    <PageTitle eyebrow="WORKSPACE SETTINGS" title="运行设置" description="查看连接状态与模型调用预算。密钥不会展示在页面中。" />
    <LoadError error={info.error} />
    <LoadError error={bridge.error} />
    <div className="two-columns"><ModelSettingsPanel isAdmin={isAdmin} summary={data?.llm} />
    <Panel title="平台连接"><Descriptions column={1} items={[
      { label: '数据库', children: status(data?.database || 'unknown') },
      { label: '显示时区', children: data?.timezone || '—' },
      { label: 'BettaFish', children: <>{bridge.data ? stateTag(bridge.data.bettafish.status) : <Tag>{bridge.error ? '状态读取失败' : '查询中'}</Tag>}{bridge.data?.bettafish.reason && <div className="subtle">{bridge.data.bettafish.reason}</div>}</> },
    ]} /><p className="subtle">平台仍在按实施计划建设。未接入能力会保持明确的待配置或未完成状态。</p></Panel></div>
    <div style={{ height: 22 }} /><Panel title="公司名称搜索服务" extra={isAdmin && <Button onClick={() => { discoveryForm.setFieldsValue(discovery.data?.config || { enabled: true, max_results: 5 }); setSearchEditing(true) }}>配置搜索</Button>}>
      {isAdmin ? <><LoadError error={discovery.error} /><Descriptions column={1} items={[{ label: '状态', children: discovery.data?.config?.enabled ? '已启用' : '未启用' }, { label: '搜索服务', children: discovery.data?.config?.endpoint_url || '未配置' }]} /></> : <p className="subtle">搜索连接由管理员管理。</p>}
      <p className="subtle">填写公司官网后可直接发现同站点的招聘入口。只有公司名称时，使用支持 JSON 响应的 SearXNG 搜索服务生成待核对候选。</p>
    </Panel>
    <Modal title="配置招聘入口搜索" open={searchEditing} onCancel={() => setSearchEditing(false)} onOk={() => discoveryForm.submit()} confirmLoading={saving} okText="保存">
      <Form form={discoveryForm} layout="vertical" onFinish={async values => { setSaving(true); try { await api('/settings/recruitment-discovery', { method: 'PUT', body: JSON.stringify(values) }); cache.invalidateQueries({ queryKey: ['discovery-settings'] }); setSearchEditing(false) } catch (error) { message.error((error as Error).message) } finally { setSaving(false) } }}>
        <Form.Item name="endpoint_url" label="SearXNG /search 地址" rules={[{ required: true }, { validator: async (_, value: string) => {
          if (!value) return
          try {
            const endpoint = new URL(value)
            if (['http:', 'https:'].includes(endpoint.protocol) && !endpoint.username && !endpoint.password) return
          } catch { /* Surface the same actionable message for malformed URLs. */ }
          throw new Error('请输入不含账号密码的完整 HTTP/HTTPS 地址')
        } }]}><Input placeholder="https://search.example.com/search" /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
        <Form.Item name="max_results" label="每次结果上限"><Select options={[3, 5, 10].map(value => ({ value, label: value + ' 条' }))} /></Form.Item>
      </Form>
    </Modal>
    <RetentionSettings />
    <AdminSettings />
  </>
}

export default function App() {
  const cache = useQueryClient()
  const user = useQuery({ queryKey: ['me'], queryFn: () => api<{ username: string; role: string }>('/auth/me') })
  if (user.isPending) return <div className="full-loading"><Spin size="large" /></div>
  if (user.error instanceof ApiError && user.error.status === 401) {
    return <Login onSuccess={() => cache.invalidateQueries({ queryKey: ['me'] })} />
  }
  if (user.error) return <div className="full-loading"><Alert type="error" message="工作台暂时无法连接" description={user.error.message} action={<Button onClick={() => user.refetch()}>重试</Button>} /></div>
  const nav = [
    ['/', '情报总览', LayoutDashboard], ['/companies', '关注公司', Building2],
    ['/devices', '采集设备', Database],
    ['/jobs', '招聘情报', BriefcaseBusiness], ['/sources', '数据源', Database],
    ['/analytics', '招聘趋势与比较', ChartNoAxesCombined],
    ['/sentiment', '公司整体舆情', MessageSquare],
    ['/news', 'HR 行业资讯', Newspaper],
    ['/plans', '监测计划', CalendarClock], ['/tasks', '任务中心', Activity],
    ['/reports', '分析报告', FileText],
    ['/assistant', '情报助手', Bot],
    ['/notifications', '通知设置', Bell],
    ['/maintenance', '适配器维护', Wrench],
    ['/settings', '运行设置', Settings2],
  ] as const
  return <div className="app-shell"><aside className="sidebar">
    <div className="brand"><Radar size={29} /><span>观势<span className="brand-dot">.</span></span></div>
    <div className="sidebar-label">企业外部情报工作台</div>
    <nav>{nav.map(([path, label, Icon]) => <NavLink key={path} to={path} end={path === '/'} aria-label={label} title={label}><Icon size={18} /><span>{label}</span></NavLink>)}</nav>
    <div className="sidebar-bottom"><div className="workspace-badge"><span className="status-dot" /><div>内部工作空间<small>数据保留来源，可追溯变化</small></div></div>
      <button className="logout" onClick={async () => { await post('/auth/logout'); cache.clear(); cache.invalidateQueries({ queryKey: ['me'] }) }}><LogOut size={15} />退出登录</button></div>
  </aside><div className="main-shell"><header><div><span className="breadcrumb">工作空间</span><ChevronRight size={13} /><span>企业情报</span></div><div className="header-user"><span>{beijingCalendarDate()}</span><span className="avatar">{user.data?.username.slice(0, 1).toUpperCase()}</span></div></header>
    <main><Suspense fallback={<Spin />}><Routes>
      <Route path="/" element={<Overview />} /><Route path="/companies" element={<Companies />} />
      <Route path="/companies/:companyId" element={<CompanyWorkspace />} />
      <Route path="/sources" element={<Sources />} /><Route path="/tasks" element={<Tasks />} />
      <Route path="/devices" element={<DevicesPage />} />
      <Route path="/jobs" element={<Jobs />} /><Route path="/plans" element={<Plans />} />
      <Route path="/reports" element={<Reports />} />
      <Route path="/sentiment" element={<SentimentPage />} />
      <Route path="/notifications" element={<NotificationsPage />} />
      <Route path="/news" element={<NewsPage />} />
      <Route path="/analytics" element={<RecruitmentAnalytics />} />
      <Route path="/assistant" element={<AssistantPage />} />
      <Route path="/maintenance" element={<MaintenancePage />} />
      <Route path="/settings" element={<SettingsPage />} /><Route path="*" element={<Navigate to="/" />} />
    </Routes></Suspense></main>
  </div></div>
}
