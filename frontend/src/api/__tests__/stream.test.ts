import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamChat } from '../stream'
import type { StreamHandlers } from '../stream'

/** Builds a Response whose body streams `parts` in the given slices. */
function sseResponse(parts: string[], status = 200): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const part of parts) controller.enqueue(encoder.encode(part))
      controller.close()
    },
  })
  return new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } })
}

function collect() {
  const tokens: string[] = []
  const events: string[] = []
  const handlers: StreamHandlers = {
    onStart: () => events.push('start'),
    onStatus: () => events.push('status'),
    onToken: (t) => {
      tokens.push(t)
      events.push('token')
    },
    onSources: () => events.push('sources'),
    onToolCall: () => events.push('tool_call'),
    onToolResult: () => events.push('tool_result'),
    onUsage: () => events.push('usage'),
    onDone: () => events.push('done'),
    onError: () => events.push('error'),
  }
  return { tokens, events, handlers }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamChat SSE parsing', () => {
  it('dispatches a well-formed sequence in order', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([
          'event: start\ndata: {"conversation_id":"c","assistant_message_id":"a","user_message_id":"u"}\n\n',
          'event: status\ndata: {"label":"Searching"}\n\n',
          'event: token\ndata: {"text":"Hello"}\n\n',
          'event: token\ndata: {"text":" world"}\n\n',
          'event: done\ndata: {"message_id":"a","sources":[],"tool_trace":[],"title":null,"usage":{}}\n\n',
        ]),
      ),
    )

    const { tokens, events, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(events).toEqual(['start', 'status', 'token', 'token', 'done'])
    expect(tokens.join('')).toBe('Hello world')
  })

  it('reassembles a frame split across network chunks', async () => {
    // The decisive case: a frame boundary that lands mid-JSON.
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse(['event: token\nda', 'ta: {"text":"par', 'tial"}\n\nevent: token\ndata: {"text":"!"}\n\n']),
      ),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['partial', '!'])
  })

  it('delivers several frames arriving in one chunk', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([
          'event: token\ndata: {"text":"a"}\n\nevent: token\ndata: {"text":"b"}\n\nevent: token\ndata: {"text":"c"}\n\n',
        ]),
      ),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['a', 'b', 'c'])
  })

  it('ignores heartbeat comment frames', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([': keepalive\n\n', 'event: token\ndata: {"text":"x"}\n\n', ': keepalive\n\n']),
      ),
    )

    const { tokens, events, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['x'])
    expect(events).toEqual(['token'])
  })

  it('preserves newlines inside token text', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse(['event: token\ndata: {"text":"line1\\nline2"}\n\n'])),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['line1\nline2'])
  })

  it('tolerates CRLF line endings from a proxy', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse(['event: token\r\ndata: {"text":"crlf"}\n\n'])),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['crlf'])
  })

  it('skips an unparseable frame without aborting the stream', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([
          'event: token\ndata: {not json}\n\n',
          'event: token\ndata: {"text":"survived"}\n\n',
        ]),
      ),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['survived'])
  })

  it('reports a rate limit as an error rather than an empty stream', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: 'Message limit reached (120/hour).' }), {
          status: 429,
        }),
      ),
    )

    const messages: string[] = []
    await streamChat({ message: 'hi' }, { onError: (m) => messages.push(m) })

    expect(messages).toEqual(['Message limit reached (120/hour).'])
  })

  it('surfaces a server error frame', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse(['event: error\ndata: {"message":"boom"}\n\n'])),
    )

    const messages: string[] = []
    await streamChat({ message: 'hi' }, { onError: (m) => messages.push(m) })

    expect(messages).toEqual(['boom'])
  })

  it('flushes a trailing frame with no terminating blank line', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse(['event: token\ndata: {"text":"tail"}'])),
    )

    const { tokens, handlers } = collect()
    await streamChat({ message: 'hi' }, handlers)

    expect(tokens).toEqual(['tail'])
  })
})
