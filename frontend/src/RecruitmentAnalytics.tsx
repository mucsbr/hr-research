import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { App, Button, Descriptions, Select, Space, Table, Tag } from 'antd'
import { api, post } from './api'
import type { Company } from './api'
import { ModuleError, ModulePanel, ModuleTitle, dateText, stateTag } from './ModuleUI'

interface Point { date: string; coverage: string; scope_changed: boolean; trend_comparable: boolean; changes: Record<string, number | null> }
interface Metrics {
  company_id: string; company_name: string; as_of: string; data_status: string;
  observed_jobs: number | null; observed_source_records: number | null; comparable_jobs: number | null;
  cities: Record<string, number> | null; functions: Record<string, number> | null;
  source_coverage: { source_id: string; kind: string; url: string; status: string; coverage: string; last_attempt_at: string | null }[];
  daily_series: Point[]; limitations: string[];
}
const valueText = (value: number | null | undefined) => value == null ? '未知' : value
const kinds: Record<string, string> = { careers: '官网', boss: 'BOSS', liepin: '猎聘', zhaopin: '智联', '51job': '前程无忧' }

function Trend({ data }: { data: Point[] }) {
  const max = Math.max(1, ...data.flatMap(p => [p.changes.first_seen || 0, p.changes.suspected_removed || 0]))
  const width = 660, gap = width / Math.max(data.length, 1)
  const hasPoints = data.some(p => p.changes.first_seen !== null || p.changes.suspected_removed !== null)
  if (!hasPoints) return <p className="subtle">所选窗口尚无可用日序列。未采集日期保持空值。</p>
  return <><svg viewBox="0 0 720 225" role="img" aria-label="按来源统计的每日岗位首次发现与疑似下架事件" style={{ width: '100%', maxHeight: 260 }}>
    <line x1="38" y1="182" x2="700" y2="182" stroke="#d5dad0" />
    <text x="8" y="27" fill="#879184" fontSize="12">{max}</text><text x="14" y="185" fill="#879184" fontSize="12">0</text>
    {data.map((p, index) => <g key={p.date}>{(['first_seen', 'suspected_removed'] as const).map((type, n) => {
      const value = p.changes[type]
      if (value === null || value === undefined) return null
      return <rect key={type} x={40 + index * gap + n * gap * .4} y={182 - value / max * 150} width={Math.max(.7, gap * .35)} height={value / max * 150} fill={n === 0 ? '#567d6b' : '#c28061'}><title>{p.date} · {n === 0 ? '首次发现' : '疑似下架'}：{value} · {p.coverage}</title></rect>
    })}</g>)}
    <text x="40" y="211" fill="#879184" fontSize="12">{data[0]?.date}</text><text x="700" y="211" textAnchor="end" fill="#879184" fontSize="12">{data.at(-1)?.date}</text>
  </svg><Space><Tag color="green">首次发现</Tag><Tag color="orange">疑似下架</Tag><span className="subtle">图表仅绘制已有事件；缺失日期见下表。</span></Space></>
}

export default function RecruitmentAnalytics() {
  const companies = useQuery({ queryKey: ['companies'], queryFn: () => api<Company[]>('/companies') })
  const [ids, setIds] = useState<string[]>([]), [days, setDays] = useState(30), [busy, setBusy] = useState(false)
  const { message } = App.useApp(), navigate = useNavigate()
  const metrics = useQuery({ queryKey: ['recruitment-metrics', ids[0], days], queryFn: () => api<Metrics>('/recruitment/metrics?company_id=' + ids[0] + '&days=' + days), enabled: ids.length > 0 })
  const data = metrics.data
  async function compare() {
    setBusy(true)
    try { await post('/recruitment/comparisons', { company_ids: ids, days, idempotency_key: crypto.randomUUID() }); message.success('比较任务已创建，不调用模型'); navigate('/tasks') }
    catch (error) { message.error((error as Error).message) } finally { setBusy(false) }
  }
  return <>
    <ModuleTitle title="招聘趋势与比较" description="按同一时间窗口查看岗位变化，保留未知值和不同来源的覆盖差异。" />
    <div className="filter-bar"><Select mode="multiple" placeholder="选择一家公司查看趋势，选择多家可创建比较" style={{ minWidth: 290, flex: 1 }} value={ids} onChange={setIds} options={companies.data?.map(c => ({ value: c.id, label: c.name }))} />
      <Select value={days} onChange={setDays} style={{ width: 130 }} options={[7, 30, 90].map(value => ({ value, label: '近 ' + value + ' 天' }))} /><Button disabled={ids.length < 2} loading={busy} type="primary" onClick={compare}>创建比较报告</Button></div>
    <ModuleError error={companies.error || metrics.error} />
    {!ids.length ? <div className="note-box">先选择公司。公司名单由你指定，平台不会自动加入竞品。</div> : data && <>
      <ModulePanel title={data.company_name + ' · 当前观察'} extra={stateTag(data.data_status)}><Descriptions column={3} items={[
        { label: '已观察岗位', children: valueText(data.observed_jobs) }, { label: '来源岗位记录', children: valueText(data.observed_source_records) },
        { label: '完整范围岗位数', children: valueText(data.comparable_jobs) }, { label: '统计截至', children: dateText(data.as_of) },
        { label: '地点分布', children: data.cities ? Object.entries(data.cities).map(([city, count]) => city + ' ' + count).join('、') : '未知' },
        { label: '职能分布', children: data.functions ? Object.entries(data.functions).map(([name, count]) => name + ' ' + count).join('、') : '未知' },
      ]} /><div className="note-box">{data.limitations.join(' ')}</div></ModulePanel>
      <div style={{ height: 22 }} /><ModulePanel title="每日岗位变化"><Trend data={data.daily_series} />
        <Table rowKey="date" size="small" dataSource={[...data.daily_series].reverse()} pagination={{ pageSize: 10 }} columns={[
          { title: '日期', dataIndex: 'date' }, { title: '首次发现', render: (_, p: Point) => valueText(p.changes.first_seen) },
          { title: '内容变更', render: (_, p: Point) => valueText(p.changes.content_changed) }, { title: '疑似下架', render: (_, p: Point) => valueText(p.changes.suspected_removed) },
          { title: '覆盖', dataIndex: 'coverage', render: v => v === 'complete' ? '完整' : v === 'partial' ? '部分' : '未知' },
          { title: '可比较', render: (_, p: Point) => p.trend_comparable ? '是' : p.scope_changed ? '范围发生变化' : '覆盖不足' },
        ]} /></ModulePanel>
      <div style={{ height: 22 }} /><ModulePanel title="来源覆盖"><Table rowKey="source_id" dataSource={data.source_coverage} pagination={false} columns={[
        { title: '平台', dataIndex: 'kind', render: v => kinds[v] || v }, { title: '状态', dataIndex: 'status', render: stateTag },
        { title: '覆盖', dataIndex: 'coverage', render: v => <Tag>{v === 'complete' ? '范围完整' : v === 'partial' ? '部分' : '未知'}</Tag> },
        { title: '最近采集', dataIndex: 'last_attempt_at', render: dateText }, { title: '入口', dataIndex: 'url', render: v => <a href={v} target="_blank" rel="noreferrer">查看来源</a> },
      ]} /></ModulePanel>
    </>}
  </>
}
