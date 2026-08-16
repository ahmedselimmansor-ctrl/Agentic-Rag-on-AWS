import { API_BASE, identityHeaders } from './client'
import type { Attachment, Source, ToolTraceEntry, Usage } from '@/types'

/**
 * SSE over POST. EventSource is GET-only, so we read the response body as a
 * stream and parse the wire format ourselves. Frames are separated by a blank
 * line; a frame may carry multiple `data:` lines which concatenate with \n.
 */

export interface StreamHandlers {
  onStart?: (ids: { conversation_id: string; assistant_message_id: string; user_message_id: string }) => void
  onStatus?: (label: string) => void
  onToken?: (text: string) => void
  onSources?: (sources: Source[]) => void
  onToolCall?: (name: string, args: Record<string, unknown>) => void
  onToolResult?: (entry: ToolTraceEntry) => void
  onUsage?: (usage: Usage) => void
  onDone?: (payload: {
    message_id: string
    sources: Source[]
    tool_trace: ToolTraceEntry[]
    title: string | null
    usage: Usage
  }) => void
  onError?: (message: string) => void
}

export interface ChatStreamRequest {
  message: string
  conversationId?: string | null
  attachments?: Attachment[]
  webSearch?: boolean
  signal?: AbortSignal
}

export async function streamChat(
  { message, conversationId, attachments = [], webSearch = false, signal }: ChatStreamRequest,
  handlers: StreamHandlers,
): Promise<void> {
  const response = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { ...identityHeaders(), 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({
      message,
      conversation_id: conversationId ?? null,
      web_search: webSearch,
      attachments: attachments.map((a) => ({
        document_id: a.document_id,
        filename: a.filename,
        mime_type: a.mime_type,
        url: a.url ?? null,
        size_bytes: a.size_bytes,
      })),
    }),
    signal,
  })

  if (!response.ok || !response.body) {
    let detail = `Request failed (${response.status})`
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      /* keep the default */
    }
    handlers.onError?.(detail)
    return
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // Frames end with a blank line. Anything after the last one is a partial
      // frame and must stay in the buffer until the next read.
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        dispatchFrame(frame, handlers)
        boundary = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) dispatchFrame(buffer, handlers)
  } finally {
    reader.releaseLock()
  }
}

function dispatchFrame(frame: string, handlers: StreamHandlers): void {
  let event = 'message'
  const dataLines: string[] = []

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line || line.startsWith(':')) continue // comment / heartbeat
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }

  if (!dataLines.length) return

  let payload: any
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }

  switch (event) {
    case 'start':
      handlers.onStart?.(payload)
      break
    case 'status':
      handlers.onStatus?.(payload.label)
      break
    case 'token':
      handlers.onToken?.(payload.text)
      break
    case 'sources':
      handlers.onSources?.(payload.sources ?? [])
      break
    case 'tool_call':
      handlers.onToolCall?.(payload.name, payload.arguments ?? {})
      break
    case 'tool_result':
      handlers.onToolResult?.(payload)
      break
    case 'usage':
      handlers.onUsage?.(payload)
      break
    case 'done':
      handlers.onDone?.(payload)
      break
    case 'error':
      handlers.onError?.(payload.message ?? 'Unknown error')
      break
  }
}
