import { useMemo, useState } from 'react'
import { ChatIcon, CloseIcon, PlusIcon, SearchIcon, TrashIcon } from './Icons'
import type { Conversation } from '@/types'

interface SidebarProps {
  conversations: Conversation[]
  activeId: string | null
  open: boolean
  onClose: () => void
  onNew: () => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onRename: (id: string, title: string) => void
  userEmail: string
  userName: string | null
  onSignOut: () => void
}

/** Groups threads the way people actually remember them: by recency band. */
function groupByRecency(conversations: Conversation[]) {
  const now = Date.now()
  const day = 86_400_000
  const buckets: Record<string, Conversation[]> = {
    Today: [],
    Yesterday: [],
    'Previous 7 days': [],
    'Previous 30 days': [],
    Older: [],
  }

  for (const c of conversations) {
    const stamp = new Date(c.last_message_at ?? c.created_at).getTime()
    const age = now - stamp
    if (age < day) buckets.Today.push(c)
    else if (age < 2 * day) buckets.Yesterday.push(c)
    else if (age < 7 * day) buckets['Previous 7 days'].push(c)
    else if (age < 30 * day) buckets['Previous 30 days'].push(c)
    else buckets.Older.push(c)
  }

  return Object.entries(buckets).filter(([, items]) => items.length > 0)
}

export function Sidebar({
  conversations,
  activeId,
  open,
  onClose,
  onNew,
  onSelect,
  onDelete,
  onRename,
  userEmail,
  userName,
  onSignOut,
}: SidebarProps) {
  const [filter, setFilter] = useState('')
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftTitle, setDraftTitle] = useState('')

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    const filtered = needle
      ? conversations.filter((c) => c.title.toLowerCase().includes(needle))
      : conversations
    return groupByRecency(filtered)
  }, [conversations, filter])

  const commitRename = (id: string) => {
    const title = draftTitle.trim()
    if (title) onRename(id, title)
    setEditingId(null)
    setDraftTitle('')
  }

  return (
    <>
      {open && <div className="sidebar-scrim" onClick={onClose} aria-hidden />}
      <aside className={`sidebar${open ? ' is-open' : ''}`} aria-label="Conversations">
        <div className="sidebar-head">
          <button className="btn-new" onClick={onNew}>
            <PlusIcon size={16} />
            <span>New chat</span>
          </button>
          <button className="icon-btn sidebar-close" onClick={onClose} aria-label="Close sidebar">
            <CloseIcon />
          </button>
        </div>

        <div className="sidebar-search">
          <SearchIcon size={14} />
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Search chats"
            aria-label="Search conversations"
          />
        </div>

        <nav className="sidebar-list">
          {groups.length === 0 && (
            <p className="sidebar-empty">
              {filter ? 'No chats match that search.' : 'No conversations yet.'}
            </p>
          )}

          {groups.map(([label, items]) => (
            <section key={label} className="sidebar-group">
              <h2 className="sidebar-group-label">{label}</h2>
              <ul>
                {items.map((c) => (
                  <li key={c.id}>
                    <div
                      className={`chat-row${c.id === activeId ? ' is-active' : ''}`}
                      onClick={() => editingId !== c.id && onSelect(c.id)}
                      onDoubleClick={() => {
                        setEditingId(c.id)
                        setDraftTitle(c.title)
                      }}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' && editingId !== c.id) onSelect(c.id)
                      }}
                    >
                      <ChatIcon size={14} className="chat-row-icon" />
                      {editingId === c.id ? (
                        <input
                          className="chat-rename"
                          value={draftTitle}
                          autoFocus
                          onChange={(e) => setDraftTitle(e.target.value)}
                          onBlur={() => commitRename(c.id)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') commitRename(c.id)
                            if (e.key === 'Escape') setEditingId(null)
                          }}
                          onClick={(e) => e.stopPropagation()}
                        />
                      ) : (
                        <span className="chat-row-title" title={c.title}>
                          {c.title}
                        </span>
                      )}
                      <button
                        className="icon-btn chat-row-delete"
                        aria-label={`Delete ${c.title}`}
                        onClick={(e) => {
                          e.stopPropagation()
                          onDelete(c.id)
                        }}
                      >
                        <TrashIcon size={14} />
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </nav>

        <footer className="sidebar-foot">
          <span className="avatar" aria-hidden>
            {(userName || userEmail || '?').charAt(0).toUpperCase()}
          </span>
          <span className="sidebar-user" title={userEmail}>
            {userName || userEmail}
          </span>
          <button className="sign-out" onClick={onSignOut}>
            Sign out
          </button>
        </footer>
      </aside>
    </>
  )
}
