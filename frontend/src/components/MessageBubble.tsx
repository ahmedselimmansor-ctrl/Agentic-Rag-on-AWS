import { useState } from 'react'
import { Markdown } from './Markdown'
import { AlertIcon, FileIcon, GlobeIcon, ImageIcon, SearchIcon } from './Icons'
import type { ChatMessage, Source } from '@/types'

interface MessageBubbleProps {
  message: ChatMessage
  onCite?: (source: Source) => void
}

const TOOL_LABEL: Record<string, string> = {
  search_documents: 'Searched your documents',
  web_search: 'Searched the web',
  fetch_page: 'Read a web page',
  retrieve: 'Retrieved passages',
}

export function MessageBubble({ message, onCite }: MessageBubbleProps) {
  const [showSources, setShowSources] = useState(false)
  const isUser = message.role === 'user'

  if (isUser) {
    return (
      <article className="msg msg-user">
        {message.attachments.length > 0 && (
          <ul className="msg-attachments">
            {message.attachments.map((a, i) => (
              <li key={a.document_id ?? i}>
                {a.mime_type.startsWith('image/') ? <ImageIcon size={13} /> : <FileIcon size={13} />}
                <span>{a.filename}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="msg-user-body">{message.content}</div>
      </article>
    )
  }

  const hasContent = message.content.length > 0
  const documentSources = message.sources.filter((s) => s.kind !== 'web')
  const webSources = message.sources.filter((s) => s.kind === 'web')

  return (
    <article className="msg msg-assistant">
      {message.tool_calls.length > 0 && (
        <ul className="tool-trace">
          {message.tool_calls.map((t, i) => (
            <li key={i} className={t.ok ? '' : 'is-error'}>
              {t.tool === 'web_search' || t.tool === 'fetch_page' ? (
                <GlobeIcon size={13} />
              ) : (
                <SearchIcon size={13} />
              )}
              <span>{TOOL_LABEL[t.tool] ?? t.tool}</span>
              {t.result_count !== undefined && t.ok && (
                <em>
                  {t.result_count} result{t.result_count === 1 ? '' : 's'}
                </em>
              )}
              {!t.ok && <em>failed</em>}
            </li>
          ))}
        </ul>
      )}

      {hasContent ? (
        <Markdown content={message.content} sources={message.sources} onCite={onCite} />
      ) : message.streaming ? (
        <div className="typing" aria-label="Generating">
          <span />
          <span />
          <span />
        </div>
      ) : null}

      {message.streaming && hasContent && <span className="cursor" aria-hidden />}

      {message.error && (
        <div className="msg-error" role="alert">
          <AlertIcon size={14} />
          <span>{message.error}</span>
        </div>
      )}

      {!message.streaming && message.sources.length > 0 && (
        <div className="sources">
          <button className="sources-toggle" onClick={() => setShowSources((v) => !v)}>
            {showSources ? 'Hide' : 'Show'} {message.sources.length} source
            {message.sources.length === 1 ? '' : 's'}
            {webSources.length > 0 && documentSources.length > 0 && (
              <em>
                {' '}
                · {documentSources.length} document, {webSources.length} web
              </em>
            )}
          </button>

          {showSources && (
            <ol className="sources-list">
              {message.sources.map((s) => (
                <li key={`${s.index}-${s.chunk_id ?? s.url}`}>
                  <span className="source-index">{s.index}</span>
                  <div className="source-body">
                    {s.url ? (
                      <a href={s.url} target="_blank" rel="noopener noreferrer">
                        {s.label}
                      </a>
                    ) : (
                      <strong>{s.label}</strong>
                    )}
                    {s.score !== null && s.score !== undefined && (
                      <span className="source-score">{s.score.toFixed(2)}</span>
                    )}
                    <p>{s.snippet}</p>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </article>
  )
}
