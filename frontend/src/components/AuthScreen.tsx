import { useState } from 'react'
import { AlertIcon, SparkIcon } from './Icons'

interface AuthScreenProps {
  error: string | null
  busy: boolean
  onSignIn: (email: string, password: string) => Promise<boolean>
  onSignUp: (email: string, password: string, displayName?: string) => Promise<boolean>
  onDismissError: () => void
}

const MIN_PASSWORD = 10

export function AuthScreen({
  error,
  busy,
  onSignIn,
  onSignUp,
  onDismissError,
}: AuthScreenProps) {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')

  const isSignUp = mode === 'signup'
  const tooShort = isSignUp && password.length > 0 && password.length < MIN_PASSWORD
  const canSubmit =
    email.includes('@') && password.length >= (isSignUp ? MIN_PASSWORD : 1) && !busy

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    if (isSignUp) await onSignUp(email, password, displayName.trim() || undefined)
    else await onSignIn(email, password)
  }

  const switchMode = () => {
    setMode(isSignUp ? 'signin' : 'signup')
    onDismissError()
  }

  return (
    <div className="auth">
      <div className="auth-card">
        <div className="auth-mark" aria-hidden>
          <SparkIcon size={22} />
        </div>

        <h1>{isSignUp ? 'Create an account' : 'Sign in'}</h1>
        <p className="auth-sub">
          {isSignUp
            ? 'Your documents and chat history stay private to your account.'
            : 'Welcome back to Agentic RAG.'}
        </p>

        {error && (
          <div className="auth-error" role="alert">
            <AlertIcon size={14} />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={submit} className="auth-form">
          {isSignUp && (
            <label>
              <span>Name <em>optional</em></span>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="name"
                maxLength={200}
                disabled={busy}
              />
            </label>
          )}

          <label>
            <span>Email</span>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
              maxLength={320}
              disabled={busy}
              autoFocus
            />
          </label>

          <label>
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isSignUp ? 'new-password' : 'current-password'}
              required
              maxLength={200}
              disabled={busy}
            />
            {isSignUp && (
              <small className={tooShort ? 'is-warn' : ''}>
                At least {MIN_PASSWORD} characters. Length matters more than symbols —
                a passphrase works well.
              </small>
            )}
          </label>

          <button type="submit" className="auth-submit" disabled={!canSubmit}>
            {busy ? 'Working…' : isSignUp ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <p className="auth-switch">
          {isSignUp ? 'Already have an account?' : 'No account yet?'}{' '}
          <button type="button" onClick={switchMode} disabled={busy}>
            {isSignUp ? 'Sign in' : 'Create one'}
          </button>
        </p>
      </div>
    </div>
  )
}
