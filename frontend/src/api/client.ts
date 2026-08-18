import type {
  Conversation,
  ConversationDetail,
  DocumentRecord,
} from '@/types'
import { AuthRequiredError, authHeaders, refreshAccessToken } from './auth'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function toError(response: Response): Promise<ApiError> {
  let detail = response.statusText
  try {
    detail = (await response.json()).detail ?? detail
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(detail, response.status)
}

/** Issues the request, refreshing the access token once on a 401. */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const send = () =>
    fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        ...authHeaders(),
        // FormData must set its own multipart boundary.
        ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        ...init.headers,
      },
    })

  let response = await send()

  if (response.status === 401) {
    await refreshAccessToken() // throws AuthRequiredError when the session is over
    response = await send()
  }

  if (!response.ok) throw await toError(response)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  listConversations: (archived = false) =>
    request<Conversation[]>(`/conversations?archived=${archived}`),

  createConversation: (title = 'New chat') =>
    request<Conversation>('/conversations', {
      method: 'POST',
      body: JSON.stringify({ title }),
    }),

  getConversation: (id: string) => request<ConversationDetail>(`/conversations/${id}`),

  renameConversation: (id: string, title: string) =>
    request<Conversation>(`/conversations/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    }),

  deleteConversation: (id: string) =>
    request<void>(`/conversations/${id}`, { method: 'DELETE' }),

  listDocuments: (conversationId?: string) =>
    request<DocumentRecord[]>(
      `/documents${conversationId ? `?conversation_id=${conversationId}` : ''}`,
    ),

  uploadDocument: (file: File, conversationId?: string) => {
    const form = new FormData()
    form.append('file', file)
    if (conversationId) form.append('conversation_id', conversationId)
    return request<DocumentRecord>('/documents', { method: 'POST', body: form })
  },

  getDocument: (id: string) => request<DocumentRecord>(`/documents/${id}`),

  getDocumentUrl: (id: string) =>
    request<{ url: string; mime_type: string }>(`/documents/${id}/url`),

  deleteDocument: (id: string) => request<void>(`/documents/${id}`, { method: 'DELETE' }),

  listMemories: () =>
    request<
      Array<{
        id: string
        kind: string
        content: string
        salience: number
        use_count: number
        created_at: string
      }>
    >('/memories'),

  deleteMemory: (id: string) => request<void>(`/memories/${id}`, { method: 'DELETE' }),

  clearMemories: () => request<void>('/memories', { method: 'DELETE' }),
}

export { ApiError, AuthRequiredError, BASE as API_BASE }
