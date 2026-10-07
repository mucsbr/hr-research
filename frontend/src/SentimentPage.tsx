import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { App, Alert, Button, Descriptions, Drawer, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag } from 'antd'
import { api, post } from './api'
import type { SourceAccount } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText, stateTag } from './ModuleUI'
import { ReportRetentionNotice, ReportRetentionState } from './RetentionSettings'

interface Subject { id: string; name: string; type: string; aliases: string[]; keywords: string[]; exclusions: string[]; active: boolean }
interface Source { id: string; subject_id: string; name: string; kind: string; entry_urls: string[]; enabled: boolean; health: string; account_id: string | null; config?: { attribution_basis?: string }; latest_run?: SourceRun | null }
interface SourceRun { id: string; platform: string; status: string; coverage: string; error_message: string | null; limitations: string[]; counts: { attempted_pages?: number; successful_pages?: number; detail_pages?: number; pending_pages?: number; search_queries?: { term: string; status: string }[]; pending_attribution?: { url: string; title: string; reason?: string }[] } }
interface Item { id: string; title: string; subject_id: string; platform: string; category: string; polarity: string; content: string; url: string; published_at: string | null; last_seen_at: string; is_read: boolean; is_favorite: boolean; is_ignored: boolean; notes: string; classification: { category_hits?: string[]; unverified?: boolean; content_extent?: string; attribution_basis?: string; source_page_url?: string } }
interface Run { id: string; kind: string; status: string; created_at: string; error_message: string | null; config_snapshot: { subject?: { name: string } }; sources?: SourceRun[] }
interface Report { id: string; title: string; content: string; created_at: string; model: string | null; cleared_at?: string | null; retention?: Record<string, unknown>; evidence: { item_id?: string; title?: string; url?: string; source_url?: string }[]; coverage: { engine?: string; limitations?: string[]; source_runs?: unknown[] } }
interface ModuleStatus { bettafish: { status: string; message?: string; reason?: string }; limitations: string[] }
const platforms: Record<string, string> = { public_web: '公开网页 / 论坛', news: '新闻', xhs: '小红书', dy: '抖音', wb: '微博', zhihu: '知乎', tieba: '百度贴吧', bilibili: '哔哩哔哩' }
const categories: Record<string, string> = { customer_experience: '客户评价', product_service: '产品服务', business: '公司经营', employer: '雇主口碑', partners: '合作伙伴', other: '其他' }
const subjectKinds: Record<string, string> = { own_company: '自有公司', brand: '品牌', product: '产品 / 服务' }
const extents: Record<string, string> = { search_excerpt: '搜索摘录', detail_body: '详情正文', source_text: '页面文本', video_description: '视频文案' }
const attributionNames: Record<string, string> = { entity_match: '名称或别名匹配', user_import: '人工指定归属', user_selected_url: '用户选定单篇' }
const runDefaults = { mode: 'collect', source_ids: [], time_from: '', time_to: '', max_pages: 2, render_mode: 'auto', analysis_topic: '整体声誉' }

function scopeTime(value?: string) {
  if (!value) return null
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return NaN
  const stamp = Date.parse(value + ':00+08:00')
  return Number.isFinite(stamp) && new Date(stamp + 8 * 3600000).toISOString().slice(0, 16) === value ? stamp : NaN
}

export default function SentimentPage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const [subjectId, setSubjectId] = useState<string>(), [tab, setTab] = useState('items'), [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState(''), [platform, setPlatform] = useState<string>(), [favorite, setFavorite] = useState(false)
  const [ignored, setIgnored] = useState(false), [unread, setUnread] = useState(false)
  const [subjectOpen, setSubjectOpen] = useState(false), [sourceOpen, setSourceOpen] = useState(false), [runOpen, setRunOpen] = useState(false), [importOpen, setImportOpen] = useState(false)
  const [editSubject, setEditSubject] = useState<Subject | null>(null), [editSource, setEditSource] = useState<Source | null>(null)
  const [item, setItem] = useState<Item | null>(null), [runId, setRunId] = useState<string | null>(null), [report, setReport] = useState<Report | null>(null)
  const [saving, setSaving] = useState(false), [notes, setNotes] = useState('')
  const [subjectForm] = Form.useForm(), [sourceForm] = Form.useForm(), [runForm] = Form.useForm(), [importForm] = Form.useForm()
  const sourceKind = Form.useWatch('kind', sourceForm) || 'public_web'
  const runMode = Form.useWatch('mode', runForm) || 'collect'
  const runSubjectId = Form.useWatch('subject_id', runForm)
  const subjects = useQuery({ queryKey: ['sentiment', 'subjects'], queryFn: () => api<Subject[]>('/sentiment/subjects') })
  const module = useQuery({ queryKey: ['sentiment', 'status'], queryFn: () => api<ModuleStatus>('/sentiment/status') })
  const accounts = useQuery({ queryKey: ['source-accounts'], queryFn: () => api<SourceAccount[]>('/source-accounts') })
  const suffix = subjectId ? '?subject_id=' + subjectId : ''
  const sources = useQuery({ queryKey: ['sentiment', 'sources', subjectId], queryFn: () => api<Source[]>('/sentiment/sources' + suffix) })
  const runSources = useQuery({ queryKey: ['sentiment', 'run-sources', runSubjectId],
    queryFn: () => api<Source[]>('/sentiment/sources?subject_id=' + runSubjectId), enabled: runOpen && !!runSubjectId })
  const runs = useQuery({ queryKey: ['sentiment', 'runs', subjectId], queryFn: () => api<Run[]>('/sentiment/runs' + suffix), refetchInterval: 6000 })
  const runDetail = useQuery({ queryKey: ['sentiment', 'run', runId], queryFn: () => api<Run>('/sentiment/runs/' + runId), enabled: !!runId, refetchInterval: runId ? 6000 : false })
  const reports = useQuery({ queryKey: ['sentiment', 'reports', subjectId], queryFn: () => api<Report[]>('/sentiment/reports' + suffix), refetchInterval: 10000 })
  const visibleReport = report && (reports.data?.find(item => item.id === report.id) || report)
  const params = new URLSearchParams({ limit: '20', offset: String((page - 1) * 20), is_ignored: String(ignored) })
  if (subjectId) params.set('subject_id', subjectId)
  if (platform) params.set('platform', platform)
  if (keyword) params.set('q', keyword)
  if (favorite) params.set('is_favorite', 'true')
  if (unread) params.set('is_read', 'false')
  const items = useQuery({ queryKey: ['sentiment', 'items', params.toString()], queryFn: () => api<{ items: Item[]; total: number }>('/sentiment/items?' + params), refetchInterval: 15000 })
  const names = Object.fromEntries((subjects.data || []).map(s => [s.id, s.name]))
  const enabledRunSources = (runSources.data || []).filter(s => s.enabled && s.subject_id === runSubjectId)
  function closeRun() { setRunOpen(false); runForm.resetFields() }
  function openRun() {
    runForm.resetFields()
    runForm.setFieldsValue({ ...runDefaults, subject_id: subjects.data?.some(s => s.id === subjectId && s.active) ? subjectId : undefined })
    setRunOpen(true)
  }
  function refresh() { cache.invalidateQueries({ queryKey: ['sentiment'] }); cache.invalidateQueries({ queryKey: ['all-subjects'] }) }
  async function save(path: string, values: unknown, method = 'POST', done?: () => void) {
    setSaving(true)
    try { await api(path, { method, body: JSON.stringify(values) }); refresh(); done?.(); message.success('已保存') }
    catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
  }
  async function patchItem(selected: Item, values: Partial<Item>) {
    try {
      const updated = await api<Item>('/sentiment/items/' + selected.id, { method: 'PATCH', body: JSON.stringify(values) })
      refresh(); if (item?.id === updated.id) setItem(updated)
    } catch (error) { message.error((error as Error).message) }
  }
  async function retry(run: Run) {
    try { await post('/sentiment/runs/' + run.id + '/retry'); refresh(); message.success('已创建独立重试任务') }
    catch (error) { message.error((error as Error).message) }
  }
  return <>
    <ModuleTitle title="公司整体舆情" description="跟踪客户评价、产品服务与公司经营，保留原文、来源覆盖和观察记录。"
      action={<Space><Button onClick={() => setImportOpen(true)}>导入资料</Button><Button type="primary" onClick={openRun}>发起任务</Button></Space>} />
    <ModuleError error={subjects.error || sources.error || items.error || runs.error || reports.error || module.error} />
    <div className="filter-bar"><Select allowClear placeholder="全部关注对象" value={subjectId} style={{ width: 240 }} onChange={value => { setSubjectId(value); setPage(1) }} options={subjects.data?.map(s => ({ value: s.id, label: s.name }))} />
      <Link to="/sources">管理平台登录</Link></div>
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'items', label: '舆情资料', children: <ModulePanel title="原文与线索" extra={<span className="subtle">当前筛选 {items.data?.total ?? '—'} 条</span>}>
        <div className="filter-bar"><Input.Search allowClear placeholder="搜索正文" onSearch={v => { setKeyword(v); setPage(1) }} style={{ width: 240 }} />
          <Select allowClear placeholder="全部来源" style={{ width: 150 }} onChange={v => { setPlatform(v); setPage(1) }} options={Object.entries(platforms).map(([value, label]) => ({ value, label }))} />
          <Space>只看收藏<Switch checked={favorite} onChange={v => { setFavorite(v); setPage(1) }} size="small" />未读<Switch checked={unread} onChange={v => { setUnread(v); setPage(1) }} size="small" />查看忽略<Switch checked={ignored} onChange={v => { setIgnored(v); setPage(1) }} size="small" /></Space>
        </div>
        <Table rowKey="id" dataSource={items.data?.items} loading={items.isPending} pagination={{ current: page, pageSize: 20, total: items.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
          { title: '内容', render: (_, i: Item) => <Button type="link" style={{ textAlign: 'left', whiteSpace: 'normal', height: 'auto' }} onClick={() => { setItem(i); setNotes(i.notes); if (!i.is_read) patchItem(i, { is_read: true }) }}>{!i.is_read && '● '}{i.title || i.content.slice(0, 60)}</Button> },
          { title: '对象', dataIndex: 'subject_id', render: v => names[v] || '—' },
          { title: '来源', dataIndex: 'platform', render: v => platforms[v] || v },
          { title: '分类', dataIndex: 'category', render: v => categories[v] || v },
          { title: '线索倾向', dataIndex: 'polarity', render: stateTag },
          { title: '资料形式', render: (_, i: Item) => <Tag>{extents[i.classification.content_extent || 'source_text'] || i.classification.content_extent}</Tag> },
          { title: '最近观察', dataIndex: 'last_seen_at', render: dateText },
          { title: '收藏', render: (_, i: Item) => <Button size="small" type={i.is_favorite ? 'primary' : 'default'} onClick={() => patchItem(i, { is_favorite: !i.is_favorite })}>{i.is_favorite ? '已收藏' : '收藏'}</Button> },
        ]} />
        <div className="note-box">分类和情绪是基于已采集样本的线索，未经事实核实。未采到内容不代表没有舆情。</div>
      </ModulePanel> },
      { key: 'subjects', label: '关注对象', children: <ModulePanel title="公司、品牌与产品" extra={<Button onClick={() => { setEditSubject(null); subjectForm.resetFields(); setSubjectOpen(true) }}>新增对象</Button>}>
        <Table rowKey="id" dataSource={subjects.data} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '类型', dataIndex: 'type', render: v => subjectKinds[v] || v },
          { title: '关键词', dataIndex: 'keywords', render: (v: string[]) => v.map(k => <Tag key={k}>{k}</Tag>) },
          { title: '启用', render: (_, s: Subject) => <Switch checked={s.active} onChange={active => save('/sentiment/subjects/' + s.id, { active }, 'PATCH')} /> },
          { title: '操作', render: (_, s: Subject) => <Button onClick={() => { setEditSubject(s); subjectForm.setFieldsValue(s); setSubjectOpen(true) }}>编辑</Button> },
        ]} />
      </ModulePanel> },
      { key: 'sources', label: '来源覆盖', children: <ModulePanel title="采集入口" extra={<Button onClick={() => { setEditSource(null); sourceForm.resetFields(); sourceForm.setFieldValue('subject_id', subjectId); setSourceOpen(true) }}>新增来源</Button>}>
        <Table rowKey="id" dataSource={sources.data} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '对象', dataIndex: 'subject_id', render: v => names[v] || '—' },
          { title: '平台', dataIndex: 'kind', render: v => platforms[v] || v },
          { title: '状态', dataIndex: 'health', render: stateTag },
          { title: '最近结果', render: (_, s: Source) => s.latest_run?.error_message || s.latest_run?.limitations?.join('；') || '尚未采集' },
          { title: '启用', render: (_, s: Source) => <Switch checked={s.enabled} onChange={enabled => save('/sentiment/sources/' + s.id, { enabled }, 'PATCH')} /> },
          { title: '操作', render: (_, s: Source) => <Button onClick={() => { setEditSource(s); sourceForm.setFieldsValue({ ...s, urls: s.entry_urls.join('\n'), attribution_basis: s.config?.attribution_basis || 'entity_match' }); setSourceOpen(true) }}>编辑</Button> },
        ]} />
      </ModulePanel> },
      { key: 'runs', label: '采集与分析任务', children: <ModulePanel title="独立任务记录">
        <Table rowKey="id" dataSource={runs.data} columns={[
          { title: '对象', render: (_, r: Run) => <Button type="link" onClick={() => setRunId(r.id)}>{r.config_snapshot.subject?.name || '舆情任务'}</Button> },
          { title: '任务', dataIndex: 'kind', render: v => ({ sentiment_collect: '采集', sentiment_collection: '采集', sentiment_analyze: '模型分析', sentiment_analysis: '模型分析', sentiment_bettafish: 'BettaFish 报告', sentiment_import: '导入' }[v as string] || v) },
          { title: '状态', dataIndex: 'status', render: stateTag }, { title: '时间', dataIndex: 'created_at', render: dateText },
          { title: '原因', dataIndex: 'error_message' },
          { title: '操作', render: (_, r: Run) => <Space><Button size="small" onClick={() => setRunId(r.id)}>覆盖详情</Button>{!['queued', 'running', 'succeeded'].includes(r.status) && r.kind !== 'sentiment_import' && <Button size="small" onClick={() => retry(r)}>重试</Button>}{['queued', 'running'].includes(r.status) && <Button size="small" onClick={() => save('/tasks/' + r.id + '/cancel', {})}>取消</Button>}</Space> },
        ]} />
      </ModulePanel> },
      { key: 'reports', label: '舆情报告', children: <ModulePanel title="分析与证据">
        <div className="note-box">BettaFish 状态：{stateTag(module.data?.bettafish.status || 'unknown')} {module.data?.bettafish.message || module.data?.bettafish.reason || ''}</div>
        <Table rowKey="id" dataSource={reports.data} columns={[
          { title: '报告', dataIndex: 'title', render: (title, r: Report) => <Button type="link" onClick={() => setReport(r)}>{title}</Button> },
          { title: '分析方式', render: (_, r: Report) => r.model || '规则摘要' }, { title: '时间', dataIndex: 'created_at', render: dateText },
          { title: '正文状态', render: (_, r: Report) => <ReportRetentionState clearedAt={r.cleared_at} hasContent={!!r.content} /> },
        ]} />
      </ModulePanel> },
    ]} />
    <Modal title={editSubject ? '编辑关注对象' : '新增关注对象'} open={subjectOpen} onCancel={() => setSubjectOpen(false)} onOk={() => subjectForm.submit()} confirmLoading={saving} okText="保存">
      <Form form={subjectForm} layout="vertical" initialValues={{ type: 'own_company', aliases: [], keywords: [], exclusions: [], active: true }} onFinish={v => { const { type, ...patch } = v; save('/sentiment/subjects' + (editSubject ? '/' + editSubject.id : ''), editSubject ? patch : { ...patch, type }, editSubject ? 'PATCH' : 'POST', () => setSubjectOpen(false)) }}>
        <Form.Item name="name" label="名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="type" label="类型"><Select disabled={!!editSubject} options={Object.entries(subjectKinds).map(([value, label]) => ({ value, label }))} /></Form.Item>
        {(['aliases', 'keywords', 'exclusions'] as const).map((name, index) => <Form.Item key={name} name={name} label={['别名', '匹配关键词', '排除关键词'][index]}><Select mode="tags" tokenSeparators={['，', ',']} /></Form.Item>)}
        <Form.Item name="active" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Modal title={editSource ? '编辑采集来源' : '新增采集来源'} open={sourceOpen} onCancel={() => setSourceOpen(false)} onOk={() => sourceForm.submit()} confirmLoading={saving} okText="保存">
      <Form form={sourceForm} layout="vertical" initialValues={{ kind: 'public_web', enabled: true, attribution_basis: 'entity_match' }} onFinish={v => {
        const { urls, ...base } = v; const entry_urls = String(urls || '').split('\n').map(u => u.trim()).filter(Boolean)
        const values = editSource ? { name: base.name, entry_urls, enabled: base.enabled, account_id: base.account_id || null, attribution_basis: base.attribution_basis } : { ...base, account_id: base.account_id || null, entry_urls }
        save('/sentiment/sources' + (editSource ? '/' + editSource.id : ''), values, editSource ? 'PATCH' : 'POST', () => setSourceOpen(false))
      }}>
        <Form.Item name="subject_id" label="关注对象" rules={[{ required: true }]}><Select disabled={!!editSource} options={subjects.data?.map(s => ({ value: s.id, label: s.name }))} /></Form.Item>
        <Form.Item name="name" label="来源名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="kind" label="平台"><Select disabled={!!editSource} options={Object.entries(platforms).map(([value, label]) => ({ value, label }))} /></Form.Item>
        <Form.Item name="urls" label="原文或可检索页面（每行一个 URL）" extra="每次任务有页数上限；网页有登录墙时会提示人工登录。"><Input.TextArea rows={4} /></Form.Item>
        <Form.Item name="account_id" label="使用账号"><Select allowClear options={accounts.data?.filter(a => a.kind === sourceKind).map(a => ({ value: a.id, label: a.name }))} /></Form.Item>
        <Form.Item name="attribution_basis" label="归属依据" extra="主题词不会单独作为公司归属依据；用户指定仅适用于明确单篇链接，不适用于首页或搜索。"><Select options={[{ value: 'entity_match', label: '正文须命中公司/品牌名称或别名' }, { value: 'user_selected_url', label: '我已选定这些单篇原文归属于该对象' }]} /></Form.Item>
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Modal title="发起舆情任务" open={runOpen} onCancel={closeRun} onOk={() => runForm.submit()} confirmLoading={saving} okText="开始"
      width={680} okButtonProps={{ disabled: !!runSubjectId && (runSources.isPending || !!runSources.error) }}>
      <Form form={runForm} layout="vertical" initialValues={runDefaults} onFinish={v => {
        const { render_mode, time_from, time_to, ...values } = v
        const start = scopeTime(time_from), end = scopeTime(time_to)
        save('/sentiment/runs', { ...values, source_ids: values.source_ids || [],
          time_from: start === null ? null : new Date(start).toISOString(), time_to: end === null ? null : new Date(end).toISOString(),
          max_pages: values.mode === 'collect' ? values.max_pages : runDefaults.max_pages,
          analysis_topic: values.mode === 'collect' ? runDefaults.analysis_topic : values.analysis_topic,
          render: values.mode !== 'collect' || render_mode === 'auto' ? null : render_mode === 'browser' }, 'POST', () => { closeRun(); setTab('runs') })
      }}>
        <Form.Item name="subject_id" label="关注对象" rules={[{ required: true, message: '请选择关注对象' }]}><Select onChange={() => runForm.setFieldValue('source_ids', [])} options={subjects.data?.filter(s => s.active).map(s => ({ value: s.id, label: s.name }))} /></Form.Item>
        <Form.Item name="mode" label="任务类型"><Select options={[{ value: 'collect', label: '采集并生成规则摘要' }, { value: 'analyze', label: '分析已有证据（使用模型预算）' }, { value: 'bettafish', label: 'BettaFish 报告（需完成配置）' }]} /></Form.Item>
        <ModuleError error={runSources.error} />
        <Form.Item name="source_ids" label="本次来源" dependencies={['subject_id', 'mode']} extra={runMode === 'collect'
          ? '留空采集该对象全部启用来源；最多选择20个。' : '选择后仅分析曾由这些来源采得的条目；留空包含该对象全部已有资料，包括人工导入。'}
          rules={[{ type: 'array', max: 20, message: '一次最多选择20个来源' }, { validator: (_, ids: string[] = []) => {
            if (runSubjectId && !runSources.isPending && !runSources.error && runMode === 'collect' && !enabledRunSources.length) return Promise.reject(new Error('该对象尚无启用来源，请先添加来源'))
            if (ids.some(id => !enabledRunSources.some(source => source.id === id))) return Promise.reject(new Error('来源不属于当前对象或已停用，请重新选择'))
            if (!ids.length && enabledRunSources.length > 20) return Promise.reject(new Error('启用来源超过20个，请选择本次来源'))
            return Promise.resolve()
          } }]}>
          <Select mode="multiple" allowClear disabled={!runSubjectId} loading={runSources.isPending && !!runSubjectId}
            placeholder="留空使用上述默认范围" options={enabledRunSources.map(s => ({ value: s.id, label: `${s.name} · ${platforms[s.kind] || s.kind}` }))} />
        </Form.Item>
        <Space align="start" style={{ width: '100%' }}>
          <Form.Item name="time_from" label="开始时间（北京时间）" dependencies={['time_to']} rules={[{ validator: (_, value) => {
            const stamp = scopeTime(value)
            return stamp !== null && !Number.isFinite(stamp) ? Promise.reject(new Error('请输入有效的北京时间')) : Promise.resolve()
          } }]}><Input type="datetime-local" step={60} /></Form.Item>
          <Form.Item name="time_to" label="结束时间（北京时间）" dependencies={['time_from']} rules={[{ validator: (_, value) => {
            const start = scopeTime(runForm.getFieldValue('time_from')), end = scopeTime(value)
            if (end !== null && !Number.isFinite(end)) return Promise.reject(new Error('请输入有效的北京时间'))
            if (start !== null && end !== null && Number.isFinite(start) && start > end) return Promise.reject(new Error('结束时间不能早于开始时间'))
            return Promise.resolve()
          } }]}><Input type="datetime-local" step={60} /></Form.Item>
        </Space>
        <p className="subtle">按北京时间（UTC+8）输入并转换为UTC提交；可只填一端，起止时间均包含，留空不限时间。</p>
        <Alert style={{ marginBottom: 16 }} type="info" message={runMode === 'collect'
          ? '时间范围用于过滤已读取内容的已知发布时间；未知发布时间仍保留并标记。它不保证平台历史搜索覆盖。'
          : '仅分析已有资料，最多取8条样本；优先按发布时间筛选，未知时按首次发现时间筛选，不会因此重新采集。'} />
        {runMode === 'collect' ? <><Form.Item name="max_pages" label="每来源总页面预算" extra="包括搜索、详情与下一页。未查关键词会在覆盖详情列明，后续轮次继续。"
          rules={[{ required: true, type: 'integer', min: 1, max: 5, message: '页面预算须为1至5的整数' }]}><InputNumber aria-valuemin={1} aria-valuemax={5} changeOnBlur={false} /></Form.Item><Form.Item name="render_mode" label="页面读取方式"><Select options={[{ value: 'auto', label: '按平台自动选择' }, { value: 'browser', label: '使用浏览器渲染' }, { value: 'http', label: '匿名来源只用 HTTP' }]} /></Form.Item></> : <Form.Item name="analysis_topic" label="分析主题" rules={[{ required: true, whitespace: true, max: 300, message: '请填写1至300字的分析主题' }]}><Input maxLength={300} /></Form.Item>}
        {runMode === 'bettafish' && <p className="subtle">默认报告至少需要 3 次模型请求。运行前检查已知必要预算，超限时不会自动提高额度。</p>}
      </Form>
    </Modal>
    <Modal title="导入原始舆情资料" open={importOpen} onCancel={() => setImportOpen(false)} onOk={() => importForm.submit()} confirmLoading={saving} okText="导入">
      <Form form={importForm} layout="vertical" initialValues={{ note: '人工导入' }} onFinish={v => {
        try { const parsed: unknown = JSON.parse(v.items); save('/sentiment/imports', { subject_id: v.subject_id, note: v.note, items: parsed }, 'POST', () => { setImportOpen(false); setTab('items') }) }
        catch { message.error('请输入合法的 JSON 数组') }
      }}>
        <Form.Item name="subject_id" label="关注对象" rules={[{ required: true }]}><Select options={subjects.data?.map(s => ({ value: s.id, label: s.name }))} /></Form.Item>
        <Form.Item name="items" label="原文 JSON 数组" rules={[{ required: true }]} extra={'每条包含 platform、url、title、content；可选 published_at（含时区），单次最多 100 条。'}><Input.TextArea rows={8} placeholder={'[{"platform":"public_web","url":"https://…","title":"原文标题","content":"原文内容"}]'} /></Form.Item>
        <Form.Item name="note" label="导入备注"><Input /></Form.Item>
      </Form>
    </Modal>
    <Drawer title={item?.title || '原文详情'} open={!!item} onClose={() => setItem(null)} width={720}>
      {item && <><Space>{stateTag(item.polarity)}<Tag>{categories[item.category] || item.category}</Tag><a href={item.url} target="_blank" rel="noreferrer">查看原文</a></Space>
        <Descriptions column={1} items={[{ label: '发布时间', children: dateText(item.published_at) }, { label: '最近观察', children: dateText(item.last_seen_at) }, { label: '资料形式', children: extents[item.classification.content_extent || 'source_text'] || '未标注' }, { label: '归属依据', children: attributionNames[item.classification.attribution_basis || 'entity_match'] || '未标注' }]} />
        {item.classification.content_extent === 'search_excerpt' && <Alert type="info" message="此条为搜索结果摘录，尚未读取完整详情。" />}
        {item.classification.content_extent === 'video_description' && <Alert type="info" message="此条仅为可读视频文案，不包含音频转写或画面分析。" />}
        <p className="job-description">{item.content}</p><div className="note-box">分类命中：{item.classification.category_hits?.join('、') || '无明确关键词'}。该记录是外部观点，未独立核实。</div>
        <Space><Button onClick={() => patchItem(item, { is_favorite: !item.is_favorite })}>{item.is_favorite ? '取消收藏' : '收藏'}</Button><Button onClick={() => patchItem(item, { is_ignored: !item.is_ignored })}>{item.is_ignored ? '恢复展示' : '忽略此条'}</Button></Space>
        <h3>内部备注</h3><Input.TextArea rows={4} value={notes} onChange={e => setNotes(e.target.value)} /><Button style={{ marginTop: 12 }} onClick={() => patchItem(item, { notes })}>保存备注</Button>
      </>}
    </Drawer>
    <Drawer title="来源覆盖与任务状态" open={!!runId} onClose={() => setRunId(null)} width={680}>
      <ModuleError error={runDetail.error} />{runDetail.data && <>{stateTag(runDetail.data.status)}{runDetail.data.error_message && <Alert type="warning" message={runDetail.data.error_message} />}
        {runDetail.data.sources?.map(s => <div className="source-result" key={s.id}><Space><strong>{platforms[s.platform] || s.platform}</strong>{stateTag(s.status)}<Tag>{s.coverage}</Tag></Space><p>{s.error_message}</p>{s.limitations.map((v, i) => <p key={i} className="subtle">{v}</p>)}
          {s.counts.attempted_pages !== undefined && <p>尝试 {s.counts.attempted_pages} 页 · 成功 {s.counts.successful_pages || 0} 页 · 详情 {s.counts.detail_pages || 0} 页 · 待查 {s.counts.pending_pages || 0} 页</p>}
          {s.counts.search_queries?.map((q, i) => <p key={i}>{q.term}：{({ not_attempted: '本轮未查', page_read: '已读取搜索页', attempted: '已尝试' } as Record<string, string>)[q.status] || q.status}</p>)}
          {!!s.counts.pending_attribution?.length && <><h4>归属待核对</h4>{s.counts.pending_attribution.map((p, i) => <p key={i}><a href={p.url} target="_blank" rel="noreferrer">{p.title || p.url}</a> · 未计入该对象的舆情资料</p>)}</>}
        </div>)}
      </>}
    </Drawer>
    <Drawer title={visibleReport?.title} open={!!report} onClose={() => setReport(null)} width={760}>
      {visibleReport && <><p className="subtle">{dateText(visibleReport.created_at)} · {visibleReport.model || '规则摘要'}</p>
        {visibleReport.cleared_at ? <ReportRetentionNotice clearedAt={visibleReport.cleared_at} />
          : visibleReport.content ? <p className="job-description">{visibleReport.content}</p> : <Alert type="info" message="报告暂无可显示的正文，请查看保留的来源证据。" />}
        <h3>原文证据</h3>{visibleReport.evidence.map((e, i) => <div className="source-result" key={e.item_id || i}><a href={e.url || e.source_url} target="_blank" rel="noreferrer">{e.title || '查看证据'}</a></div>)}
        {!visibleReport.cleared_at && visibleReport.coverage.engine === 'bettafish' && <a href={'/api/v1/sentiment/reports/' + visibleReport.id + '/artifact'}>下载 BettaFish 报告</a>}
      </>}
    </Drawer>
  </>
}
