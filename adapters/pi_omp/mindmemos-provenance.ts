// MindMemOS durable completed-turn capture for Pi and OMP.
// Uses agent_end because turn_end can fire multiple times around tool calls.

const fs = require('fs')
const path = require('path')
const crypto = require('crypto')

export type CompletedPair = {
  user: string
  assistant: string
  userTimestamp?: number
  assistantTimestamp?: number
  turnId: string
}

function textContent(content: unknown): string {
  if (typeof content === 'string') return content.trim()
  if (!Array.isArray(content)) return ''
  return content
    .filter((part) => part && typeof part === 'object' && ['text', 'input_text', 'output_text'].includes(String((part as any).type)))
    .map((part) => String((part as any).text || (part as any).content || ''))
    .filter(Boolean)
    .join('\n')
    .trim()
}

export function extractLatestCompletedPair(messages: unknown[]): CompletedPair | null {
  if (!Array.isArray(messages)) return null
  let assistantIndex = -1
  let assistantMessage: any = null
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message: any = messages[index]
    if (message?.role !== 'assistant') continue
    const text = textContent(message.content)
    if (!text || message.stopReason === 'toolUse') continue
    assistantIndex = index
    assistantMessage = message
    break
  }
  if (assistantIndex < 0) return null

  let userMessage: any = null
  for (let index = assistantIndex - 1; index >= 0; index -= 1) {
    const message: any = messages[index]
    if (message?.role === 'user' && !message.synthetic && textContent(message.content)) {
      userMessage = message
      break
    }
  }
  if (!userMessage) return null

  const user = textContent(userMessage.content)
  const assistant = textContent(assistantMessage.content)
  const turnId = String(
    assistantMessage.responseId
      || assistantMessage.id
      || assistantMessage.timestamp
      || hashJson([userMessage.timestamp, assistantMessage.timestamp, assistant])
  )
  return {
    user,
    assistant,
    ...(typeof userMessage.timestamp === 'number' ? { userTimestamp: userMessage.timestamp } : {}),
    ...(typeof assistantMessage.timestamp === 'number' ? { assistantTimestamp: assistantMessage.timestamp } : {}),
    turnId,
  }
}

function hashJson(value: unknown): string {
  return crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex')
}

export function eventIdForPair(sessionId: string, pair: CompletedPair): string {
  return `pi-omp-${hashJson([
    sessionId,
    pair.turnId,
    pair.userTimestamp || null,
    pair.assistantTimestamp || null,
    pair.user,
    pair.assistant,
  ])}`
}

/** Capture a completed turn from an embedding runtime such as pi-feishu-lark. */
export async function captureCompletedTurn(
  user: string,
  assistant: string,
  sessionId: string,
  turnId?: string,
): Promise<string | null> {
  const cleanUser = String(user || '').trim()
  const cleanAssistant = String(assistant || '').trim()
  if (!cleanUser || !cleanAssistant) return null
  const pair: CompletedPair = {
    user: cleanUser,
    assistant: cleanAssistant,
    turnId: turnId || hashJson([sessionId, cleanUser, cleanAssistant]),
  }
  const payload: Record<string, unknown> = {
    event_id: eventIdForPair(sessionId, pair),
    session_id: sessionId,
    turn_id: pair.turnId,
    user_message: pair.user,
    assistant_message: pair.assistant,
    safe_context: { runtime: runtimeKind(), hook: 'embedded_turn_complete' },
  }
  const config = loadConfig()
  spoolEvent(config, payload)
  await flushSpool(config)
  return String(payload.event_id)
}

function runtimeKind(): string {
  const names = [process.title, process.argv[0], process.argv[1], process.env._]
    .map((value) => String(value || '').split(/[\\/]/).pop()?.toLowerCase() || '')
  return names.some((name) => ['omp', 'omp.js', 'omp.sh', 'omp.exe'].includes(name)) ? 'omp' : 'pi'
}

function loadConfig(): Record<string, any> {
  const configPath = process.env.MINDMEMOS_PI_CONFIG || path.join(process.env.HOME || '', '.config/mindmemos/pi-omp.json')
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function readKey(config: Record<string, any>): string {
  if (process.env.MINDMEMOS_INGEST_KEY) return process.env.MINDMEMOS_INGEST_KEY
  if (!config.key_file) return ''
  try {
    return String(fs.readFileSync(path.resolve(String(config.key_file)), 'utf8')).trim()
  } catch {
    return ''
  }
}

function atomicWrite(file: string, value: unknown): void {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 })
  const temporary = `${file}.${process.pid}.${Date.now()}.tmp`
  fs.writeFileSync(temporary, JSON.stringify(value), { encoding: 'utf8', mode: 0o600 })
  fs.renameSync(temporary, file)
}

function spoolEvent(config: Record<string, any>, payload: Record<string, unknown>): void {
  const spoolDir = path.resolve(String(
    config.spool_dir || path.join(process.env.HOME || '', '.local/state/mindmemos/pi-omp-spool')
  ))
  const file = path.join(spoolDir, `${payload.event_id}.json`)
  if (fs.existsSync(file)) return
  atomicWrite(file, {
    payload,
    status: 'pending',
    attempts: 0,
    next_attempt_at: 0,
    created_at: Date.now(),
  })
}

async function postWithTimeout(url: string, token: string, payload: unknown, timeoutMs: number): Promise<boolean> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), timeoutMs)
  if (typeof timeout.unref === 'function') timeout.unref()
  try {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    if (!response.ok) return false
    const body: any = await response.json().catch(() => ({}))
    return body?.ok === true
  } catch {
    return false
  } finally {
    clearTimeout(timeout)
  }
}

async function flushSpool(config: Record<string, any>, limit = 10): Promise<void> {
  const token = readKey(config)
  if (!token) return
  const serviceUrl = String(config.service_url || 'http://127.0.0.1:8765').replace(/\/$/, '')
  const spoolDir = path.resolve(String(
    config.spool_dir || path.join(process.env.HOME || '', '.local/state/mindmemos/pi-omp-spool')
  ))
  let files: string[] = []
  try {
    files = fs.readdirSync(spoolDir)
      .filter((name: string) => name.endsWith('.json'))
      .sort()
      .slice(0, limit)
  } catch {
    return
  }

  for (const name of files) {
    const file = path.join(spoolDir, name)
    let item: any
    try {
      item = JSON.parse(fs.readFileSync(file, 'utf8'))
    } catch {
      continue
    }
    if (Number(item.next_attempt_at || 0) > Date.now()) continue
    const accepted = await postWithTimeout(
      `${serviceUrl}/ingest/turn`,
      token,
      item.payload,
      Number(config.timeout_ms || 750),
    )
    if (accepted) {
      try { fs.unlinkSync(file) } catch {}
      continue
    }
    const attempts = Number(item.attempts || 0) + 1
    const delay = Math.min(300_000, 2_000 * (2 ** Math.min(attempts - 1, 8)))
    atomicWrite(file, {
      ...item,
      status: 'error',
      attempts,
      next_attempt_at: Date.now() + delay,
      updated_at: Date.now(),
    })
    break
  }
}

function branchMessages(ctx: any): unknown[] {
  try {
    return ctx.sessionManager.getBranch()
      .map((entry: any) => entry?.message)
      .filter(Boolean)
  } catch {
    return []
  }
}

function isPrimaryRuntime(config: Record<string, any>): boolean {
  if (config.primary_only === false) return true
  const context = String(process.env.PI_AGENT_CONTEXT || process.env.OMP_AGENT_CONTEXT || '').toLowerCase()
  if (context && context !== 'primary' && context !== 'main') return false
  const agentId = String(process.env.OMP_AGENT_ID || '')
  return !agentId || ['main', 'primary'].includes(agentId.toLowerCase())
}

export default function (pi: any) {
  pi.on('agent_end', async (event: any, ctx: any) => {
    const config = loadConfig()
    if (!isPrimaryRuntime(config)) return
    const sessionId = String(ctx?.sessionManager?.getSessionId?.() || 'unknown-session')
    const pair = extractLatestCompletedPair(event?.messages || [])
      || extractLatestCompletedPair(branchMessages(ctx))
    if (!pair) {
      await flushSpool(config)
      return
    }
    const payload: Record<string, unknown> = {
      event_id: eventIdForPair(sessionId, pair),
      session_id: sessionId,
      turn_id: pair.turnId,
      user_message: pair.user,
      assistant_message: pair.assistant,
      ...(pair.userTimestamp ? { started_at: pair.userTimestamp } : {}),
      ...(pair.assistantTimestamp ? { completed_at: pair.assistantTimestamp } : {}),
      safe_context: { runtime: runtimeKind(), hook: 'agent_end' },
    }
    spoolEvent(config, payload)
    await flushSpool(config)
  })
}
