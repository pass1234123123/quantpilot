/**
 * Secure secret storage: desktop shell service key (phase-3 R3) + the
 * user-session JWT (R2 remediation P1-03).
 *
 * Desktop (Tauri webview): secrets live in the OS credential store
 * (Windows Credential Manager) via the Rust `credential_*` commands —
 * never in localStorage. Browser/dev mode has no Tauri bridge: the
 * service key keeps its legacy localStorage entry, while the session
 * token goes to sessionStorage (survives reload, dies with the tab).
 * Pre-remediation installs (token under localStorage qp.token) are
 * migrated once and wiped - tokens never stay in localStorage.
 *
 * Values are cached in memory so request code stays synchronous; init()
 * must run before the app mounts (see main.ts).
 */
import { invoke } from '@tauri-apps/api/core'
import { ref } from 'vue'

const LEGACY_KEY = 'qp.apiKey'
const STORE_KEY = 'api_key'
const TOKEN_STORE_KEY = 'auth_token'
const LEGACY_TOKEN_KEY = 'qp.token'
const LEGACY_USER_KEY = 'qp.user'
const SESSION_TOKEN_KEY = 'qp.session.token' // browser/dev fallback only

export function isDesktop(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export interface SessionUser {
  user_id: string
  username: string
  role: string
}

let apiKey = ''
let authToken = ''
/** Reactive so role-gated UI updates on login/logout without a store. */
const sessionUser = ref<SessionUser | null>(null)

async function migrateLegacyToken(): Promise<string> {
  // P1-03: one-time migration of the pre-remediation localStorage token,
  // then wipe both legacy entries from localStorage for good.
  const legacyToken = localStorage.getItem(LEGACY_TOKEN_KEY)
  if (!legacyToken) return ''
  if (isDesktop()) {
    await invoke('credential_set', { key: TOKEN_STORE_KEY, value: legacyToken })
  } else {
    sessionStorage.setItem(SESSION_TOKEN_KEY, legacyToken)
  }
  localStorage.removeItem(LEGACY_TOKEN_KEY)
  localStorage.removeItem(LEGACY_USER_KEY)
  return legacyToken
}

export async function initSecureStore(): Promise<void> {
  if (!isDesktop()) {
    apiKey = localStorage.getItem(LEGACY_KEY) ?? ''
    await migrateLegacyToken()
    authToken = sessionStorage.getItem(SESSION_TOKEN_KEY) ?? ''
    return
  }
  const stored = await invoke<string | null>('credential_get', { key: STORE_KEY })
  if (stored) {
    apiKey = stored
  } else {
    // one-time migration from the pre-R3 localStorage copy, then wipe it
    const legacy = localStorage.getItem(LEGACY_KEY)
    if (legacy) {
      await invoke('credential_set', { key: STORE_KEY, value: legacy })
      localStorage.removeItem(LEGACY_KEY)
      apiKey = legacy
    }
  }
  const token = await invoke<string | null>('credential_get', { key: TOKEN_STORE_KEY })
  authToken = token ?? (await migrateLegacyToken())
}

export function getApiKey(): string {
  return apiKey
}

export async function setApiKey(value: string): Promise<void> {
  const trimmed = value.trim()
  if (isDesktop()) {
    await invoke('credential_set', { key: STORE_KEY, value: trimmed })
  } else {
    localStorage.setItem(LEGACY_KEY, trimmed)
  }
  apiKey = trimmed
}

export async function clearApiKey(): Promise<void> {
  if (isDesktop()) {
    await invoke('credential_delete', { key: STORE_KEY })
  } else {
    localStorage.removeItem(LEGACY_KEY)
  }
  apiKey = ''
}

// -- user session token (JWT) --------------------------------------------
// Desktop: OS credential store. Browser/dev: sessionStorage.
// NEVER localStorage (R2 acceptance P1-03).

export function getAuthToken(): string {
  return authToken
}

export async function setAuthToken(value: string): Promise<void> {
  const trimmed = value.trim()
  if (isDesktop()) {
    await invoke('credential_set', { key: TOKEN_STORE_KEY, value: trimmed })
  } else {
    sessionStorage.setItem(SESSION_TOKEN_KEY, trimmed)
  }
  authToken = trimmed
}

export async function clearAuthToken(): Promise<void> {
  if (isDesktop()) {
    await invoke('credential_delete', { key: TOKEN_STORE_KEY })
  } else {
    sessionStorage.removeItem(SESSION_TOKEN_KEY)
  }
  authToken = ''
}

// -- session user (username/role) ----------------------------------------
// UI hints only - the backend re-validates the role on every request;
// this is not a trust boundary. Not persisted: reload restores it via
// GET /api/v1/auth/me.

export function getSessionUser(): SessionUser | null {
  return sessionUser.value
}

export function setSessionUser(user: SessionUser | null): void {
  sessionUser.value = user
}
