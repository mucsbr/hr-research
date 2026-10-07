// Executes the actual TypeScript API helper with fetch replaced; no server or external network.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from '../frontend/node_modules/typescript/lib/typescript.js'

const source = await readFile(new URL('../frontend/src/api.ts', import.meta.url), 'utf8')
const javascript = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { IdempotentActions, ApiError } = await import('data:text/javascript;base64,' + Buffer.from(javascript).toString('base64'))
const originalFetch = globalThis.fetch
const response = (status = 202) => new Response(JSON.stringify(status < 400 ? { batch_id: 'one-batch' } : { detail: 'test response' }), { status, headers: { 'Content-Type': 'application/json' } })
let count = 0
async function test(name, run) { await run(); count++; console.log('PASS ' + name) }

try {
  await test('in-flight double click shares one POST and one header', async () => {
    const requests = []
    let release
    globalThis.fetch = async (url, options) => {
      requests.push({ url, options })
      return new Promise(resolve => { release = resolve })
    }
    const actions = new IdempotentActions(undefined, () => 'stable-one')
    const first = actions.post('/companies/a/recruitment-runs')
    const second = actions.post('/companies/a/recruitment-runs')
    assert.equal(first, second)
    await Promise.resolve()
    assert.equal(requests.length, 1)
    assert.equal(requests[0].options.headers['Idempotency-Key'], 'stable-one')
    assert.equal(requests[0].options.credentials, 'include')
    release(response())
    await first
  })

  await test('network retry retains key; success starts a new action', async () => {
    const keys = []
    let serial = 0
    globalThis.fetch = async (_, options) => {
      keys.push(options.headers['Idempotency-Key'])
      if (keys.length === 1) throw new TypeError('uncertain network outcome')
      return response()
    }
    const actions = new IdempotentActions(undefined, () => 'action-' + ++serial)
    await assert.rejects(actions.post('/plans/a/run'), TypeError)
    await actions.post('/plans/a/run')
    await actions.post('/plans/a/run')
    assert.deepEqual(keys, ['action-1', 'action-1', 'action-2'])
  })

  await test('uncertain 503 retries reuse key; definitive 409 releases it', async () => {
    const keys = [], statuses = [503, 409, 202]
    let serial = 0
    globalThis.fetch = async (_, options) => {
      keys.push(options.headers['Idempotency-Key'])
      return response(statuses.shift())
    }
    const actions = new IdempotentActions(undefined, () => 'action-' + ++serial)
    await assert.rejects(actions.post('/plans/a/run'), ApiError)
    await assert.rejects(actions.post('/plans/a/run'), ApiError)
    await actions.post('/plans/a/run')
    assert.deepEqual(keys, ['action-1', 'action-1', 'action-2'])
  })

  await test('different companies and filters do not share action keys', async () => {
    const keys = []
    let serial = 0
    globalThis.fetch = async (_, options) => {
      keys.push(options.headers['Idempotency-Key'])
      throw new TypeError('keep actions unresolved')
    }
    const actions = new IdempotentActions(undefined, () => 'action-' + ++serial)
    await assert.rejects(actions.post('/companies/a/recruitment-runs'))
    await assert.rejects(actions.post('/companies/b/recruitment-runs'))
    await assert.rejects(actions.post('/recruitment/runs', { filters: { cities: ['上海'] } }))
    await assert.rejects(actions.post('/recruitment/runs', { filters: { cities: ['北京'] } }))
    assert.equal(new Set(keys).size, 4)
  })
  console.log(`${count} frontend idempotency semantics passed; external requests: 0`)
} finally {
  globalThis.fetch = originalFetch
}
