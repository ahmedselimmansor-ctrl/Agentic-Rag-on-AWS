import { useCallback, useEffect, useState } from 'react'
import * as auth from '@/api/auth'
import type { AuthUser } from '@/api/auth'

type Status = 'checking' | 'authenticated' | 'anonymous'

export function useAuth() {
  const [user, setUser] = useState<AuthUser | null>(() => auth.tokens.user())
  const [status, setStatus] = useState<Status>(() =>
    auth.isAuthenticated() ? 'checking' : 'anonymous',
  )
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // A stored token may be expired or revoked. Verify against the server before
  // showing the app, so a dead session surfaces as the sign-in screen rather
  // than a wall of failed requests.
  useEffect(() => {
    if (status !== 'checking') return
    let cancelled = false

    void (async () => {
      try {
        await auth.refreshAccessToken()
        if (!cancelled) {
          setUser(auth.tokens.user())
          setStatus('authenticated')
        }
      } catch {
        if (!cancelled) {
          auth.tokens.clear()
          setUser(null)
          setStatus('anonymous')
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [status])

  // Any request can fail with AuthRequiredError once the refresh token dies.
  useEffect(() => {
    const onUnauthorized = () => {
      auth.tokens.clear()
      setUser(null)
      setStatus('anonymous')
      setError('Your session expired. Please sign in again.')
    }
    window.addEventListener('auth:required', onUnauthorized)
    return () => window.removeEventListener('auth:required', onUnauthorized)
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    setBusy(true)
    setError(null)
    try {
      setUser(await auth.login(email, password))
      setStatus('authenticated')
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Sign in failed')
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const signUp = useCallback(
    async (email: string, password: string, displayName?: string) => {
      setBusy(true)
      setError(null)
      try {
        setUser(await auth.register(email, password, displayName))
        setStatus('authenticated')
        return true
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Registration failed')
        return false
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const signOut = useCallback(async () => {
    await auth.logout()
    setUser(null)
    setStatus('anonymous')
    setError(null)
  }, [])

  return { user, status, error, busy, signIn, signUp, signOut, setError }
}
