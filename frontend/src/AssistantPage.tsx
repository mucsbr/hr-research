import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Descriptions, Input, Space, Table, Tag } from 'antd'
import { api, post } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText } from './ModuleUI'

interface Reply {
  type: string; message: string; action_token?: string; action?: string; expires_at?: string; estimated_model_calls?: number | null; model_usage?: string;
  payload?: { names?: string[]; company_ids?: string[]; company_id?: string; days?: number; schedule?: string; timezone?: string; filters?: { cities?: string[]; keywords?: string[]; exclusions?: string[]; sources?: string[] } };
  items?: { id: string; title: string; status: string; locations: string[]; source_urls?: string[] }[];
  issues?: { input: string; instruction: string; candidates: { id: string; name: string; official_name?: string }[] }[];
  plans?: { id: string; name: string }[]; limitations?: string[];
}
interface Conversation { id: string; input: string; reply: Reply; executed?: boolean; result?: { task_id?: string; batch_id?: string; created?: number | boolean; review?: number; name?: string; state?: string } }
export default function AssistantPage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const capabilities = useQuery({ queryKey: ['assistant-capabilities'], queryFn: () => api<{ examples: string[]; limitations: string[] }>('/assistant/capabilities') })
  const companies = useQuery({ queryKey: ['companies'], queryFn: () => api<{ id: string; name: string }[]>('/companies') })
  const companyNames = Object.fromEntries((companies.data || []).map(c => [c.id, c.name]))
  const [input, setInput] = useState(''), [history, setHistory] = useState<Conversation[]>([]), [busy, setBusy] = useState(false), [executing, setExecuting] = useState<string | null>(null)
  async function send() {
    if (!input.trim()) return
    setBusy(true)
    try { const reply = await post<Reply>('/assistant/messages', { message: input }); setHistory(current => [...current, { id: crypto.randomUUID(), input, reply }]); setInput('') }
    catch (error) { message.error((error as Error).message) } finally { setBusy(false) }
  }
  async function execute(turn: Conversation) {
    setExecuting(turn.id)
    try {
      const result = await post<Conversation['result']>('/assistant/actions/execute', { action_token: turn.reply.action_token })
      setHistory(items => items.map(item => item.id === turn.id ? { ...item, executed: true, result } : item))
      for (const key of ['companies', 'plans', 'tasks', 'dashboard']) cache.invalidateQueries({ queryKey: [key] })
    } catch (error) { message.error((error as Error).message) } finally { setExecuting(null) }
  }
  return <>
    <ModuleTitle title="情报助手" description="用中文指令管理关注名单、发起采集、建立计划和查询已有岗位。" />
    <ModuleError error={capabilities.error} />
    <ModulePanel title="输入指令"><Input.TextArea value={input} onChange={e => setInput(e.target.value)} rows={3} placeholder="例如：查询岗位：公司名称；城市=上海；关键词=工程师" maxLength={3000} />
      <Space style={{ marginTop: 12 }}><Button type="primary" loading={busy} onClick={send}>查看结果或动作</Button><span className="subtle">解析指令不调用模型。写操作会先展示执行内容。</span></Space>
      <div className="note-box">{capabilities.data?.limitations.join(' ')} 下列名称仅用于格式示例，请替换为你的关注对象。
        <Space wrap style={{ marginTop: 8 }}>{capabilities.data?.examples.map(example => <Button key={example} type="link" size="small" onClick={() => setInput(example)}>{example}</Button>)}</Space>
      </div>
    </ModulePanel>
    {[...history].reverse().map(turn => <div key={turn.id} style={{ marginTop: 22 }}><ModulePanel title={turn.input}>
      <p>{turn.reply.message}</p>
      {turn.reply.issues?.map(issue => <div className="note-box" key={issue.input}><strong>{issue.input}</strong> · {issue.instruction}{issue.candidates.map(c => <p key={c.id}>{c.name}（{c.official_name || '未补充正式名称'}） · ID：{c.id}</p>)}</div>)}
      {turn.reply.plans?.map(plan => <p key={plan.id}>{plan.name} · ID：{plan.id}</p>)}
      {turn.reply.type === 'action_draft' && <><Descriptions column={1} items={[
        { label: '公司', children: turn.reply.payload?.names?.join('、') || (turn.reply.payload?.company_ids || (turn.reply.payload?.company_id ? [turn.reply.payload.company_id] : [])).map(id => companyNames[id] || id).join('、') || '见动作说明' },
        { label: '条件', children: turn.reply.payload?.filters ? ['城市：' + (turn.reply.payload.filters.cities?.join('、') || '不限'), '关键词：' + (turn.reply.payload.filters.keywords?.join('、') || '不限'), '排除：' + (turn.reply.payload.filters.exclusions?.join('、') || '无')].join('；') : '见动作说明' },
        { label: '模型调用', children: turn.reply.model_usage === 'conditional_website_adaptation' ? '常规采集不调用；新官网或改版适配可能使用受预算限制的模型请求' : turn.reply.estimated_model_calls ? `预计 ${turn.reply.estimated_model_calls} 次，执行前检查预算` : '不调用模型' },
        { label: '可执行至', children: dateText(turn.reply.expires_at) },
      ]} />{turn.executed ? <Space><Tag color="green">已执行</Tag>{turn.result?.task_id || turn.result?.batch_id ? <Link to="/tasks">查看任务</Link> : <Link to={turn.reply.action?.includes('plan') ? '/plans' : '/companies'}>查看更新结果</Link>}</Space> : <Button type="primary" loading={executing === turn.id} onClick={() => execute(turn)}>执行上述动作</Button>}</>}
      {turn.reply.items && <Table rowKey="id" dataSource={turn.reply.items} pagination={{ pageSize: 10 }} columns={[
        { title: '岗位', dataIndex: 'title' }, { title: '地点', dataIndex: 'locations', render: (v: string[]) => v.join('、') },
        { title: '状态', dataIndex: 'status', render: v => v === 'suspected_removed' ? '疑似下架' : '已观察到' },
        { title: '原文', render: (_, job: NonNullable<Reply['items']>[number]) => job.source_urls?.map((url, index) => <a key={url} href={url} target="_blank" rel="noreferrer">来源 {index + 1} </a>) },
      ]} />}
      {turn.reply.limitations?.map(value => <p key={value} className="subtle">{value}</p>)}
    </ModulePanel></div>)}
  </>
}
