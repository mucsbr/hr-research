import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, App, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { api, post } from './api'
import { ModuleError, ModulePanel, ModuleTitle } from './ModuleUI'

interface Device { id: string; name: string; platform: string; architecture: string; status: string; version: string; chrome_version: string | null; last_seen_at: string | null; enabled: boolean }
interface Profile { id: string; name: string; device_id: string; device_name: string; device_status: string; enabled: boolean }
interface Release { id: string; filename: string; platform: string; architecture: string; version: string; signature_status: string; native_validation: string; sha256: string }
const statuses: Record<string, string> = { online: '在线', offline: '离线', paused: '已暂停', revoked: '已撤销' }

export default function DevicesPage() {
  const { message } = App.useApp(), cache = useQueryClient()
  const devices = useQuery({ queryKey: ['probe-devices'], queryFn: () => api<Device[]>('/probes/devices'), refetchInterval: 5000 })
  const profiles = useQuery({ queryKey: ['probe-profiles'], queryFn: () => api<Profile[]>('/probes/profiles') })
  const downloads = useQuery({ queryKey: ['probe-downloads'], queryFn: () => api<{ releases: Release[]; server: string | null }>('/probes/downloads') })
  const [pairing, setPairing] = useState<{ pairing_config: string; server: string; expires_at: string } | null>(null)
  const [profileOpen, setProfileOpen] = useState(false), [form] = Form.useForm()
  const pair = useMutation({ mutationFn: () => post<{ pairing_config: string; server: string; expires_at: string }>('/probes/pairings'), onSuccess: setPairing, onError: (e: Error) => message.error(e.message) })
  const addProfile = useMutation({ mutationFn: (value: { device_id: string; name: string }) => post('/probes/profiles', value), onSuccess: () => { setProfileOpen(false); form.resetFields(); cache.invalidateQueries({ queryKey: ['probe-profiles'] }) }, onError: (e: Error) => message.error(e.message) })
  return <>
    <ModuleTitle title="采集设备" description="安装桌面探针，在自己的Chrome中登录；设备主动连接平台完成采集。" />
    <ModuleError error={devices.error || profiles.error || downloads.error} />
    <Alert type="info" showIcon message="下载安装 → 配对连接 → Chrome登录 → 绑定来源" description="探针使用专用Chrome资料目录。登录和采集都在同一台设备完成；电脑休眠或探针离线时，平台会保留任务进度并提示处理。" style={{ marginBottom: 20 }} />
    <ModulePanel title="1. 下载探针并连接" extra={<Button type="primary" loading={pair.isPending} onClick={() => pair.mutate()}>生成一次性配对配置</Button>}>
      <Space wrap align="start">
        {downloads.data?.releases.map(r => <Card key={r.id} size="small" title={(r.platform === 'win32' ? 'Windows' : 'macOS') + ' · ' + r.architecture} style={{ width: 310 }}>
          <p>版本 {r.version}</p>
          {r.signature_status !== 'signed' && <Tag color="orange">内部未签名构建</Tag>}
          {r.native_validation !== 'passed' && <Tag>原生系统验收待完成</Tag>}
          <p><a href={'/api/v1/probes/downloads/' + r.id}>下载安装包</a></p>
        </Card>)}
      </Space>
      {downloads.data && !downloads.data.releases.length && <Alert type="warning" message="安装包尚未发布" description="设备管理接口可用不代表Windows/macOS安装包已经交付。" />}
    </ModulePanel>
    <ModulePanel title="2. 已配对的采集设备" extra={<Button onClick={() => { devices.refetch(); profiles.refetch() }}>刷新设备</Button>}>
      <Table rowKey="id" dataSource={devices.data} loading={devices.isPending} columns={[
        { title: '设备', dataIndex: 'name' },
        { title: '系统', render: (_, d: Device) => (d.platform === 'win32' ? 'Windows' : 'macOS') + ' · ' + d.architecture },
        { title: '状态', render: (_, d: Device) => <Tag color={d.status === 'online' ? 'green' : 'default'}>{statuses[d.status] || d.status}</Tag> },
        { title: '探针版本', dataIndex: 'version' }, { title: 'Chrome', render: (_, d: Device) => d.chrome_version || '尚未打开' },
        { title: '操作', render: (_, d: Device) => <Button disabled={!d.enabled} onClick={() => Modal.confirm({ title: '撤销这台设备？', content: '平台将停止下发任务，已有业务数据和本机Chrome资料保留。', onOk: async () => { await post('/probes/devices/' + d.id + '/revoke'); cache.invalidateQueries({ queryKey: ['probe-devices'] }); cache.invalidateQueries({ queryKey: ['probe-profiles'] }) } })}>撤销配对</Button> },
      ]} />
    </ModulePanel>
    <ModulePanel title="3. 采集浏览器" extra={<Button onClick={() => setProfileOpen(true)}>新增独立浏览器资料</Button>}>
      <Table rowKey="id" dataSource={profiles.data} columns={[
        { title: '设备', dataIndex: 'device_name' }, { title: '浏览器备注', dataIndex: 'name' },
        { title: '状态', render: (_, p: Profile) => statuses[p.device_status] || p.device_status },
      ]} />
      <p>先在探针里选择平台并完成Chrome登录，再到“数据源与登录”绑定账号和公司来源。不同平台可共享一个采集浏览器；同一平台的不同身份请使用独立浏览器资料。</p>
    </ModulePanel>
    <Modal title="配对这台电脑" open={!!pairing} onCancel={() => setPairing(null)} footer={<Button onClick={() => setPairing(null)}>完成</Button>}>
      <p>平台地址：{pairing?.server}</p><p>在探针中粘贴下面的配置。配置仅可使用一次，十分钟内有效；请仅交给你要配对的设备。</p>
      <Typography.Paragraph copyable={{ text: pairing?.pairing_config }}><Input.TextArea readOnly value={pairing?.pairing_config} rows={4} /></Typography.Paragraph>
    </Modal>
    <Modal title="新增独立浏览器资料" open={profileOpen} onCancel={() => setProfileOpen(false)} onOk={() => form.submit()} confirmLoading={addProfile.isPending}>
      <Form form={form} layout="vertical" onFinish={v => addProfile.mutate(v)}>
        <Form.Item name="device_id" label="采集设备" rules={[{ required: true }]}><Select options={devices.data?.filter(d => d.enabled).map(d => ({ value: d.id, label: d.name }))} /></Form.Item>
        <Form.Item name="name" label="浏览器备注" rules={[{ required: true }]}><Input maxLength={120} /></Form.Item>
      </Form>
    </Modal>
  </>
}
