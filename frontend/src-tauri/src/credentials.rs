//! OS credential store bridge (Windows Credential Manager via keyring).
//!
//! Three tiny commands is the whole API surface; the webview cannot read
//! arbitrary entries, only its own `com.quantpilot.desktop` service. A
//! missing entry reads as `None` and deleting one that is already gone is
//! not an error, so the frontend can treat the store as a simple map.

use keyring::Entry;

const SERVICE: &str = "com.quantpilot.desktop";

fn entry(key: &str) -> Result<Entry, String> {
    Entry::new(SERVICE, key).map_err(|e| format!("credential store unavailable: {e}"))
}

#[tauri::command]
pub fn credential_set(key: String, value: String) -> Result<(), String> {
    entry(&key)?
        .set_password(&value)
        .map_err(|e| format!("failed to store credential: {e}"))
}

#[tauri::command]
pub fn credential_get(key: String) -> Result<Option<String>, String> {
    match entry(&key)?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(format!("failed to read credential: {e}")),
    }
}

#[tauri::command]
pub fn credential_delete(key: String) -> Result<(), String> {
    match entry(&key)?.delete_credential() {
        Ok(()) => Ok(()),
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(format!("failed to delete credential: {e}")),
    }
}
