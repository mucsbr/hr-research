import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag } from 'antd'
import { api, post } from './api'
import { ModuleError, ModulePanel, dateText } from './ModuleUI'

interface User { id: string; username: string; role: string; active: boolean; created_at: string }
interface Audit { id: string; action: string; target: string; created_at: string }
export default function AdminSettings() {
  const { message } = App.useApp(), cache = useQueryClient()
  const me = useQuery({ queryKey: ['me'], queryFn: () => api<{ id: string; role: string }>('/auth/me') })
  const isAdmin = me.data?.role === 'admin'
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<User[]>('/users'), enabled: isAdmin })
  const audit = useQuery({ queryKey: ['audit-events'], queryFn: () => api<Audit[]>('/audit-events'), enabled: isAdmin })
  const [open, setOpen] = useState(false), [saving, setSaving] = useState(false), [form] = Form.useForm()
  async function modify(user: User, changes: Partial<User>) {
    try {
      await api('/users/' + user.id, { method: 'PATCH', body: JSON.stringify(changes) })
      cache.invalidateQueries({ queryKey: ['users'] }); cache.invalidateQueries({ queryKey: ['audit-events'] })
      if (user.id === me.data?.id) cache.invalidateQueries({ queryKey: ['me'] })
      message.success('已更新，原登录会话已撤销')
    } catch (error) { message.error((error as Error).message) }
  }
  if (!isAdmin) return null
  return <><div style={{ height: 22 }} /><ModuleError error={users.error || audit.error} />
    <ModulePanel title="内部用户" extra={<Button onClick={() => setOpen(true)}>新增用户</Button>}>
      <Table rowKey="id" dataSource={users.data} columns={[
        { title: '用户名', dataIndex: 'username' }, { title: '角色', dataIndex: 'role', render: v => v === 'admin' ? '管理员' : '普通用户' },
        { title: '状态', dataIndex: 'active', render: v => <Tag color={v ? 'green' : undefined}>{v ? '启用' : '停用'}</Tag> },
        { title: '操作', render: (_, user: User) => <Space>
          <Popconfirm title={user.active ? '停用此用户并结束其登录会话？' : '重新启用此用户？'} onConfirm={() => modify(user, { active: !user.active })}><Button size="small">{user.active ? '停用' : '启用'}</Button></Popconfirm>
          <Popconfirm title="变更该用户权限并结束其登录会话？" onConfirm={() => modify(user, { role: user.role === 'admin' ? 'user' : 'admin' })}><Button size="small">{user.role === 'admin' ? '改为普通用户' : '设为管理员'}</Button></Popconfirm>
        </Space> },
      ]} />
    </ModulePanel><div style={{ height: 22 }} /><ModulePanel title="最近管理操作">
      <Table rowKey="id" dataSource={audit.data} pagination={{ pageSize: 10 }} columns={[
        { title: '操作', dataIndex: 'action' }, { title: '记录', dataIndex: 'target', ellipsis: true }, { title: '时间', dataIndex: 'created_at', render: dateText },
      ]} />
    </ModulePanel>
    <Modal title="新增内部用户" open={open} onCancel={() => setOpen(false)} onOk={() => form.submit()} okText="创建用户" confirmLoading={saving}>
      <Form form={form} layout="vertical" initialValues={{ role: 'user' }} onFinish={async values => {
        setSaving(true)
        try { await post('/users', values); form.resetFields(); setOpen(false); cache.invalidateQueries({ queryKey: ['users'] }); cache.invalidateQueries({ queryKey: ['audit-events'] }) }
        catch (error) { message.error((error as Error).message) } finally { setSaving(false) }
      }}>
        <Form.Item name="username" label="用户名" rules={[{ required: true }]}><Input autoComplete="off" /></Form.Item>
        <Form.Item name="password" label="初始密码" rules={[{ required: true }, { min: 12 }]}><Input.Password autoComplete="new-password" /></Form.Item>
        <Form.Item name="role" label="角色"><Select options={[{ value: 'user', label: '普通用户' }, { value: 'admin', label: '管理员' }]} /></Form.Item>
      </Form>
    </Modal>
  </>
}
