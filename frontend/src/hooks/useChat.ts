import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { streamChat } from '@/api/stream'
import type { Attachment, ChatMessage, Conversation, Usage } from '@/types'

function uid(): string {
  return crypto.randomUUID()
}

export function useChat() {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [isStreaming, setIsStreaming] = useState(false)
  const [status, setStatus] = useState<string | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loadingConversation, setLoadingConversation] = useState(false)

  const abortRef = useRef<AbortController | null>(null)
  // Token deltas arrive faster than React should re-render; they are buffered
  // here and flushed on an animation frame.
  const bufferRef = useRef('')
  const frameRef = useRef<number | null>(null)
  const assistantIdRef = useRef<string | null>(null)

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await api.listConversations())
    } catch (e) {
      console.error('failed to load conversations', e)
    }
  }, [])

  useEffect(() => {
    void refreshConversations()
  }, [refreshConversations])

  const flushBuffer = useCallback(() => {
    frameRef.current = null
    const pending = bufferRef.current
    if (!pending) return
    bufferRef.current = ''
    const id = assistantIdRef.current
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, content: m.content + pending } : m)),
    )
  }, [])

  const scheduleFlush = useCallback(() => {
    if (frameRef.current !== null) return
    frameRef.current = requestAnimationFrame(flushBuffer)
  }, [flushBuffer])

  const selectConversation = useCallback(
    async (id: string | null) => {
      abortRef.current?.abort()
      setConversationId(id)
      setError(null)
      setUsage(null)
      setStatus(null)

      if (!id) {
        setMessages([])
        return
      }

      setLoadingConversation(true)
      try {
        const detail = await api.getConversation(id)
        setMessages(
          detail.messages
            .filter((m) => m.role === 'user' || m.role === 'assistant')
            .map((m) => ({
              id: m.id,
              role: m.role as 'user' | 'assistant',
              content: m.content,
              sources: m.sources ?? [],
              tool_calls: m.tool_calls ?? [],
              attachments: m.attachments ?? [],
              error: m.error,
              created_at: m.created_at,
            })),
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to load conversation')
        setMessages([])
      } finally {
        setLoadingConversation(false)
      }
    },
    [],
  )

  const newConversation = useCallback(() => {
    abortRef.current?.abort()
    setConversationId(null)
    setMessages([])
    setError(null)
    setUsage(null)
    setStatus(null)
  }, [])

  const send = useCallback(
    async (text: string, attachments: Attachment[] = [], webSearch = false) => {
      const trimmed = text.trim()
      if (!trimmed || isStreaming) return

      setError(null)
      setUsage(null)
      setIsStreaming(true)
      setStatus('Thinking')

      const assistantId = uid()
      assistantIdRef.current = assistantId
      bufferRef.current = ''

      setMessages((prev) => [
        ...prev,
        {
          id: uid(),
          role: 'user',
          content: trimmed,
          sources: [],
          tool_calls: [],
          attachments,
        },
        {
          id: assistantId,
          role: 'assistant',
          content: '',
          sources: [],
          tool_calls: [],
          attachments: [],
          streaming: true,
        },
      ])

      const controller = new AbortController()
      abortRef.current = controller

      const patch = (updater: (m: ChatMessage) => ChatMessage) =>
        setMessages((prev) => prev.map((m) => (m.id === assistantId ? updater(m) : m)))

      try {
        await streamChat(
          {
            message: trimmed,
            conversationId,
            attachments,
            webSearch,
            signal: controller.signal,
          },
          {
            onStart: ({ conversation_id }) => {
              if (!conversationId) setConversationId(conversation_id)
            },
            onStatus: (label) => setStatus(label),
            onToken: (chunk) => {
              bufferRef.current += chunk
              scheduleFlush()
            },
            onSources: (sources) => patch((m) => ({ ...m, sources })),
            onToolCall: (name) => setStatus(`Running ${name.replace(/_/g, ' ')}`),
            onToolResult: (entry) =>
              patch((m) => ({ ...m, tool_calls: [...m.tool_calls, entry] })),
            onUsage: (u) => setUsage(u),
            onDone: ({ sources, tool_trace, title }) => {
              flushBuffer()
              patch((m) => ({
                ...m,
                streaming: false,
                sources: sources.length ? sources : m.sources,
                tool_calls: tool_trace.length ? tool_trace : m.tool_calls,
              }))
              if (title) void refreshConversations()
            },
            onError: (message) => {
              flushBuffer()
              setError(message)
              patch((m) => ({ ...m, streaming: false, error: message }))
            },
          },
        )
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          const message = e instanceof Error ? e.message : 'Stream failed'
          setError(message)
          patch((m) => ({ ...m, streaming: false, error: message }))
        }
      } finally {
        flushBuffer()
        setIsStreaming(false)
        setStatus(null)
        abortRef.current = null
        // A brand-new thread only appears in the sidebar once its title lands.
        if (!conversationId) void refreshConversations()
      }
    },
    [conversationId, isStreaming, scheduleFlush, flushBuffer, refreshConversations],
  )

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    flushBuffer()
    setIsStreaming(false)
    setStatus(null)
    setMessages((prev) =>
      prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
    )
  }, [flushBuffer])

  const deleteConversation = useCallback(
    async (id: string) => {
      await api.deleteConversation(id)
      if (id === conversationId) newConversation()
      await refreshConversations()
    },
    [conversationId, newConversation, refreshConversations],
  )

  const renameConversation = useCallback(
    async (id: string, title: string) => {
      await api.renameConversation(id, title)
      await refreshConversations()
    },
    [refreshConversations],
  )

  useEffect(() => () => abortRef.current?.abort(), [])

  return {
    conversations,
    conversationId,
    messages,
    isStreaming,
    status,
    usage,
    error,
    loadingConversation,
    send,
    stop,
    newConversation,
    selectConversation,
    deleteConversation,
    renameConversation,
    refreshConversations,
  }
}
