import type {
  Conversation,
  ConversationDetail,
  DocumentRecord,
} from '@/types'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** Placeholder identity. Swap for a real bearer token behind Cognito/OIDC. */
function identityHeaders(): Record<string, string> {
  const email = localStorage.getItem('userEmail') ?? 'local@example.com'
  return { 'X-User-Email': email }
}

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...identityHeaders(),
      ...(init.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...init.headers,
    },
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = body.detail ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, response.status)
  }

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
}

export { ApiError, BASE as API_BASE, identityHeaders }
