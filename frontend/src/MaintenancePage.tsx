import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Drawer, Form, Input, InputNumber, Modal, Popconfirm, Space, Switch, Table, Tag } from 'antd'
import { api } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText, stateTag } from './ModuleUI'

interface Release { id?: string; version: string; fingerprint: string; status: string; origin: string; manifest: Record<string, unknown>; validation?: { passed: boolean; scope?: string; code_proposal_id?: string; checks?: { name: string; passed: boolean }[] }; created_at?: string }
interface Adapter { kind: string; registered: boolean; upstream_available: boolean; active: Release; registry: { agent_enabled: boolean; check_enabled: boolean; auto_repair_enabled: boolean; auto_publish_enabled: boolean; code_agent_enabled: boolean; code_auto_publish_enabled: boolean; code_upstream_enabled: boolean; release_generation: number; last_check_at: string | null } | null }
interface Run { id: string; kind: string; status: string; created_at: string; error_message: string | null; details: Record<string, unknown> }
interface Incident { id: string; status: string; error_code: string; attempts: number; recovery_attempts: number; created_at: string; task_id: string; recovery_task_id: string | null; details: Record<string, unknown>; kind?: string; code_agent_enabled?: boolean }
interface CodeProposal { id: string; kind: string; status: string; origin: string; attempts: number; max_candidates: number; created_at: string; error_message?: string; error_code?: string; bundle_sha?: string; release_id?: string; base_generation: number; current_generation: number; current_active_release_id: string; validation_count?: number; completed_validation_count?: number; source_diff?: { path: string; diff: string; truncated: boolean }[]; dependency_diff?: { before: string[]; after: string[] }; validation_run_refs?: { case_id: string; status: string; run_id?: string; error_code?: string; verification_outcome?: string }[]; task_summary?: Record<string, unknown>; model_contexts?: unknown[] }
const names: Record<string, string> = { official: '企业官网', boss: 'BOSS 直聘', liepin: '猎聘', zhaopin: '智联招聘', '51job': '前程无忧' }
const codeStates: Record<string, string> = { queued: '等待生成', generating: '生成中', retry_ready: '等待下一候选', draft_ready: '候选已生成', resolving: '锁定依赖', resolved: '依赖已锁定', uploading: '登记代码包', validating: '隔离验证中', validated: '验证通过', awaiting_publish: '等待发布', published: '已发布', recovery_queued: '已发布，等待恢复采集', recovering: '已发布，恢复采集中', recovered: '已恢复采集', recovery_failed: '恢复失败，请查看记录', unknown: '结果未知，已停止', blocked_configuration: '等待运行环境配置', cancelled: '已取消', failed: '失败，请查看版本记录', conflict: '基线版本已变化', skipped: '没有相关变更' }

export default function MaintenancePage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<{ role: string }>('/auth/me') })
  const isAdmin = me.data?.role === 'admin'
  const adapters = useQuery({ queryKey: ['maintenance', 'adapters'], queryFn: () => api<Adapter[]>('/adapter-maintenance/adapters'), enabled: isAdmin })
  const runs = useQuery({ queryKey: ['maintenance', 'runs'], queryFn: () => api<Run[]>('/adapter-maintenance/runs'), enabled: isAdmin, refetchInterval: 10000 })
  const incidents = useQuery({ queryKey: ['maintenance', 'incidents'], queryFn: () => api<Incident[]>('/adapter-maintenance/incidents'), enabled: isAdmin, refetchInterval: 7000 })
  const proposals = useQuery({ queryKey: ['maintenance', 'code-proposals'], queryFn: () => api<CodeProposal[]>('/adapter-maintenance/code-proposals'), enabled: isAdmin, refetchInterval: 7000 })
  const [selected, setSelected] = useState<Adapter | null>(null), [candidate, setCandidate] = useState(''), [candidateOpen, setCandidateOpen] = useState(false)
  const [generating, setGenerating] = useState<Adapter | null>(null), [busy, setBusy] = useState(false), [form] = Form.useForm()
  const releases = useQuery({ queryKey: ['maintenance', 'releases', selected?.kind], queryFn: () => api<Release[]>('/adapter-maintenance/adapters/' + selected?.kind + '/releases'), enabled: !!selected && isAdmin })
  const currentAdapter = adapters.data?.find(a => a.kind === selected?.kind) || selected
  const [detail, setDetail] = useState<Release | Run | Incident | null>(null)
  const [codeDetail, setCodeDetail] = useState<CodeProposal | null>(null)
  async function openCodeProposal(id: string) {
    try { setCodeDetail(await api<CodeProposal>('/adapter-maintenance/code-proposals/' + id)) }
    catch (error) { message.error((error as Error).message) }
  }
  async function action(path: string, values: unknown = {}, method = 'POST', done?: () => void) {
    setBusy(true)
    try { await api('/adapter-maintenance' + path, { method, body: JSON.stringify(values) }); cache.invalidateQueries({ queryKey: ['maintenance'] }); done?.(); message.success('操作已记录') }
    catch (error) { message.error((error as Error).message) } finally { setBusy(false) }
  }
  if (me.isPending) return <p>正在检查权限…</p>
  if (!isAdmin) return <div className="note-box">适配器发布、回滚和上游检查由管理员操作。</div>
  return <>
    <ModuleTitle title="适配器维护" description="查看当前采集规则、检查已登记上游、验证候选版本并保留回滚记录。"
      action={<Button loading={busy} onClick={() => action('/register')}>登记内置版本</Button>} />
    <ModuleError error={adapters.error || runs.error || incidents.error || proposals.error} />
    <ModulePanel title="已支持的招聘平台"><Table rowKey="kind" dataSource={adapters.data} loading={adapters.isPending} columns={[
      { title: '来源', dataIndex: 'kind', render: v => names[v] || v },
      { title: '当前版本', render: (_, a: Adapter) => <Space direction="vertical" size={0}><strong>{a.active.version}</strong><span className="subtle">{a.active.fingerprint.slice(0, 12)} · {a.registered ? '已登记' : '内置默认'}</span></Space> },
      { title: '每日上游检查', render: (_, a: Adapter) => <Switch disabled={!a.upstream_available} checked={a.registry?.check_enabled || false} onChange={check_enabled => action('/adapters/' + a.kind + '/settings', { check_enabled }, 'PATCH')} /> },
      { title: '允许 JSON 模型修复', render: (_, a: Adapter) => <Switch disabled={a.kind === 'official' || !!a.active.manifest.program} checked={a.registry?.agent_enabled || false} onChange={agent_enabled => action('/adapters/' + a.kind + '/settings', { agent_enabled }, 'PATCH')} /> },
      { title: '解析失败自动修复', render: (_, a: Adapter) => <Switch disabled={!a.registry?.agent_enabled} checked={a.registry?.auto_repair_enabled || false} onChange={auto_repair_enabled => action('/adapters/' + a.kind + '/settings', { auto_repair_enabled }, 'PATCH')} /> },
      { title: '验证后自动发布与恢复', render: (_, a: Adapter) => <Switch disabled={!a.registry?.auto_repair_enabled} checked={a.registry?.auto_publish_enabled || false} onChange={auto_publish_enabled => action('/adapters/' + a.kind + '/settings', { auto_publish_enabled }, 'PATCH')} /> },
      { title: '操作', render: (_, a: Adapter) => <Space wrap><Button size="small" disabled={!a.upstream_available} onClick={() => action('/adapters/' + a.kind + '/check')}>检查上游</Button><Button size="small" onClick={() => setSelected(a)}>版本管理</Button><Button size="small" disabled={!a.registry?.agent_enabled || a.kind === 'official' || !!a.active.manifest.program} onClick={() => { form.resetFields(); setGenerating(a) }}>生成规则候选</Button></Space> },
    ]} /><div className="note-box">自动修复默认关闭。启用后，只有带可核验脱敏证据的解析错误会申请模型预算，每次最多两个候选；登录、验证、网络错误不触发。验证通过后最多恢复采集一次，失败保留记录并按情况回滚。</div></ModulePanel>
    <div style={{ height: 22 }} /><ModulePanel title="代码与依赖维护"><Table rowKey="kind" dataSource={adapters.data} columns={[
      { title: '来源', dataIndex: 'kind', render: v => names[v] || v },
      { title: '当前执行方式', render: (_, a: Adapter) => <Tag color={a.active.manifest.program ? 'blue' : 'default'}>{a.active.manifest.program ? '已验证代码包' : a.kind === 'official' ? '内置官网采集' : '内置解析与 JSON 规则'}</Tag> },
      { title: '独立启用代码模型', render: (_, a: Adapter) => <Switch checked={a.registry?.code_agent_enabled || false} onChange={code_agent_enabled => action('/adapters/' + a.kind + '/settings', { code_agent_enabled }, 'PATCH')} /> },
      { title: '固定上游自动升级', render: (_, a: Adapter) => <Switch disabled={!a.registry?.code_agent_enabled || !a.upstream_available} checked={a.registry?.code_upstream_enabled || false} onChange={code_upstream_enabled => action('/adapters/' + a.kind + '/settings', { code_upstream_enabled }, 'PATCH')} /> },
      { title: '代码验证后自动发布', render: (_, a: Adapter) => <Switch disabled={!a.registry?.code_agent_enabled} checked={a.registry?.code_auto_publish_enabled || false} onChange={code_auto_publish_enabled => action('/adapters/' + a.kind + '/settings', { code_auto_publish_enabled }, 'PATCH')} /> },
    ]} /><div className="note-box">代码维护拥有独立开关，默认关闭。平台从已登记上游或已记录的失败证据创建提案，先检查隔离运行环境，再申请模型预算。代码与规则共用同一事件的两次候选额度；基础设施失败或结果未知时停止。测试预期由平台维护。</div></ModulePanel>
    <div style={{ height: 22 }} /><ModulePanel title="代码维护提案"><Table rowKey="id" dataSource={proposals.data} loading={proposals.isPending} columns={[
      { title: '来源', dataIndex: 'kind', render: v => names[v] || v },
      { title: '创建时间', dataIndex: 'created_at', render: dateText },
      { title: '状态', dataIndex: 'status', render: v => <Tag>{codeStates[v] || v}</Tag> },
      { title: '候选额度', render: (_, p: CodeProposal) => `${p.attempts} / ${p.max_candidates}` },
      { title: '隔离验证', render: (_, p: CodeProposal) => `${p.completed_validation_count || 0} / ${p.validation_count || 0}` },
      { title: '说明', dataIndex: 'error_message' },
      { title: '操作', render: (_, p: CodeProposal) => <Space wrap><Button size="small" onClick={() => openCodeProposal(p.id)}>差异与验证</Button>
        {['validated', 'awaiting_publish'].includes(p.status) && <Popconfirm title="发布已经独立验证的代码与依赖版本？" onConfirm={() => action('/code-proposals/' + p.id + '/publish', { expected_active_release_id: p.current_active_release_id, expected_generation: p.current_generation })}><Button size="small">发布</Button></Popconfirm>}
        {!['published', 'recovery_queued', 'recovering', 'recovered', 'cancelled', 'failed', 'unknown', 'conflict', 'skipped'].includes(p.status) && <Button size="small" onClick={() => action('/code-proposals/' + p.id + '/cancel')}>取消</Button>}
      </Space> },
    ]} /></ModulePanel>
    <div style={{ height: 22 }} /><ModulePanel title="自动修复与恢复采集"><Table rowKey="id" dataSource={incidents.data} columns={[
      { title: '发现时间', dataIndex: 'created_at', render: dateText },
      { title: '原始错误', dataIndex: 'error_code' }, { title: '状态', dataIndex: 'status', render: stateTag },
      { title: '模型候选次数', dataIndex: 'attempts' }, { title: '恢复次数', dataIndex: 'recovery_attempts' },
      { title: '操作', render: (_, incident: Incident) => <Space wrap><Button size="small" onClick={async () => { try { setDetail(await api<Incident>('/adapter-maintenance/incidents/' + incident.id)) } catch (error) { message.error((error as Error).message) } }}>证据与记录</Button>
        {incident.status === 'awaiting_publish' && <Popconfirm title="发布已验证候选，并对原失败来源恢复一次采集？" onConfirm={() => action('/incidents/' + incident.id + '/publish')}><Button size="small">发布并恢复</Button></Popconfirm>}
        {['queued', 'running', 'awaiting_publish', 'recovery_queued', 'recovering'].includes(incident.status) && <Button size="small" onClick={() => action('/incidents/' + incident.id + '/cancel')}>停止修复</Button>}
        {incident.code_agent_enabled && ['needs_engineering', 'failed'].includes(incident.status) && <Button size="small" onClick={() => action('/incidents/' + incident.id + '/code-proposal')}>创建代码提案</Button>}
      </Space> },
    ]} /></ModulePanel>
    <div style={{ height: 22 }} /><ModulePanel title="维护记录"><Table rowKey="id" dataSource={runs.data} columns={[
      { title: '类型', dataIndex: 'kind', render: v => ({ upstream_check: '上游检查', shadow_validation: '影子验证', candidate_validation: '候选校验', publish: '发布', rollback: '回滚', model_candidate: '模型候选' } as Record<string, string>)[v] || v },
      { title: '状态', dataIndex: 'status', render: stateTag }, { title: '时间', dataIndex: 'created_at', render: dateText }, { title: '说明', dataIndex: 'error_message' },
      { title: '详情', render: (_, r: Run) => <Button size="small" onClick={() => setDetail(r)}>查看记录</Button> },
    ]} /></ModulePanel>
    <Drawer title={(selected ? names[selected.kind] : '') + ' · 版本管理'} open={!!selected} onClose={() => setSelected(null)} width={760}>
      <ModuleError error={releases.error} />
      <Button disabled={currentAdapter?.kind === 'official' || !!currentAdapter?.active.manifest.program} onClick={() => { setCandidate(JSON.stringify(currentAdapter?.active.manifest, null, 2)); setCandidateOpen(true) }}>提交候选配置</Button>
      <Table rowKey={r => r.id || r.fingerprint} dataSource={releases.data} columns={[
        { title: '版本', dataIndex: 'version' }, { title: '状态', dataIndex: 'status', render: v => <Tag>{v}</Tag> },
        { title: '验证', render: (_, r: Release) => r.validation?.passed ? '已通过' : '待检查' },
        { title: '操作', render: (_, r: Release) => <Space wrap><Button size="small" onClick={() => setDetail(r)}>依据</Button>{r.id && <>
          <Button size="small" onClick={() => action('/releases/' + r.id + '/validate')}>验证</Button>
          {r.status === 'validated' && <Popconfirm title="发布此候选供后续采集使用？" onConfirm={() => r.validation?.code_proposal_id ? action('/code-proposals/' + r.validation.code_proposal_id + '/publish', { expected_active_release_id: currentAdapter?.active.id, expected_generation: currentAdapter?.registry?.release_generation }) : action('/releases/' + r.id + '/publish')}><Button size="small">发布</Button></Popconfirm>}
          {['retired', 'published'].includes(r.status) && r.id !== currentAdapter?.active.id && <Popconfirm title="重新核验历史产物后，将此版本恢复为当前版本？" onConfirm={() => action('/releases/' + r.id + '/rollback', { expected_active_release_id: currentAdapter?.active.id, expected_generation: currentAdapter?.registry?.release_generation })}><Button size="small">回滚到此版</Button></Popconfirm>}
        </>}</Space> },
      ]} />
      <div className="note-box">影子验证不会写入正式岗位或发送通知。运行中的任务保持其开始时使用的版本。</div>
    </Drawer>
    <Modal title="提交受限 JSON 规则" open={candidateOpen} onCancel={() => setCandidateOpen(false)} okText="保存候选" confirmLoading={busy} onOk={() => { try { action('/adapters/' + selected?.kind + '/candidates', { manifest: JSON.parse(candidate) }, 'POST', () => setCandidateOpen(false)) } catch { message.error('请输入合法 JSON') } }} width={720}>
      <Input.TextArea rows={16} value={candidate} onChange={e => setCandidate(e.target.value)} spellCheck={false} /><p className="subtle">候选必须保留平台和上游身份，通过字段与选择器验证后才能发布。</p>
    </Modal>
    <Modal title="生成适配修复候选" open={!!generating} onCancel={() => setGenerating(null)} onOk={() => form.submit()} okText="生成并验证" confirmLoading={busy}>
      <Form form={form} layout="vertical" initialValues={{ max_candidates: 1, auto_publish: false }} onFinish={v => action('/adapters/' + generating?.kind + '/generate', v, 'POST', () => setGenerating(null))}>
        <Form.Item name="failure_description" label="已观察到的失败及字段变化" rules={[{ required: true }]}><Input.TextArea rows={5} maxLength={1500} placeholder="描述实际失败，勿粘贴 Cookie、密钥或账号个人信息" /></Form.Item>
        <Form.Item name="max_candidates" label="候选上限（会使用模型预算）"><InputNumber min={1} max={2} /></Form.Item>
        <Form.Item name="auto_publish" label="验证通过后自动发布" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Modal>
    <Drawer title="代码提案 · 差异与验证" open={!!codeDetail} onClose={() => setCodeDetail(null)} width={860}>
      {codeDetail && <><Space wrap><Tag>{names[codeDetail.kind] || codeDetail.kind}</Tag><Tag>{codeStates[codeDetail.status] || codeDetail.status}</Tag><span>基线代次 {codeDetail.base_generation} · 当前代次 {codeDetail.current_generation}</span></Space>
        {codeDetail.error_message && <div className="note-box">{codeDetail.error_message}</div>}
        <h3>依赖版本变化</h3><p>原依赖：{codeDetail.dependency_diff?.before.join('，') || '无额外依赖'}</p><p>候选依赖：{codeDetail.dependency_diff?.after.join('，') || '无额外依赖'}</p>
        <h3>源码差异</h3>{codeDetail.source_diff?.length ? codeDetail.source_diff.map(file => <section key={file.path}><strong>{file.path}</strong>{file.truncated && <Tag>展示已截断</Tag>}<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{file.diff}</pre></section>) : <p className="subtle">尚未生成源码差异。</p>}
        <h3>隔离验证记录</h3><Table rowKey="case_id" size="small" pagination={false} dataSource={codeDetail.validation_run_refs} columns={[
          { title: '平台案例', dataIndex: 'case_id' }, { title: '状态', dataIndex: 'status', render: stateTag }, { title: '执行标识', dataIndex: 'run_id', ellipsis: true }, { title: '原因', dataIndex: 'error_code' },
        ]} /><h3>验证结论与输入范围</h3><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{JSON.stringify({ summary: codeDetail.task_summary, model_contexts: codeDetail.model_contexts }, null, 2)}</pre>
        <Button onClick={() => openCodeProposal(codeDetail.id)}>刷新记录</Button></>}
    </Drawer>
    <Modal title="维护验证依据" open={!!detail} onCancel={() => setDetail(null)} footer={null} width={720}><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 12 }}>{detail && JSON.stringify(detail, null, 2)}</pre></Modal>
  </>
}
