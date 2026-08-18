/** Email verification and password reset — all unauthenticated. */

import { authHeaders } from './auth'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function post(path: string, body: unknown, authed = false): Promise<string> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(authed ? authHeaders() : {}),
    },
    body: JSON.stringify(body),
  })

  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(payload.detail ?? `Request failed (${response.status})`)
  return payload.message ?? 'Done.'
}

export const verifyEmail = (token: string) => post('/account/verify-email', { token })

export const forgotPassword = (email: string) => post('/account/forgot-password', { email })

export const resetPassword = (token: string, password: string) =>
  post('/account/reset-password', { token, password })

export const resendVerification = () =>
  post('/account/resend-verification', {}, true)
