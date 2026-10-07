const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
})

function parts(value: string | Date) {
  const date = value instanceof Date ? value : new Date(value)
  if (!Number.isFinite(date.getTime())) return null
  return Object.fromEntries(formatter.formatToParts(date).map(part => [part.type, part.value]))
}

/** Keep source dates at their stated precision; format timestamp instants in Beijing. */
export function beijingDateTime(value?: string | Date | null, short = false): string {
  if (!value) return '—'
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const date = new Date(value + 'T00:00:00Z')
    return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? value : '—'
  }
  const fields = parts(value)
  if (!fields) return '—'
  return `${short ? '' : fields.year + '-'}${fields.month}-${fields.day} ${fields.hour}:${fields.minute}`
}

export function beijingCalendarDate(value: Date = new Date()): string {
  const fields = parts(value)
  return fields ? `${fields.year} 年 ${Number(fields.month)} 月 ${Number(fields.day)} 日` : '—'
}
