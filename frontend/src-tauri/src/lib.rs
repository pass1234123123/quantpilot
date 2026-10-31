// Minimal desktop shell: loads the existing Vue frontend (dev server or
// bundled dist). No native commands are exposed to the webview.
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running QuantPilot desktop shell");
}
