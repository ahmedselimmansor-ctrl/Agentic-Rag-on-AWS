import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { Attachment } from '@/types'

const TERMINAL = new Set(['ready', 'failed'])
const POLL_INTERVAL_MS = 1500

/**
 * Uploads attach immediately as a chip, then poll until ingestion reaches a
 * terminal state. A chip that is still embedding is shown as pending rather
 * than blocking the composer — the user can keep typing.
 */
export function useUploads(conversationId: string | null) {
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const timers = useRef(new Map<string, number>())

  const stopPolling = useCallback((documentId: string) => {
    const handle = timers.current.get(documentId)
    if (handle !== undefined) {
      window.clearTimeout(handle)
      timers.current.delete(documentId)
    }
  }, [])

  const poll = useCallback(
    (documentId: string) => {
      const tick = async () => {
        try {
          const doc = await api.getDocument(documentId)
          setAttachments((prev) =>
            prev.map((a) =>
              a.document_id === documentId
                ? { ...a, status: doc.status, error: doc.error }
                : a,
            ),
          )
          if (TERMINAL.has(doc.status)) {
            stopPolling(documentId)
            // Images go to the vision model by URL, so resolve it once ready.
            if (doc.status === 'ready' && doc.mime_type.startsWith('image/')) {
              try {
                const { url } = await api.getDocumentUrl(documentId)
                setAttachments((prev) =>
                  prev.map((a) => (a.document_id === documentId ? { ...a, url } : a)),
                )
              } catch {
                /* the answer still works without the inline image */
              }
            }
            return
          }
          timers.current.set(documentId, window.setTimeout(tick, POLL_INTERVAL_MS))
        } catch {
          stopPolling(documentId)
        }
      }
      timers.current.set(documentId, window.setTimeout(tick, POLL_INTERVAL_MS))
    },
    [stopPolling],
  )

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files)
      if (!list.length) return

      setUploading(true)
      setUploadError(null)

      // Optimistic chips so the UI reacts before the network does.
      const optimistic: Attachment[] = list.map((file) => ({
        localId: crypto.randomUUID(),
        filename: file.name,
        mime_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
        status: 'pending',
        previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
      }))
      setAttachments((prev) => [...prev, ...optimistic])

      await Promise.all(
        list.map(async (file, i) => {
          const localId = optimistic[i].localId!
          try {
            const doc = await api.uploadDocument(file, conversationId ?? undefined)
            setAttachments((prev) =>
              prev.map((a) =>
                a.localId === localId
                  ? { ...a, document_id: doc.id, status: doc.status, error: doc.error }
                  : a,
              ),
            )
            if (!TERMINAL.has(doc.status)) poll(doc.id)
          } catch (e) {
            const message = e instanceof Error ? e.message : 'Upload failed'
            setUploadError(message)
            setAttachments((prev) =>
              prev.map((a) =>
                a.localId === localId ? { ...a, status: 'failed', error: message } : a,
              ),
            )
          }
        }),
      )

      setUploading(false)
    },
    [conversationId, poll],
  )

  const remove = useCallback(
    (attachment: Attachment) => {
      if (attachment.document_id) stopPolling(attachment.document_id)
      if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl)
      setAttachments((prev) =>
        prev.filter((a) =>
          attachment.localId
            ? a.localId !== attachment.localId
            : a.document_id !== attachment.document_id,
        ),
      )
    },
    [stopPolling],
  )

  const clear = useCallback(() => {
    timers.current.forEach((handle) => window.clearTimeout(handle))
    timers.current.clear()
    setAttachments((prev) => {
      prev.forEach((a) => a.previewUrl && URL.revokeObjectURL(a.previewUrl))
      return []
    })
    setUploadError(null)
  }, [])

  useEffect(
    () => () => {
      timers.current.forEach((handle) => window.clearTimeout(handle))
      timers.current.clear()
    },
    [],
  )

  return { attachments, uploading, uploadError, addFiles, remove, clear }
}
