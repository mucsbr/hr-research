import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { App, Alert, Button, Descriptions, Drawer, Form, Input, InputNumber, Modal, Select, Space, Switch, Table, Tabs, Tag } from 'antd'
import { api, post } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText, stateTag } from './ModuleUI'

interface Source { id: string; name: string; kind: string; url: string; subject_id: string | null; source_type: string; publisher: string | null; regions: string[]; topics: string[]; industries: string[]; keywords: string[]; exclusions: string[]; enabled: boolean; health: string; last_success_at: string | null }
interface Article { id: string; url: string; title: string; content: string; excerpt: string; publisher: string | null; source_type: string; content_type: string; published_at: string | null; published_date: string | null; policy_issued_date: string | null; effective_date: string | null; applicable_audience: string | null; first_seen_at: string; last_seen_at: string; regions: string[]; topics: string[]; industries: string[]; is_read: boolean; is_favorite: boolean; notes: string; classification?: { content_extent?: string; uncertainty?: string[]; date_candidates?: { effective_date_candidates?: string[]; policy_issued_date_candidates?: string[] } }; observations?: { id: string; url: string; created_at: string }[] }
interface Run { id: string; status: string; created_at: string; error_message: string | null; summary: Record<string, unknown>; sources?: { id: string; status: string; coverage: string; error_message: string | null; limitations: string[] }[] }
const sourceTypes: Record<string, string> = { official: '官方机构', media: '新闻媒体', industry: '行业机构', other: '其他' }
const contentTypes: Record<string, string> = { policy_document: '政策原文', official_interpretation: '官方解读', media_interpretation: '媒体解读', official_news: '官方动态', industry_news: '行业资讯', source_article: '来源文章' }
const contentExtents: Record<string, string> = { full_text: '原文正文', feed_summary: 'Feed 摘要', feed_content: 'Feed 内容（未核验原网页全文）' }

export default function NewsPage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const [tab, setTab] = useState('items'), [page, setPage] = useState(1), [keyword, setKeyword] = useState('')
  const [sourceFilter, setSourceFilter] = useState<string>(), [type, setType] = useState<string>(), [favorite, setFavorite] = useState(false)
  const [region, setRegion] = useState(''), [topic, setTopic] = useState(''), [industry, setIndustry] = useState('')
  const [sourceOpen, setSourceOpen] = useState(false), [runOpen, setRunOpen] = useState(false), [editing, setEditing] = useState<Source | null>(null)
  const [itemId, setItemId] = useState<string | null>(null), [runId, setRunId] = useState<string | null>(null), [notes, setNotes] = useState(''), [saving, setSaving] = useState(false)
  const [sourceForm] = Form.useForm(), [runForm] = Form.useForm()
  const sources = useQuery({ queryKey: ['news', 'sources'], queryFn: () => api<Source[]>('/news/sources') })
  const runs = useQuery({ queryKey: ['news', 'runs'], queryFn: () => api<Run[]>('/news/runs'), refetchInterval: 7000 })
  const params = new URLSearchParams({ limit: '20', offset: String((page - 1) * 20) })
  if (keyword) params.set('q', keyword)
  if (sourceFilter) params.set('source_id', sourceFilter)
  if (type) params.set('content_type', type)
  if (favorite) params.set('is_favorite', 'true')
  if (region) params.set('region', region)
  if (topic) params.set('topic', topic)
  if (industry) params.set('industry', industry)
  const items = useQuery({ queryKey: ['news', 'items', params.toString()], queryFn: () => api<{ items: Article[]; total: number }>('/news/items?' + params), refetchInterval: 20000 })
  const detail = useQuery({ queryKey: ['news', 'item', itemId], queryFn: () => api<Article>('/news/items/' + itemId), enabled: !!itemId })
  const runDetail = useQuery({ queryKey: ['news', 'run', runId], queryFn: () => api<Run>('/news/runs/' + runId), enabled: !!runId, refetchInterval: runId ? 7000 : false })
  const article = detail.data
  async function save(path: string, value: unknown, method = 'POST', done?: () => void) {
    setSaving(true)
    try { await api(path, { method, body: JSON.stringify(value) }); cache.invalidateQueries({ queryKey: ['news'] }); done?.(); message.success('已保存') }
    catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
  }
  return <>
    <ModuleTitle title="HR 行业资讯" description="关注人才市场、用工政策与行业实践，按来源和地区积累可追溯资料。"
      action={<Space><Link to="/notifications">订阅提醒</Link><Button type="primary" onClick={() => setRunOpen(true)}>采集资讯</Button></Space>} />
    <ModuleError error={sources.error || runs.error || items.error} />
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'items', label: '资讯与政策', children: <ModulePanel title="资料列表" extra={<span className="subtle">当前筛选 {items.data?.total ?? '—'} 篇</span>}>
        <div className="filter-bar"><Input.Search placeholder="搜索标题或正文" allowClear onSearch={v => { setKeyword(v); setPage(1) }} style={{ width: 230 }} />
          <Select allowClear placeholder="全部来源" style={{ width: 170 }} onChange={v => { setSourceFilter(v); setPage(1) }} options={sources.data?.map(s => ({ value: s.id, label: s.name }))} />
          <Select allowClear placeholder="全部类型" style={{ width: 150 }} onChange={v => { setType(v); setPage(1) }} options={Object.entries(contentTypes).map(([value, label]) => ({ value, label }))} />
          <Space>只看收藏<Switch size="small" checked={favorite} onChange={v => { setFavorite(v); setPage(1) }} /></Space>
        </div>
        <div className="filter-bar"><Input.Search allowClear placeholder="地区标签，如上海" onSearch={v => { setRegion(v); setPage(1) }} style={{ width: 230 }} /><Input.Search allowClear placeholder="主题标签，如社会保险" onSearch={v => { setTopic(v); setPage(1) }} style={{ width: 230 }} /><Input.Search allowClear placeholder="行业标签，如制造业" onSearch={v => { setIndustry(v); setPage(1) }} style={{ width: 230 }} /></div>
        <Table rowKey="id" dataSource={items.data?.items} loading={items.isPending} pagination={{ current: page, pageSize: 20, total: items.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
          { title: '标题', render: (_, item: Article) => <Button type="link" style={{ whiteSpace: 'normal', textAlign: 'left', height: 'auto' }} onClick={() => { setItemId(item.id); setNotes(item.notes); if (!item.is_read) save('/news/items/' + item.id, { is_read: true }, 'PATCH') }}>{!item.is_read && '● '}{item.title}</Button> },
          { title: '类型', dataIndex: 'content_type', render: v => <Tag>{contentTypes[v] || v}</Tag> },
          { title: '资料形式', render: (_, item: Article) => <Tag>{contentExtents[item.classification?.content_extent || ''] || '未标注'}</Tag> },
          { title: '地区', dataIndex: 'regions', render: (values: string[]) => values.join('、') || '未标注' },
          { title: '发布日期', render: (_, item: Article) => item.published_date || dateText(item.published_at) },
          { title: '生效日期', dataIndex: 'effective_date', render: v => v || '未明确' },
          { title: '收藏', render: (_, item: Article) => <Button size="small" type={item.is_favorite ? 'primary' : 'default'} onClick={() => save('/news/items/' + item.id, { is_favorite: !item.is_favorite }, 'PATCH')}>{item.is_favorite ? '已收藏' : '收藏'}</Button> },
        ]} />
        <div className="note-box">发布日期、采集时间与政策生效日期分别记录。摘要为原文摘录，政策适用范围以原文为准。</div>
      </ModulePanel> },
      { key: 'sources', label: '资讯来源', children: <ModulePanel title="网页与订阅源" extra={<Button onClick={() => { setEditing(null); sourceForm.resetFields(); setSourceOpen(true) }}>新增来源</Button>}>
        <Table rowKey="id" dataSource={sources.data} columns={[
          { title: '名称', dataIndex: 'name' }, { title: '采集方式', dataIndex: 'kind', render: v => v === 'feed' ? 'RSS / Atom' : '公开网页' },
          { title: '来源类型', dataIndex: 'source_type', render: v => sourceTypes[v] || v }, { title: '状态', dataIndex: 'health', render: stateTag },
          { title: '最近成功', dataIndex: 'last_success_at', render: dateText },
          { title: '启用', render: (_, s: Source) => <Switch checked={s.enabled} onChange={enabled => save('/news/sources/' + s.id, { enabled }, 'PATCH')} /> },
          { title: '操作', render: (_, s: Source) => <Button onClick={() => { setEditing(s); sourceForm.setFieldsValue(s); setSourceOpen(true) }}>编辑</Button> },
        ]} />
      </ModulePanel> },
      { key: 'runs', label: '执行记录', children: <ModulePanel title="采集任务">
        <Table rowKey="id" dataSource={runs.data} columns={[
          { title: '任务', render: (_, r: Run) => <Button type="link" onClick={() => setRunId(r.id)}>{r.id.slice(0, 8)}</Button> },
          { title: '状态', dataIndex: 'status', render: stateTag }, { title: '时间', dataIndex: 'created_at', render: dateText }, { title: '原因', dataIndex: 'error_message' },
          { title: '操作', render: (_, r: Run) => <Space><Button size="small" onClick={() => setRunId(r.id)}>来源详情</Button>{['queued', 'running'].includes(r.status) ? <Button size="small" onClick={() => save('/tasks/' + r.id + '/cancel', {})}>取消</Button> : <Button size="small" onClick={async () => { try { await post('/news/runs/' + r.id + '/retry'); cache.invalidateQueries({ queryKey: ['news'] }) } catch (error) { message.error((error as Error).message) } }}>重新采集</Button>}</Space> },
        ]} />
      </ModulePanel> },
    ]} />
    <Modal title={editing ? '编辑资讯来源' : '新增资讯来源'} open={sourceOpen} onCancel={() => setSourceOpen(false)} onOk={() => sourceForm.submit()} confirmLoading={saving} okText="保存">
      <Form form={sourceForm} layout="vertical" initialValues={{ kind: 'feed', source_type: 'other', regions: [], topics: [], industries: [], keywords: [], exclusions: [], enabled: true }} onFinish={v => { const { kind, ...changes } = v; save('/news/sources' + (editing ? '/' + editing.id : ''), editing ? changes : { ...changes, kind }, editing ? 'PATCH' : 'POST', () => setSourceOpen(false)) }}>
        <Form.Item name="name" label="来源名称" rules={[{ required: true }]}><Input /></Form.Item>
        <Form.Item name="kind" label="采集方式"><Select disabled={!!editing} options={[{ value: 'feed', label: 'RSS / Atom 订阅' }, { value: 'web', label: '公开文章或资讯列表' }]} /></Form.Item>
        <Form.Item name="url" label="来源 URL" rules={[{ required: true }, { type: 'url' }]}><Input /></Form.Item>
        <Form.Item name="source_type" label="来源类型"><Select options={Object.entries(sourceTypes).map(([value, label]) => ({ value, label }))} /></Form.Item>
        <Form.Item name="publisher" label="发布机构（可选）"><Input /></Form.Item>
        {(['regions', 'topics', 'industries', 'keywords', 'exclusions'] as const).map((name, i) => <Form.Item key={name} name={name} label={['地区标签', '主题标签', '行业标签', '至少命中一个关键词（空为不限）', '排除关键词'][i]}><Select mode="tags" tokenSeparators={['，', ',']} /></Form.Item>)}
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Modal title="采集 HR 资讯" open={runOpen} onCancel={() => setRunOpen(false)} onOk={() => runForm.submit()} confirmLoading={saving} okText="开始采集">
      <Form form={runForm} layout="vertical" initialValues={{ source_ids: [], max_articles: 10, max_pages: 3, render: false }} onFinish={v => save('/news/runs', v, 'POST', () => { setRunOpen(false); setTab('runs') })}>
        <Form.Item name="source_ids" label="采集来源（留空使用全部启用来源）"><Select mode="multiple" options={sources.data?.filter(s => s.enabled).map(s => ({ value: s.id, label: s.name }))} /></Form.Item>
        <Space align="start"><Form.Item name="max_articles" label="文章上限"><InputNumber min={1} max={30} /></Form.Item><Form.Item name="max_pages" label="每来源列表页上限"><InputNumber min={1} max={5} /></Form.Item></Space>
        <Form.Item name="render" label="用浏览器读取动态网页来源" valuePropName="checked" extra="RSS/Atom 订阅仍使用 HTTP 读取。"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Drawer title={article?.title || '资讯详情'} open={!!itemId} onClose={() => setItemId(null)} width={760}>
      <ModuleError error={detail.error} />{article && <><Space><Tag>{contentTypes[article.content_type] || article.content_type}</Tag><a href={article.url} target="_blank" rel="noreferrer">查看原文</a></Space>
        <Descriptions column={2} items={[
          { label: '发布机构', children: article.publisher || '未注明' }, { label: '来源类型', children: sourceTypes[article.source_type] || article.source_type },
          { label: '发布时间', children: article.published_at ? dateText(article.published_at) : article.published_date || '未知' }, { label: '首次采集', children: dateText(article.first_seen_at) },
          { label: '政策发文日期', children: article.policy_issued_date || '未明确' }, { label: '生效日期', children: article.effective_date || '未明确' },
          { label: '适用对象', children: article.applicable_audience || '以原文为准' }, { label: '主题', children: article.topics.join('、') || '未分类' },
          { label: '行业', children: article.industries.join('、') || '未标注' }, { label: '资料形式', children: contentExtents[article.classification?.content_extent || ''] || '未标注' },
        ]} />
        {!!article.classification?.uncertainty?.length && <Alert type="warning" message="资料范围与日期说明" description={<ul>{article.classification.uncertainty.map((note, index) => <li key={index}>{note}</li>)}</ul>} />}
        {(!!article.classification?.date_candidates?.effective_date_candidates?.length || !!article.classification?.date_candidates?.policy_issued_date_candidates?.length) && <div className="note-box">
          <strong>原文提到的日期</strong><p>以下是提取到的日期候选，存在多个日期时不自动确定政策适用或生效时间。</p>
          {!!article.classification.date_candidates.effective_date_candidates?.length && <p>生效日期候选：{article.classification.date_candidates.effective_date_candidates.join('、')}</p>}
          {!!article.classification.date_candidates.policy_issued_date_candidates?.length && <p>发文日期候选：{article.classification.date_candidates.policy_issued_date_candidates.join('、')}</p>}
        </div>}
        <h3>原文摘录</h3><p className="job-description">{article.excerpt}</p><h3>{contentExtents[article.classification?.content_extent || ''] || '已采集资料'}</h3><p className="job-description">{article.content}</p>
        <h3>采集来源记录</h3>{article.observations?.map(o => <p key={o.id}><a href={o.url} target="_blank" rel="noreferrer">{o.url}</a> · {dateText(o.created_at)}</p>)}
        <h3>内部备注</h3><Input.TextArea value={notes} onChange={e => setNotes(e.target.value)} rows={3} /><Button style={{ marginTop: 12 }} onClick={() => save('/news/items/' + article.id, { notes }, 'PATCH')}>保存备注</Button>
      </>}
    </Drawer>
    <Drawer title="资讯来源执行情况" open={!!runId} onClose={() => setRunId(null)} width={680}>
      <ModuleError error={runDetail.error} />{runDetail.data && <>{stateTag(runDetail.data.status)}{runDetail.data.error_message && <Alert type="warning" message={runDetail.data.error_message} />}
        {runDetail.data.sources?.map(s => <div className="source-result" key={s.id}><Space>{stateTag(s.status)}<Tag>{s.coverage}</Tag></Space><p>{s.error_message}</p>{s.limitations.map((v, i) => <p key={i}>{v}</p>)}</div>)}
      </>}
    </Drawer>
  </>
}
