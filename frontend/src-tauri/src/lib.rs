// Desktop shell with a hardened security posture (phase-3 R3):
//   - secrets live in the OS credential store (Windows Credential Manager
//     via the keyring crate), never in WebView localStorage
//   - top-level navigation is restricted to the app's own origins
//   - the webview gets exactly three credential commands, nothing else
pub mod credentials;

use tauri::{WebviewUrl, WebviewWindowBuilder};

/// Only the bundled app origin (WebView2 serves dist via
/// http://tauri.localhost) and the Vite dev server (:5173) may host a
/// top-level navigation. The API base never needs navigation: the frontend
/// reaches it via fetch (CSP connect-src governs that instead).
fn is_app_origin(url: &tauri::Url) -> bool {
    let host = url.host_str().unwrap_or_default();
    let port = url.port_or_known_default();
    match host {
        "tauri.localhost" => true,
        "localhost" | "127.0.0.1" => port == Some(5173),
        _ => false,
    }
}

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            credentials::credential_set,
            credentials::credential_get,
            credentials::credential_delete,
        ])
        .setup(|app| {
            // window built here (not in tauri.conf.json) because the
            // navigation allowlist can only be attached programmatically
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("QuantPilot")
                .inner_size(1440.0, 900.0)
                .min_inner_size(1100.0, 700.0)
                .center()
                .on_navigation(is_app_origin)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running QuantPilot desktop shell");
}
