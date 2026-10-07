export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api/v1' + path, {
    ...options, credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  const data = await response.json()
  if (!response.ok) {
    const detail = data.detail
    throw new ApiError(response.status, typeof detail === 'string'
      ? detail : Array.isArray(detail) ? detail.map(x => x.msg).join('；') : detail?.message || '请求失败，请稍后重试')
  }
  return data
}

export const post = <T,>(path: string, body: unknown = {}) => api<T>(path, {
  method: 'POST', body: JSON.stringify(body),
})

type ActionSender = (path: string, body: unknown, key: string) => Promise<unknown>

/** One user action keeps its key across uncertain network/server results; no automatic retry. */
export class IdempotentActions {
  private requests = new Map<string, { key: string; pending?: Promise<unknown> }>()
  constructor(
    private send: ActionSender = (path, body, key) => api(path, {
      method: 'POST', body: JSON.stringify(body), headers: { 'Idempotency-Key': key },
    }),
    private newKey: () => string = () => crypto.randomUUID(),
  ) {}

  post<T>(path: string, body: unknown = {}): Promise<T> {
    const signature = path + '\n' + JSON.stringify(body)
    const entry = this.requests.get(signature) || { key: this.newKey() }
    if (entry.pending) return entry.pending as Promise<T>
    this.requests.set(signature, entry)
    entry.pending = Promise.resolve().then(() => this.send(path, body, entry.key)).then(result => {
      this.requests.delete(signature)
      return result
    }).catch(error => {
      // Timeouts, transient 5xx and unknown transport outcomes must replay the same key.
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status)) {
        this.requests.delete(signature)
      }
      throw error
    }).finally(() => {
      if (this.requests.get(signature) === entry) entry.pending = undefined
    })
    return entry.pending as Promise<T>
  }
}

export interface Company {
  id: string; name: string; official_name: string | null; role: string;
  website: string | null; notes: string; created_at: string; archived: boolean
}
export interface Source {
  id: string; company_id: string; kind: string; entry_url: string;
  health: string; adapter: string; last_success_at: string | null; enabled: boolean
  account_id: string | null; external_company_id: string | null;
}
export interface SourceAccount {
  id: string; kind: string; name: string; health: string; last_verified_at: string | null
}
export interface LoginSession {
  id: string; account_id: string; status: string; url: string; title: string;
  viewport: { width: number; height: number }
}
export interface Task {
  id: string; batch_id: string | null; company_id: string; kind: string; status: string; progress: number;
  created_at: string; finished_at: string | null; error_message: string | null;
  domain: string;
  config_snapshot: { company_name?: string; subject?: { name: string }; name?: string };
  summary: { counts?: Record<string, number>; valid_sources?: number; sources?: number }
}
export interface Report {
  id: string; title: string; domain: string; content: string; model: string | null; created_at: string;
  cleared_at?: string | null; retention?: Record<string, unknown>;
  evidence: { job_id?: string; item_id?: string; title: string; source_url?: string; url?: string }[];
}
export interface TaskDetail extends Task {
  sources: { id: string; status: string; coverage: string; error_message?: string; counts: Record<string, unknown>;
    checkpoint?: { status: string; saved_pages: number; raw_bytes: number; expires_at: string; within_time_limit: boolean } | null }[]
  events: { id: string; message: string; level: string; created_at: string }[]
}
export interface JobData {
  description?: string; requirements?: string; experience?: string; education?: string;
  salary_raw?: string; department?: string; source_updated_at?: string; recruiting_entity?: string
}
export interface JobSource {
  id: string; url: string; data: JobData; first_seen_at: string; last_seen_at: string; status: string
}
export interface Job {
  id: string; company_id: string; title: string; locations: string[]; function: string | null;
  status: string; sources: JobSource[]; updated_at: string
}
export interface Change {
  id: string; title: string; company_id: string; type: string; created_at: string;
  details: Record<string, unknown>
}
export interface JobDetail extends Job { versions: { id: string; data: JobData; created_at: string }[]; changes: Change[] }
export interface Dashboard {
  companies: number; jobs: number; sources: number; active_tasks: number;
  needs_action: number; changes_7d: number; latest_completed_at: string | null
  sentiment_items: number; unread_news: number; reports: number; active_subjects: number
}
export interface Plan {
  id: string; name: string; company_ids: string[]; schedule: string | null;
  timezone: string; enabled: boolean; next_run_at: string | null
  filters?: Record<string, unknown>
}
export interface SystemStatus {
  database: string; timezone: string; bettafish: { configured: boolean };
  llm: { configured: boolean; model: string; daily_request_limit: number; task_call_limit: number;
    max_output_tokens: number; calls_today: number }
}
