/**
 * Secure secret storage: the user's login credentials (R2 P1-03 + R3 P1-02).
 *
 * Two SEPARATE credential slots - a JWT and a personal API key are
 * different credential types and are never conflated:
 *   - auth_token     JWT issued by POST /api/v1/auth/login (password login)
 *   - login_api_key  personal qpk_... key entered on the LOGIN page
 *                     (R3 P1-02); travels via X-API-Key, never as a JWT
 *
 * Desktop (Tauri webview): both live in the OS credential store
 * (Windows Credential Manager) via the Rust `credential_*` commands -
 * never in localStorage. Browser/dev mode has no Tauri bridge: both go
 * to sessionStorage (survives reload, dies with the tab). Pre-remediation
 * installs (token under localStorage qp.token) are migrated once and
 * wiped - tokens never stay in localStorage.
 *
 * The R2 "Settings key" slot (api_key / qp.apiKey) is RETIRED: the shared
 * service key is a machine lane and never a user identity. A personal
 * qpk_ key previously stored there is migrated once into the login-key
 * slot; the legacy entry is wiped either way.
 *
 * Values are cached in memory so request code stays synchronous; init()
 * must run before the app mounts (see main.ts).
 */
import { invoke } from '@tauri-apps/api/core'
import { ref } from 'vue'

const LEGACY_KEY = 'qp.apiKey' // R2 dev-mode Settings key (retired)
const LEGACY_KEY_STORE_KEY = 'api_key' // R2 desktop Settings key (retired)
const LOGIN_KEY_STORE_KEY = 'login_api_key'
const TOKEN_STORE_KEY = 'auth_token'
const LEGACY_TOKEN_KEY = 'qp.token'
const LEGACY_USER_KEY = 'qp.user'
const SESSION_TOKEN_KEY = 'qp.session.token' // browser/dev fallback only
const SESSION_LOGIN_KEY = 'qp.session.loginKey' // browser/dev fallback only

export function isDesktop(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

export interface SessionUser {
  user_id: string
  username: string
  role: string
}

let loginApiKey = ''
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

async function initDesktop(): Promise<void> {
  // R3 P1-02: one-time migration of the retired R2 Settings key. A
  // personal qpk_ key stored there becomes the login key; anything else
  // (e.g. a shared service key) is dropped - the slot is never a
  // credential source again and is wiped either way.
  let loginKey =
    (await invoke<string | null>('credential_get', { key: LOGIN_KEY_STORE_KEY })) ?? ''
  if (!loginKey) {
    const legacy = await invoke<string | null>('credential_get', {
      key: LEGACY_KEY_STORE_KEY,
    })
    if (legacy && legacy.startsWith('qpk_')) {
      await invoke('credential_set', { key: LOGIN_KEY_STORE_KEY, value: legacy })
      loginKey = legacy
    }
  }
  await invoke('credential_delete', { key: LEGACY_KEY_STORE_KEY }).catch(() => {})
  localStorage.removeItem(LEGACY_KEY)
  loginApiKey = loginKey

  const token = await invoke<string | null>('credential_get', { key: TOKEN_STORE_KEY })
  authToken = token ?? (await migrateLegacyToken())
}

export async function initSecureStore(): Promise<void> {
  if (!isDesktop()) {
    localStorage.removeItem(LEGACY_KEY) // retired Settings key slot
    await migrateLegacyToken()
    authToken = sessionStorage.getItem(SESSION_TOKEN_KEY) ?? ''
    loginApiKey = sessionStorage.getItem(SESSION_LOGIN_KEY) ?? ''
    return
  }
  await initDesktop()
}

// -- personal login API key (qpk_) ---------------------------------------
// The key entered on the LOGIN page (R3 P1-02). Desktop: OS credential
// store. Browser/dev: sessionStorage. NEVER localStorage, and NEVER the
// same slot as the JWT - the two credential types stay separate.

export function getLoginApiKey(): string {
  return loginApiKey
}

export async function setLoginApiKey(value: string): Promise<void> {
  const trimmed = value.trim()
  if (isDesktop()) {
    await invoke('credential_set', { key: LOGIN_KEY_STORE_KEY, value: trimmed })
  } else {
    sessionStorage.setItem(SESSION_LOGIN_KEY, trimmed)
  }
  loginApiKey = trimmed
}

export async function clearLoginApiKey(): Promise<void> {
  if (isDesktop()) {
    await invoke('credential_delete', { key: LOGIN_KEY_STORE_KEY })
  } else {
    sessionStorage.removeItem(SESSION_LOGIN_KEY)
  }
  loginApiKey = ''
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
