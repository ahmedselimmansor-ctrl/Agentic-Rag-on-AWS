/**
 * Token storage and the refresh dance.
 *
 * The access token is short-lived, so any request can hit a 401 mid-session.
 * `withFreshToken` refreshes once and retries — and concurrent callers share a
 * single in-flight refresh, otherwise a page that fires five requests at once
 * would rotate the refresh token five times and trip the replay detector.
 */

const ACCESS_KEY = 'auth.access'
const REFRESH_KEY = 'auth.refresh'
const USER_KEY = 'auth.user'

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

export interface AuthUser {
  id: string
  email: string
  display_name: string | null
  created_at: string
  last_login_at: string | null
}

interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

export class AuthRequiredError extends Error {
  constructor(message = 'Not authenticated') {
    super(message)
    this.name = 'AuthRequiredError'
  }
}

// ------------------------------------------------------------- storage ----
export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  user: (): AuthUser | null => {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw) as AuthUser
    } catch {
      return null
    }
  },
  save(payload: TokenResponse) {
    localStorage.setItem(ACCESS_KEY, payload.access_token)
    localStorage.setItem(REFRESH_KEY, payload.refresh_token)
    localStorage.setItem(USER_KEY, JSON.stringify(payload.user))
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
    localStorage.removeItem(USER_KEY)
  },
}

// --------------------------------------------------------------- calls ----
async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail ?? detail
    } catch {
      /* keep the default */
    }
    throw new Error(detail)
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T)
}

export async function register(
  email: string,
  password: string,
  displayName?: string,
): Promise<AuthUser> {
  const payload = await post<TokenResponse>('/auth/register', {
    email,
    password,
    display_name: displayName || null,
  })
  tokens.save(payload)
  return payload.user
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const payload = await post<TokenResponse>('/auth/login', { email, password })
  tokens.save(payload)
  return payload.user
}

export async function logout(): Promise<void> {
  const refresh = tokens.refresh()
  if (refresh) {
    // Best-effort: local state must clear even if the network call fails.
    await post('/auth/logout', { refresh_token: refresh }).catch(() => undefined)
  }
  tokens.clear()
}

let inFlightRefresh: Promise<string> | null = null

export function refreshAccessToken(): Promise<string> {
  if (inFlightRefresh) return inFlightRefresh

  const refresh = tokens.refresh()
  if (!refresh) return Promise.reject(new AuthRequiredError())

  inFlightRefresh = post<TokenResponse>('/auth/refresh', { refresh_token: refresh })
    .then((payload) => {
      tokens.save(payload)
      return payload.access_token
    })
    .catch((error) => {
      // Refresh failed => the session is genuinely over. Broadcast it so the
      // app can drop to the sign-in screen from wherever the failure surfaced,
      // instead of every call site having to handle it.
      tokens.clear()
      window.dispatchEvent(new CustomEvent('auth:required'))
      throw new AuthRequiredError(error instanceof Error ? error.message : undefined)
    })
    .finally(() => {
      inFlightRefresh = null
    })

  return inFlightRefresh
}

export function authHeaders(): Record<string, string> {
  const access = tokens.access()
  return access ? { Authorization: `Bearer ${access}` } : {}
}

/**
 * Runs `attempt` with the current token; on 401 refreshes once and retries.
 * `attempt` receives the headers to use so it can be either fetch or a stream.
 */
export async function withFreshToken<T>(
  attempt: (headers: Record<string, string>) => Promise<T>,
  isUnauthorized: (result: T) => boolean,
): Promise<T> {
  const first = await attempt(authHeaders())
  if (!isUnauthorized(first)) return first

  await refreshAccessToken() // throws AuthRequiredError if the session is over
  return attempt(authHeaders())
}

export function isAuthenticated(): boolean {
  return Boolean(tokens.access() && tokens.refresh())
}
