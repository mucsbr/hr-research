import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Descriptions, Form, Input, Modal, Popconfirm, Select, Space, Switch, Table, Tabs, Tag } from 'antd'
import { api, post } from './api'
import type { Company, Job } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText } from './ModuleUI'

interface CompanyDetail extends Company { aliases: string[] }
interface Candidate { id: string; url: string; title: string; status: string; evidence: Record<string, unknown> }
interface DiscoverySearch {
  query: string; request_status: 'succeeded' | 'partial'; returned_results: number
  unresponsive_engines: { engine: string; reason: string }[]
}
interface DiscoveryResult { status: string; detail?: string; candidates: Candidate[]; search?: DiscoverySearch }
interface DiscoveryNotice {
  companyId?: string; type: 'info' | 'success' | 'warning' | 'error'; title: string; detail: string; search?: DiscoverySearch
}
interface Proposal { id: string; left_job_id: string; right_job_id: string; confidence: number; status: string; evidence: Record<string, unknown> }
interface Merge { id: string; target_job_id: string; status: string; created_at: string; evidence: Record<string, unknown> }
function evidenceText(value: Record<string, unknown>) {
  return JSON.stringify(value, null, 2)
}

export default function CompanyWorkspace() {
  const { companyId } = useParams(), { message } = App.useApp(), cache = useQueryClient()
  const company = useQuery({ queryKey: ['company-detail', companyId], queryFn: () => api<CompanyDetail>('/companies/' + companyId) })
  const candidates = useQuery({ queryKey: ['entry-candidates', companyId], queryFn: () => api<Candidate[]>('/companies/' + companyId + '/entry-candidates') })
  const proposals = useQuery({ queryKey: ['job-matches', companyId], queryFn: () => api<Proposal[]>('/companies/' + companyId + '/job-matches') })
  const merges = useQuery({ queryKey: ['job-merges', companyId], queryFn: () => api<Merge[]>('/companies/' + companyId + '/job-merges') })
  const jobs = useQuery({ queryKey: ['company-jobs', companyId], queryFn: () => api<{ items: Job[] }>('/jobs?company_id=' + companyId + '&page_size=100') })
  const [editOpen, setEditOpen] = useState(false), [merging, setMerging] = useState<Proposal | null>(null), [busy, setBusy] = useState(false)
  const [discoveryNotice, setDiscoveryNotice] = useState<DiscoveryNotice | null>(null), [form] = Form.useForm(), [mergeForm] = Form.useForm()
  const [evidence, setEvidence] = useState<Record<string, unknown> | null>(null)
  const jobNames = Object.fromEntries((jobs.data?.items || []).map(job => [job.id, job.title + ' · ' + job.locations.join('/')]))
  function refresh() {
    for (const key of ['companies', 'company-detail', 'entry-candidates', 'job-matches', 'job-merges', 'company-jobs', 'jobs', 'sources', 'dashboard']) cache.invalidateQueries({ queryKey: [key] })
  }
  async function action(path: string, data: unknown = {}, method = 'POST', done?: () => void) {
    setBusy(true)
    try { await api(path, { method, body: JSON.stringify(data) }); refresh(); done?.(); message.success('已更新') }
    catch (error) { message.error((error as Error).message) } finally { setBusy(false) }
  }
  async function discover() {
    setBusy(true)
    try {
      const result = await post<DiscoveryResult>('/companies/' + companyId + '/entry-discovery')
      const partial = result.status === 'partial' || result.search?.request_status === 'partial' || !!result.search?.unresponsive_engines.length
      const detail = result.detail || (result.status === 'search_configuration_required'
        ? '尚未配置名称搜索服务。可以先补充公司官网，或在运行设置中配置 SearXNG 搜索。'
        : result.candidates.length ? '请核对主体后绑定候选入口。' : '本次未发现可信的招聘入口，请补充或修正官网。')
      setDiscoveryNotice({ companyId, type: partial ? 'warning' : result.candidates.length ? 'success' : 'info',
        title: partial ? `本次发现 ${result.candidates.length} 个候选，部分搜索引擎未响应，结果可能不完整。`
          : result.candidates.length ? `本次发现 ${result.candidates.length} 个候选入口` : detail,
        detail: !partial && !result.candidates.length ? '' : detail, search: result.search })
      refresh()
    } catch (error) {
      setDiscoveryNotice({ companyId, type: 'error', title: '入口发现未完成，保留此前候选。', detail: (error as Error).message })
      message.error((error as Error).message)
    } finally { setBusy(false) }
  }
  return <>
    <ModuleTitle title={company.data?.name || '公司档案'} description="管理公司身份、发现招聘入口，并核对跨来源的重复岗位。"
      action={<Space><Link to="/companies">返回公司清单</Link><Button onClick={() => { if (company.data) form.setFieldsValue(company.data); setEditOpen(true) }}>编辑档案</Button></Space>} />
    <ModuleError error={company.error || candidates.error || proposals.error || merges.error} />
    <ModulePanel title="关注主体"><Descriptions column={2} items={[
      { label: '正式名称', children: company.data?.official_name || '未补充' }, { label: '别名', children: company.data?.aliases.join('、') || '无' },
      { label: '公司官网', children: company.data?.website ? <a href={company.data.website} target="_blank" rel="noreferrer">{company.data.website}</a> : '未补充' },
      { label: '状态', children: company.data?.archived ? '已归档' : '关注中' }, { label: '备注', children: company.data?.notes || '—' },
    ]} /></ModulePanel>
    <div style={{ height: 22 }} /><Tabs items={[
      { key: 'entries', label: '招聘入口发现', children: <ModulePanel title="候选入口" extra={<Button loading={busy} onClick={discover}>发现招聘入口</Button>}>
        {discoveryNotice && discoveryNotice.companyId === companyId && <Alert type={discoveryNotice.type} showIcon message={discoveryNotice.title} style={{ marginBottom: 16 }} description={
          <div style={{ overflowWrap: 'anywhere' }}>{discoveryNotice.detail && <div>{discoveryNotice.detail}</div>}
            {discoveryNotice.search && <>
              <div>查询：{discoveryNotice.search.query} · 搜索返回 {discoveryNotice.search.returned_results} 条结果</div>
              {!!discoveryNotice.search.unresponsive_engines.length && <ul style={{ marginBottom: 0 }}>
                {discoveryNotice.search.unresponsive_engines.map((engine, index) => <li key={engine.engine + index}>{engine.engine}：{engine.reason}</li>)}
              </ul>}
            </>}
          </div>
        } />}
        <Table rowKey="id" dataSource={candidates.data} columns={[
          { title: '入口', render: (_, c: Candidate) => <a href={c.url} target="_blank" rel="noreferrer">{c.title || c.url}</a> },
          { title: '状态', dataIndex: 'status', render: v => <Tag>{({ pending: '待核对', bound: '已绑定', rejected: '已排除' } as Record<string, string>)[v] || v}</Tag> },
          { title: '依据', render: (_, c: Candidate) => <Button size="small" onClick={() => setEvidence(c.evidence)}>查看证据</Button> },
          { title: '操作', render: (_, c: Candidate) => c.status === 'pending' ? <Space><Button size="small" onClick={() => action('/entry-candidates/' + c.id + '/bind')}>绑定入口</Button><Button size="small" onClick={() => action('/entry-candidates/' + c.id + '/reject')}>排除</Button></Space> : null },
        ]} />
        <div className="note-box">优先从你填写的官网寻找入口。仅提供公司名时，使用管理员配置的搜索服务生成候选；绑定前请核对公司主体。</div>
      </ModulePanel> },
      { key: 'matches', label: '跨来源岗位匹配', children: <ModulePanel title="疑似相同岗位" extra={<Button loading={busy} onClick={() => action('/companies/' + companyId + '/job-matches')}>检查匹配</Button>}>
        <Table rowKey="id" dataSource={proposals.data} columns={[
          { title: '岗位 A', dataIndex: 'left_job_id', render: v => jobNames[v] || v.slice(0, 8) }, { title: '岗位 B', dataIndex: 'right_job_id', render: v => jobNames[v] || v.slice(0, 8) },
          { title: '匹配分', dataIndex: 'confidence' }, { title: '状态', dataIndex: 'status', render: v => <Tag>{v === 'pending' ? '待核对' : v === 'merged' ? '已合并' : v}</Tag> },
          { title: '操作', render: (_, p: Proposal) => <Space><Button size="small" onClick={() => setEvidence(p.evidence)}>依据</Button>{p.status === 'pending' && <Button size="small" onClick={() => { setMerging(p); mergeForm.setFieldsValue({ target_job_id: p.left_job_id, reason: '' }) }}>合并</Button>}</Space> },
        ]} /><div className="note-box">只比较同一家公司的岗位。匹配分是规则相似度，不代表概率；合并保留每个来源及历史，支持在无后续冲突时撤销。</div>
      </ModulePanel> },
      { key: 'merges', label: '合并记录', children: <ModulePanel title="可审计的合并历史"><Table rowKey="id" dataSource={merges.data} columns={[
        { title: '主岗位', dataIndex: 'target_job_id', render: v => jobNames[v] || v.slice(0, 8) }, { title: '状态', dataIndex: 'status', render: v => v === 'merged' ? '已合并' : '已撤销' },
        { title: '时间', dataIndex: 'created_at', render: dateText },
        { title: '操作', render: (_, m: Merge) => <Space><Button size="small" onClick={() => setEvidence(m.evidence)}>依据</Button>{m.status === 'merged' && <Popconfirm title="恢复原来的来源归属？" onConfirm={() => action('/job-merges/' + m.id + '/undo')}><Button size="small">撤销合并</Button></Popconfirm>}</Space> },
      ]} /></ModulePanel> },
    ]} />
    <Modal title="编辑公司档案" open={editOpen} onCancel={() => setEditOpen(false)} onOk={() => form.submit()} confirmLoading={busy} okText="保存">
      <Form form={form} layout="vertical" onFinish={v => action('/companies/' + companyId, v, 'PATCH', () => setEditOpen(false))}>
        <Form.Item name="name" label="公司名称" rules={[{ required: true }]}><Input /></Form.Item><Form.Item name="official_name" label="正式名称"><Input /></Form.Item>
        <Form.Item name="website" label="公司官网"><Input /></Form.Item><Form.Item name="aliases" label="别名"><Select mode="tags" tokenSeparators={['，', ',']} /></Form.Item>
        <Form.Item name="role" label="类型"><Select options={[{ value: 'competitor', label: '竞品公司' }, { value: 'own', label: '自有公司' }, { value: 'other', label: '其他关注' }]} /></Form.Item>
        <Form.Item name="notes" label="备注"><Input.TextArea rows={3} /></Form.Item><Form.Item name="archived" label="归档" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Modal title="合并岗位来源" open={!!merging} onCancel={() => setMerging(null)} onOk={() => mergeForm.submit()} confirmLoading={busy} okText="合并">
      <Form form={mergeForm} layout="vertical" onFinish={v => action('/job-matches/' + merging?.id + '/merge', v, 'POST', () => setMerging(null))}>
        <Form.Item name="target_job_id" label="保留的主岗位" rules={[{ required: true }]}><Select options={merging ? [merging.left_job_id, merging.right_job_id].map(id => ({ value: id, label: jobNames[id] || id })) : []} /></Form.Item>
        <Form.Item name="reason" label="合并依据" rules={[{ required: true }]}><Input.TextArea rows={3} /></Form.Item>
      </Form>
    </Modal>
    <Modal title="发现与匹配依据" open={!!evidence} onCancel={() => setEvidence(null)} footer={null}><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{evidence && evidenceText(evidence)}</pre></Modal>
  </>
}
