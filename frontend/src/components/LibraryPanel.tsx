import { useCallback, useEffect, useState } from 'react'
import { api } from '@/api/client'
import { AlertIcon, CloseIcon, FileIcon, ImageIcon, TrashIcon } from './Icons'
import type { DocumentRecord } from '@/types'

interface MemoryRecord {
  id: string
  kind: string
  content: string
  salience: number
  use_count: number
  created_at: string
}

interface LibraryPanelProps {
  open: boolean
  onClose: () => void
}

type Tab = 'documents' | 'memory'

const STATUS_TEXT: Record<string, string> = {
  pending: 'Queued',
  parsing: 'Parsing',
  chunking: 'Chunking',
  embedding: 'Embedding',
  ready: 'Ready',
  failed: 'Failed',
}

const BUSY = new Set(['pending', 'parsing', 'chunking', 'embedding'])

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/**
 * Everything the assistant knows about you, and the controls to remove it.
 * Uploads and extracted memories are otherwise invisible after the turn that
 * created them — which makes them impossible to correct or delete.
 */
export function LibraryPanel({ open, onClose }: LibraryPanelProps) {
  const [tab, setTab] = useState<Tab>('documents')
  const [documents, setDocuments] = useState<DocumentRecord[]>([])
  const [memories, setMemories] = useState<MemoryRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [docs, mems] = await Promise.all([api.listDocuments(), api.listMemories()])
      setDocuments(docs)
      setMemories(mems as MemoryRecord[])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load your library')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  // Documents mid-ingest settle within seconds; poll only while some are busy.
  useEffect(() => {
    if (!open || !documents.some((d) => BUSY.has(d.status))) return
    const handle = window.setInterval(() => void load(), 2500)
    return () => window.clearInterval(handle)
  }, [open, documents, load])

  useEffect(() => {
    if (!open) return
    const onEsc = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onEsc)
    return () => document.removeEventListener('keydown', onEsc)
  }, [open, onClose])

  if (!open) return null

  const removeDocument = async (id: string) => {
    setDocuments((prev) => prev.filter((d) => d.id !== id))
    try {
      await api.deleteDocument(id)
    } catch {
      void load() // put it back if the server disagreed
    }
  }

  const removeMemory = async (id: string) => {
    setMemories((prev) => prev.filter((m) => m.id !== id))
    try {
      await api.deleteMemory(id)
    } catch {
      void load()
    }
  }

  const clearAllMemories = async () => {
    const previous = memories
    setMemories([])
    try {
      await api.clearMemories()
    } catch {
      setMemories(previous)
    }
  }

  return (
    <>
      <div className="drawer-scrim" onClick={onClose} aria-hidden />
      <aside className="library" role="dialog" aria-label="Your library">
        <header className="library-head">
          <div className="library-tabs" role="tablist">
            <button
              role="tab"
              aria-selected={tab === 'documents'}
              className={tab === 'documents' ? 'is-active' : ''}
              onClick={() => setTab('documents')}
            >
              Documents <em>{documents.length}</em>
            </button>
            <button
              role="tab"
              aria-selected={tab === 'memory'}
              className={tab === 'memory' ? 'is-active' : ''}
              onClick={() => setTab('memory')}
            >
              Memory <em>{memories.length}</em>
            </button>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Close library">
            <CloseIcon />
          </button>
        </header>

        {error && (
          <div className="library-error" role="alert">
            <AlertIcon size={14} />
            <span>{error}</span>
          </div>
        )}

        <div className="library-body">
          {loading && documents.length === 0 && memories.length === 0 && (
            <p className="library-empty">Loading…</p>
          )}

          {tab === 'documents' &&
            (documents.length === 0 && !loading ? (
              <p className="library-empty">
                Nothing uploaded yet. Attach files with <strong>+</strong> in the prompt box
                and they become searchable here.
              </p>
            ) : (
              <ul className="library-list">
                {documents.map((d) => (
                  <li key={d.id} className={d.status === 'failed' ? 'is-failed' : ''}>
                    <span className="library-icon">
                      {d.mime_type.startsWith('image/') ? (
                        <ImageIcon size={15} />
                      ) : (
                        <FileIcon size={15} />
                      )}
                    </span>
                    <div className="library-main">
                      <strong title={d.filename}>{d.filename}</strong>
                      <span className="library-meta">
                        <span className={BUSY.has(d.status) ? 'is-busy' : ''}>
                          {STATUS_TEXT[d.status] ?? d.status}
                        </span>
                        {d.status === 'ready' && (
                          <>
                            {' · '}
                            {d.chunk_count} chunk{d.chunk_count === 1 ? '' : 's'}
                            {d.page_count > 0 && ` · ${d.page_count}p`}
                          </>
                        )}
                        {' · '}
                        {formatBytes(d.size_bytes)}
                      </span>
                      {d.error && <span className="library-detail">{d.error}</span>}
                    </div>
                    <button
                      className="icon-btn"
                      onClick={() => void removeDocument(d.id)}
                      aria-label={`Delete ${d.filename}`}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            ))}

          {tab === 'memory' &&
            (memories.length === 0 && !loading ? (
              <p className="library-empty">
                Nothing remembered yet. Durable facts and preferences are picked up as you
                chat, and applied in later conversations.
              </p>
            ) : (
              <>
                <ul className="library-list">
                  {memories.map((m) => (
                    <li key={m.id}>
                      <span className="library-kind">{m.kind}</span>
                      <div className="library-main">
                        <p>{m.content}</p>
                        <span className="library-meta">
                          used {m.use_count}×{' · '}
                          {new Date(m.created_at).toLocaleDateString()}
                        </span>
                      </div>
                      <button
                        className="icon-btn"
                        onClick={() => void removeMemory(m.id)}
                        aria-label="Forget this"
                      >
                        <TrashIcon size={14} />
                      </button>
                    </li>
                  ))}
                </ul>
                <button className="library-clear" onClick={() => void clearAllMemories()}>
                  Forget everything
                </button>
              </>
            ))}
        </div>
      </aside>
    </>
  )
}
