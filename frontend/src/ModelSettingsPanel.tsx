import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Collapse, Descriptions, Form, Input, InputNumber, Modal, Select, Switch, Tag } from 'antd'
import { api } from './api'
import type { SystemStatus } from './api'
import { ModuleError, ModulePanel } from './ModuleUI'

interface Configuration {
  base_url: string; model: string; fallback_model: string; has_api_key: boolean; enabled: boolean; configured: boolean;
  daily_request_limit: number; task_call_limit: number; max_output_tokens: number; max_input_chars: number;
  timeout_seconds: number; reasoning_effort: string | null; output_token_parameter: string; revision: number; origin: string;
}

export default function ModelSettingsPanel({ isAdmin, summary }: { isAdmin: boolean; summary?: SystemStatus['llm'] }) {
  const { message } = App.useApp(), cache = useQueryClient(), [form] = Form.useForm()
  const configuration = useQuery({ queryKey: ['model-settings'], queryFn: () => api<Configuration>('/settings/models'), enabled: isAdmin })
  const [editing, setEditing] = useState(false), [saving, setSaving] = useState(false), [revision, setRevision] = useState(0)
  const clearKey = Form.useWatch('clear_api_key', form)
  const data = configuration.data
  const configured = isAdmin ? data?.configured : summary?.configured
  function open() {
    if (!data) return
    form.resetFields(); form.setFieldsValue({ ...data, api_key: '', clear_api_key: false, reasoning_effort: data.reasoning_effort || '' })
    setRevision(data.revision); setEditing(true)
  }
  return <ModulePanel title="模型与成本控制" extra={isAdmin && <Button onClick={open} disabled={!data}>配置模型</Button>}>
    <ModuleError error={configuration.error} />
    <Descriptions column={1} items={[
      { label: '连接配置', children: configured ? <Tag color="green">已配置</Tag> : <Tag>未启用或待配置</Tag> },
      ...(isAdmin ? [{ label: 'Base URL', children: data?.base_url || '—' }, { label: 'API Key', children: data?.has_api_key ? '已保存 · 不回显' : '未配置' }] : []),
      { label: '模型', children: (isAdmin ? data?.model : summary?.model) || '—' },
      { label: '每日请求上限', children: (isAdmin ? data?.daily_request_limit : summary?.daily_request_limit) ?? '—' },
      { label: '单任务调用上限', children: (isAdmin ? data?.task_call_limit : summary?.task_call_limit) ?? '—' },
      { label: '单次输出上限', children: ((isAdmin ? data?.max_output_tokens : summary?.max_output_tokens) ?? '—') + ' tokens' },
      { label: '今日调用记录', children: summary?.calls_today ?? '—' },
    ]} />
    <div className="note-box">常规采集、历史比较和页面刷新不调用模型。保存配置不发送模型请求，后续调用使用新配置；更换模型保留已累计的调用预算。</div>
    {!isAdmin && <p className="subtle">模型连接由管理员配置。</p>}
    <Modal title="配置模型与成本控制" open={editing} onCancel={() => { setEditing(false); form.resetFields() }} onOk={() => form.submit()} confirmLoading={saving} okText="保存配置" width={760}>
      <Alert type="info" showIcon message="填写服务商提供的 Base URL、API Key 和模型名" description="支持 OpenAI 兼容的 Chat Completions 接口，也支持可访问的内网模型服务。保存只校验配置，不进行付费连通测试。" style={{ marginBottom: 20 }} />
      <Form form={form} layout="vertical" onFinish={async values => {
        setSaving(true)
        try {
          await api('/settings/models', { method: 'PUT', body: JSON.stringify({ ...values, expected_revision: revision, api_key: values.api_key || undefined, reasoning_effort: values.reasoning_effort || null }) })
          form.resetFields(); setEditing(false)
          await Promise.all([cache.invalidateQueries({ queryKey: ['model-settings'] }), cache.invalidateQueries({ queryKey: ['system'] }), cache.invalidateQueries({ queryKey: ['sentiment', 'status'] }), cache.invalidateQueries({ queryKey: ['audit-events'] })])
          message.success('模型配置已保存，后续调用生效')
        } catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
      }}>
        <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]} extra="例如 https://provider.example/v1，按服务商地址填写；完整 /chat/completions 地址也可粘贴。"><Input placeholder="https://provider.example/v1" maxLength={2000} autoComplete="off" /></Form.Item>
        <Form.Item name="api_key" label="API Key" extra={data?.has_api_key ? '留空保留原密钥；更换 Base URL 时请填写对应服务的新密钥。' : '填写服务商密钥。密钥加密保存，不会回显。'}><Input.Password disabled={clearKey} placeholder={data?.has_api_key ? '已保存，填写可替换' : '填写 API Key'} autoComplete="new-password" maxLength={4096} /></Form.Item>
        <Form.Item name="model" label="模型名" rules={[{ required: true }]} extra="自由填写服务商实际提供的模型名或部署ID。"><Input placeholder="服务商的模型名或部署ID" maxLength={300} /></Form.Item>
        <Form.Item name="enabled" label="启用模型调用" valuePropName="checked"><Switch /></Form.Item>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '0 18px' }}>
          <Form.Item name="daily_request_limit" label="每日请求上限" rules={[{ required: true }]} extra="0表示暂停发送请求，失败和未知结果也计入。"><InputNumber min={0} max={10000} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="task_call_limit" label="单任务调用上限" rules={[{ required: true }]}><InputNumber min={0} max={1000} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="max_output_tokens" label="单次输出上限（tokens）" rules={[{ required: true }]}><InputNumber min={1} max={131072} precision={0} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="max_input_chars" label="单次输入上限（字符）" rules={[{ required: true }]}><InputNumber min={1} max={2000000} precision={0} style={{ width: '100%' }} /></Form.Item>
        </div>
        <Collapse items={[{ key: 'advanced', label: '更多参数', forceRender: true, children: <>
          <Form.Item name="timeout_seconds" label="请求超时（秒）" rules={[{ required: true }]}><InputNumber min={1} max={600} precision={0} /></Form.Item>
          <Form.Item name="fallback_model" label="备用模型名（可选）" extra="与主模型共用当前连接；不会因失败或超时自动切换。"><Input maxLength={300} /></Form.Item>
          <Form.Item name="reasoning_effort" label="推理参数（可选）" extra="默认不发送。仅在服务商和模型支持时设置。"><Select options={['', 'none', 'minimal', 'low', 'medium', 'high', 'max'].map(value => ({ value, label: value || '不发送' }))} /></Form.Item>
          <Form.Item name="output_token_parameter" label="输出上限参数" extra="按服务商接口要求选择，默认 max_tokens。"><Select options={['max_tokens', 'max_completion_tokens'].map(value => ({ value, label: value }))} /></Form.Item>
          <Form.Item name="clear_api_key" label="清除已保存的密钥" valuePropName="checked"><Switch onChange={value => { if (value) form.setFieldsValue({ api_key: '', enabled: false }) }} /></Form.Item>
        </> }]} />
      </Form>
    </Modal>
  </ModulePanel>
}
