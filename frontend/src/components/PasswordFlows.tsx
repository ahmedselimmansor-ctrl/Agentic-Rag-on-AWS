import { useEffect, useState } from 'react'
import { AlertIcon, CheckIcon, SparkIcon } from './Icons'
import * as accountApi from '@/api/account'

const MIN_PASSWORD = 10

/** Reads ?token= for the verify/reset links that arrive by email. */
export function useTokenFromUrl(): string | null {
  const [token] = useState(() => new URLSearchParams(window.location.search).get('token'))
  return token
}

export type Route = 'app' | 'verify-email' | 'reset-password' | 'forgot-password'

export function currentRoute(): Route {
  const path = window.location.pathname
  if (path.startsWith('/verify-email')) return 'verify-email'
  if (path.startsWith('/reset-password')) return 'reset-password'
  return 'app'
}

function clearUrl() {
  window.history.replaceState({}, '', '/')
}

function Shell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="auth">
      <div className="auth-card">
        <div className="auth-mark" aria-hidden>
          <SparkIcon size={22} />
        </div>
        <h1>{title}</h1>
        {children}
      </div>
    </div>
  )
}

// ------------------------------------------------------------ verify -------
export function VerifyEmailScreen({ onDone }: { onDone: () => void }) {
  const token = useTokenFromUrl()
  const [state, setState] = useState<'working' | 'ok' | 'error'>('working')
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (!token) {
      setState('error')
      setMessage('That link is missing its confirmation token.')
      return
    }
    let cancelled = false
    void accountApi
      .verifyEmail(token)
      .then((m) => {
        if (cancelled) return
        setState('ok')
        setMessage(m)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setState('error')
        setMessage(e instanceof Error ? e.message : 'Could not confirm your email.')
      })
    return () => {
      cancelled = true
    }
  }, [token])

  return (
    <Shell title={state === 'ok' ? 'Email confirmed' : 'Confirming your email'}>
      {state === 'working' && <p className="auth-sub">One moment…</p>}
      {state === 'ok' && (
        <>
          <p className="auth-sub">
            <CheckIcon size={14} /> {message}
          </p>
          <button
            className="auth-submit"
            onClick={() => {
              clearUrl()
              onDone()
            }}
          >
            Continue
          </button>
        </>
      )}
      {state === 'error' && (
        <>
          <div className="auth-error" role="alert">
            <AlertIcon size={14} />
            <span>{message}</span>
          </div>
          <button
            className="auth-submit"
            onClick={() => {
              clearUrl()
              onDone()
            }}
          >
            Back to sign in
          </button>
        </>
      )}
    </Shell>
  )
}

// ------------------------------------------------------------- forgot ------
export function ForgotPasswordScreen({ onBack }: { onBack: () => void }) {
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState<string | null>(null)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!email.includes('@') || busy) return
    setBusy(true)
    try {
      setSent(await accountApi.forgotPassword(email))
    } catch {
      // The endpoint answers identically either way; a network failure should
      // not reveal more than success does.
      setSent('If that address has an account, a reset link is on its way.')
    } finally {
      setBusy(false)
    }
  }

  if (sent) {
    return (
      <Shell title="Check your email">
        <p className="auth-sub">{sent}</p>
        <button className="auth-submit" onClick={onBack}>
          Back to sign in
        </button>
      </Shell>
    )
  }

  return (
    <Shell title="Reset your password">
      <p className="auth-sub">
        Enter your email address and we&rsquo;ll send you a link to choose a new password.
      </p>
      <form onSubmit={submit} className="auth-form">
        <label>
          <span>Email</span>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
            required
            autoFocus
            disabled={busy}
          />
        </label>
        <button type="submit" className="auth-submit" disabled={!email.includes('@') || busy}>
          {busy ? 'Sending…' : 'Send reset link'}
        </button>
      </form>
      <p className="auth-switch">
        <button type="button" onClick={onBack}>
          Back to sign in
        </button>
      </p>
    </Shell>
  )
}

// -------------------------------------------------------------- reset ------
export function ResetPasswordScreen({ onDone }: { onDone: () => void }) {
  const token = useTokenFromUrl()
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD
  const mismatch = confirm.length > 0 && confirm !== password
  const canSubmit = password.length >= MIN_PASSWORD && confirm === password && !busy

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit || !token) return
    setBusy(true)
    setError(null)
    try {
      await accountApi.resetPassword(token, password)
      setDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not reset your password.')
    } finally {
      setBusy(false)
    }
  }

  if (!token) {
    return (
      <Shell title="Reset your password">
        <div className="auth-error" role="alert">
          <AlertIcon size={14} />
          <span>That link is missing its reset token.</span>
        </div>
        <button
          className="auth-submit"
          onClick={() => {
            clearUrl()
            onDone()
          }}
        >
          Back to sign in
        </button>
      </Shell>
    )
  }

  if (done) {
    return (
      <Shell title="Password updated">
        <p className="auth-sub">
          <CheckIcon size={14} /> All other sessions were signed out. Sign in with your new
          password.
        </p>
        <button
          className="auth-submit"
          onClick={() => {
            clearUrl()
            onDone()
          }}
        >
          Sign in
        </button>
      </Shell>
    )
  }

  return (
    <Shell title="Choose a new password">
      {error && (
        <div className="auth-error" role="alert">
          <AlertIcon size={14} />
          <span>{error}</span>
        </div>
      )}
      <form onSubmit={submit} className="auth-form">
        <label>
          <span>New password</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
            autoFocus
            disabled={busy}
          />
          <small className={tooShort ? 'is-warn' : ''}>At least {MIN_PASSWORD} characters.</small>
        </label>
        <label>
          <span>Confirm password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
            disabled={busy}
          />
          {mismatch && <small className="is-warn">Passwords do not match.</small>}
        </label>
        <button type="submit" className="auth-submit" disabled={!canSubmit}>
          {busy ? 'Updating…' : 'Update password'}
        </button>
      </form>
    </Shell>
  )
}
