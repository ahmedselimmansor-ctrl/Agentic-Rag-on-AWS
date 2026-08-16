import { useEffect, useLayoutEffect, useRef } from 'react'
import { MessageBubble } from './MessageBubble'
import { SparkIcon } from './Icons'
import type { ChatMessage, Source } from '@/types'

interface MessageListProps {
  messages: ChatMessage[]
  isStreaming: boolean
  onCite?: (source: Source) => void
  onSuggestion: (text: string) => void
}

const SUGGESTIONS = [
  'Summarise the key points of the document I just uploaded.',
  'What does this contract say about termination?',
  'Compare what my documents say with current public guidance.',
]

export function MessageList({ messages, isStreaming, onCite, onSuggestion }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  // Only auto-scroll while the user is already at the bottom — yanking the view
  // away from someone reading earlier output is the classic chat-UI sin.
  const pinnedRef = useRef(true)

  useLayoutEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight
      pinnedRef.current = distance < 120
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => el.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (pinnedRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: isStreaming ? 'auto' : 'smooth' })
    }
  }, [messages, isStreaming])

  if (messages.length === 0) {
    return (
      <div className="thread" ref={scrollRef}>
        <div className="empty-state">
          <div className="empty-mark" aria-hidden>
            <SparkIcon size={22} />
          </div>
          <h1>What are we digging into?</h1>
          <p>
            Attach documents with <strong>+</strong>, or turn on <strong>Web search</strong> to
            let the agent look things up. Answers cite what they came from.
          </p>
          <ul className="suggestions">
            {SUGGESTIONS.map((s) => (
              <li key={s}>
                <button onClick={() => onSuggestion(s)}>{s}</button>
              </li>
            ))}
          </ul>
        </div>
      </div>
    )
  }

  return (
    <div className="thread" ref={scrollRef}>
      <div className="thread-inner">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} onCite={onCite} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
