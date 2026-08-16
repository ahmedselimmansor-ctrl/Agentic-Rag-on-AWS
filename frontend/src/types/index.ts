export type Role = 'user' | 'assistant'

export interface Source {
  index: number
  chunk_id?: string
  document_id?: string
  filename?: string
  label: string
  snippet: string
  url?: string
  page_from?: number | null
  page_to?: number | null
  modality?: string
  kind?: 'web' | 'document'
  score?: number | null
}

export interface ToolTraceEntry {
  tool: string
  arguments?: Record<string, unknown>
  ok: boolean
  error?: string | null
  duration_ms?: number
  result_count?: number
}

export interface Attachment {
  document_id?: string
  filename: string
  mime_type: string
  url?: string | null
  size_bytes: number
  /** Client-only: ingestion progress for the chip in the prompt box. */
  status?: DocumentStatus
  error?: string | null
  localId?: string
  previewUrl?: string
}

export interface ChatMessage {
  id: string
  role: Role
  content: string
  sources: Source[]
  tool_calls: ToolTraceEntry[]
  attachments: Attachment[]
  error?: string | null
  created_at?: string
  /** Client-only: true while tokens are still arriving. */
  streaming?: boolean
  status?: string
}

export interface Conversation {
  id: string
  title: string
  archived: boolean
  created_at: string
  updated_at: string
  last_message_at: string | null
}

export interface ConversationDetail extends Conversation {
  summary: string | null
  messages: Array<{
    id: string
    ordinal: number
    role: Role | 'system' | 'tool'
    content: string
    sources: Source[]
    tool_calls: ToolTraceEntry[]
    attachments: Attachment[]
    error: string | null
    created_at: string
  }>
}

export type DocumentStatus =
  | 'pending'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'ready'
  | 'failed'

export interface DocumentRecord {
  id: string
  filename: string
  mime_type: string
  size_bytes: number
  status: DocumentStatus
  error: string | null
  page_count: number
  chunk_count: number
  conversation_id: string | null
  created_at: string
}

export interface Usage {
  prompt_tokens: number
  completion_tokens: number
  latency_ms: number
  steps: number
}
