import { useEffect, useRef, useState } from 'react'
import {
  AlertIcon,
  CloseIcon,
  FileIcon,
  GlobeIcon,
  ImageIcon,
  PlusIcon,
  SendIcon,
  StopIcon,
} from './Icons'
import type { Attachment } from '@/types'

interface PromptBoxProps {
  /** Controlled so suggestion chips and retries can seed the composer. */
  value: string
  onChange: (value: string) => void
  attachments: Attachment[]
  uploading: boolean
  uploadError: string | null
  isStreaming: boolean
  status: string | null
  webSearch: boolean
  onToggleWebSearch: () => void
  onAddFiles: (files: FileList | File[]) => void
  onRemoveAttachment: (attachment: Attachment) => void
  onSend: (text: string) => void
  onStop: () => void
}

const DOC_ACCEPT = '.pdf,.docx,.txt,.md,.csv,.json,.html,.log,.yaml,.yml'
const IMAGE_ACCEPT = 'image/*'

const STATUS_LABEL: Record<string, string> = {
  pending: 'queued',
  parsing: 'parsing',
  chunking: 'chunking',
  embedding: 'embedding',
  ready: 'ready',
  failed: 'failed',
}

export function PromptBox({
  value: text,
  onChange: setText,
  attachments,
  uploading,
  uploadError,
  isStreaming,
  status,
  webSearch,
  onToggleWebSearch,
  onAddFiles,
  onRemoveAttachment,
  onSend,
  onStop,
}: PromptBoxProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [dragging, setDragging] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const docInputRef = useRef<HTMLInputElement>(null)
  const imageInputRef = useRef<HTMLInputElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  // Grow with content up to a ceiling, then scroll internally.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`
  }, [text])

  useEffect(() => {
    if (!menuOpen) return
    const onDocClick = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false)
    }
    const onEsc = (e: KeyboardEvent) => e.key === 'Escape' && setMenuOpen(false)
    document.addEventListener('mousedown', onDocClick)
    document.addEventListener('keydown', onEsc)
    return () => {
      document.removeEventListener('mousedown', onDocClick)
      document.removeEventListener('keydown', onEsc)
    }
  }, [menuOpen])

  const submit = () => {
    const value = text.trim()
    if (!value || isStreaming) return
    onSend(value)
    setText('')
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter is a newline. IME composition must not submit.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      submit()
    }
  }

  const handlePaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files)
    if (files.length) {
      e.preventDefault()
      onAddFiles(files)
    }
  }

  const pickFiles = (ref: React.RefObject<HTMLInputElement>) => {
    setMenuOpen(false)
    ref.current?.click()
  }

  const canSend = text.trim().length > 0 && !isStreaming

  return (
    <div
      className={`composer-wrap${dragging ? ' is-dragging' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDragging(false)
      }}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        if (e.dataTransfer.files.length) onAddFiles(e.dataTransfer.files)
      }}
    >
      {status && (
        <div className="composer-status" role="status">
          <span className="pulse" aria-hidden />
          {status}
        </div>
      )}

      {uploadError && (
        <div className="composer-error" role="alert">
          <AlertIcon size={14} /> {uploadError}
        </div>
      )}

      <div className="composer">
        {attachments.length > 0 && (
          <ul className="chips">
            {attachments.map((a) => {
              const key = a.localId ?? a.document_id ?? a.filename
              const isImage = a.mime_type.startsWith('image/')
              const busy = a.status && !['ready', 'failed'].includes(a.status)
              return (
                <li
                  key={key}
                  className={`chip${a.status === 'failed' ? ' is-failed' : ''}${busy ? ' is-busy' : ''}`}
                  title={a.error ?? a.filename}
                >
                  {a.previewUrl && isImage ? (
                    <img src={a.previewUrl} alt="" className="chip-thumb" />
                  ) : isImage ? (
                    <ImageIcon size={14} />
                  ) : (
                    <FileIcon size={14} />
                  )}
                  <span className="chip-name">{a.filename}</span>
                  {a.status && a.status !== 'ready' && (
                    <span className="chip-status">{STATUS_LABEL[a.status] ?? a.status}</span>
                  )}
                  <button
                    className="chip-remove"
                    onClick={() => onRemoveAttachment(a)}
                    aria-label={`Remove ${a.filename}`}
                  >
                    <CloseIcon size={12} />
                  </button>
                </li>
              )
            })}
          </ul>
        )}

        <textarea
          ref={textareaRef}
          className="composer-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder="Ask about your documents…"
          rows={1}
          aria-label="Message"
        />

        <div className="composer-actions">
          <div className="composer-left">
            <div className="menu-anchor" ref={menuRef}>
              <button
                className={`icon-btn plus-btn${menuOpen ? ' is-open' : ''}`}
                onClick={() => setMenuOpen((v) => !v)}
                aria-label="Add attachment"
                aria-expanded={menuOpen}
                aria-haspopup="menu"
                disabled={uploading}
              >
                <PlusIcon size={18} />
              </button>

              {menuOpen && (
                <div className="menu" role="menu">
                  <button role="menuitem" onClick={() => pickFiles(docInputRef)}>
                    <FileIcon size={15} />
                    <span>
                      Add file
                      <small>PDF, DOCX, TXT, MD, CSV</small>
                    </span>
                  </button>
                  <button role="menuitem" onClick={() => pickFiles(imageInputRef)}>
                    <ImageIcon size={15} />
                    <span>
                      Add image
                      <small>PNG, JPG, WEBP</small>
                    </span>
                  </button>
                </div>
              )}
            </div>

            <button
              className={`toggle-btn${webSearch ? ' is-on' : ''}`}
              onClick={onToggleWebSearch}
              aria-pressed={webSearch}
              title="Let the agent search the web when the documents fall short"
            >
              <GlobeIcon size={15} />
              <span>Web search</span>
            </button>
          </div>

          {isStreaming ? (
            <button className="send-btn is-stop" onClick={onStop} aria-label="Stop generating">
              <StopIcon size={15} />
            </button>
          ) : (
            <button
              className="send-btn"
              onClick={submit}
              disabled={!canSend}
              aria-label="Send message"
            >
              <SendIcon size={16} />
            </button>
          )}
        </div>
      </div>

      <p className="composer-hint">
        Enter to send · Shift+Enter for a new line · drop or paste files to attach
      </p>

      <input
        ref={docInputRef}
        type="file"
        accept={DOC_ACCEPT}
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) onAddFiles(e.target.files)
          e.target.value = ''
        }}
      />
      <input
        ref={imageInputRef}
        type="file"
        accept={IMAGE_ACCEPT}
        multiple
        hidden
        onChange={(e) => {
          if (e.target.files?.length) onAddFiles(e.target.files)
          e.target.value = ''
        }}
      />
    </div>
  )
}
