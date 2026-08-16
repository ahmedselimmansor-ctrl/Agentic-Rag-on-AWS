import { useCallback, useEffect, useState } from 'react'
import { Sidebar } from './components/Sidebar'
import { MessageList } from './components/MessageList'
import { PromptBox } from './components/PromptBox'
import { SourceDrawer } from './components/SourceDrawer'
import { AlertIcon, MenuIcon, PlusIcon } from './components/Icons'
import { useChat } from './hooks/useChat'
import { useUploads } from './hooks/useUploads'
import type { Source } from './types'

export default function App() {
  const chat = useChat()
  const uploads = useUploads(chat.conversationId)

  const [draft, setDraft] = useState('')
  const [webSearch, setWebSearch] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 900)
  const [activeSource, setActiveSource] = useState<Source | null>(null)

  const handleSend = useCallback(
    (text: string) => {
      // Only attachments that finished ingesting can be retrieved from.
      const ready = uploads.attachments.filter((a) => a.status === 'ready')
      void chat.send(text, ready, webSearch)
      uploads.clear()
      setDraft('')
    },
    [chat, uploads, webSearch],
  )

  const handleNew = useCallback(() => {
    chat.newConversation()
    uploads.clear()
    setDraft('')
    if (window.innerWidth < 900) setSidebarOpen(false)
  }, [chat, uploads])

  const handleSelect = useCallback(
    (id: string) => {
      void chat.selectConversation(id)
      uploads.clear()
      if (window.innerWidth < 900) setSidebarOpen(false)
    },
    [chat, uploads],
  )

  // Cmd/Ctrl+K starts a new chat from anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        handleNew()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [handleNew])

  const activeTitle =
    chat.conversations.find((c) => c.id === chat.conversationId)?.title ?? 'New chat'

  return (
    <div className={`app${sidebarOpen ? ' sidebar-open' : ''}`}>
      <Sidebar
        conversations={chat.conversations}
        activeId={chat.conversationId}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onNew={handleNew}
        onSelect={handleSelect}
        onDelete={(id) => void chat.deleteConversation(id)}
        onRename={(id, title) => void chat.renameConversation(id, title)}
      />

      <main className="main">
        <header className="topbar">
          <button
            className="icon-btn"
            onClick={() => setSidebarOpen((v) => !v)}
            aria-label="Toggle sidebar"
          >
            <MenuIcon />
          </button>
          <h1 className="topbar-title">{activeTitle}</h1>
          <button className="icon-btn" onClick={handleNew} aria-label="New chat" title="New chat (⌘K)">
            <PlusIcon size={17} />
          </button>
        </header>

        {chat.error && (
          <div className="banner" role="alert">
            <AlertIcon size={15} />
            <span>{chat.error}</span>
          </div>
        )}

        {chat.loadingConversation ? (
          <div className="thread">
            <div className="loading">Loading conversation…</div>
          </div>
        ) : (
          <MessageList
            messages={chat.messages}
            isStreaming={chat.isStreaming}
            onCite={setActiveSource}
            onSuggestion={setDraft}
          />
        )}

        <PromptBox
          value={draft}
          onChange={setDraft}
          attachments={uploads.attachments}
          uploading={uploads.uploading}
          uploadError={uploads.uploadError}
          isStreaming={chat.isStreaming}
          status={chat.status}
          webSearch={webSearch}
          onToggleWebSearch={() => setWebSearch((v) => !v)}
          onAddFiles={(files) => void uploads.addFiles(files)}
          onRemoveAttachment={uploads.remove}
          onSend={handleSend}
          onStop={chat.stop}
        />

        {chat.usage && !chat.isStreaming && (
          <p className="usage">
            {chat.usage.prompt_tokens.toLocaleString()} in ·{' '}
            {chat.usage.completion_tokens.toLocaleString()} out ·{' '}
            {(chat.usage.latency_ms / 1000).toFixed(1)}s
            {chat.usage.steps > 1 && ` · ${chat.usage.steps} steps`}
          </p>
        )}
      </main>

      <SourceDrawer source={activeSource} onClose={() => setActiveSource(null)} />
    </div>
  )
}
