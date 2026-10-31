/**
 * Secure secret storage for the desktop shell (phase-3 R3).
 *
 * Desktop (Tauri webview): the service API key lives in the OS credential
 * store (Windows Credential Manager) via the Rust `credential_*` commands —
 * never in localStorage. Browser/dev mode has no Tauri bridge and keeps the
 * legacy localStorage entry so the Vite-proxy flow stays unchanged. A
 * pre-R3 desktop install migrates its localStorage key once, then wipes it.
 *
 * Values are cached in memory so request code stays synchronous; init()
 * must run before the app mounts (see main.ts).
 */
import { invoke } from '@tauri-apps/api/core'

const LEGACY_KEY = 'qp.apiKey'
const STORE_KEY = 'api_key'

export function isDesktop(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
}

let apiKey = ''

export async function initSecureStore(): Promise<void> {
  if (!isDesktop()) {
    apiKey = localStorage.getItem(LEGACY_KEY) ?? ''
    return
  }
  const stored = await invoke<string | null>('credential_get', { key: STORE_KEY })
  if (stored) {
    apiKey = stored
    return
  }
  // one-time migration from the pre-R3 localStorage copy, then wipe it
  const legacy = localStorage.getItem(LEGACY_KEY)
  if (legacy) {
    await invoke('credential_set', { key: STORE_KEY, value: legacy })
    localStorage.removeItem(LEGACY_KEY)
    apiKey = legacy
  }
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
