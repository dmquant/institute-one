# Institute One — Obsidian plugin

Talk to the [Institute One](../README.md) backend from inside Obsidian: ask an
analyst, queue deep research, feed the whiteboard topic pool, and keep an eye
on the task queue from the status bar.

Desktop only. The backend must be running (default `http://127.0.0.1:8100`).

## Manual install

1. Build the plugin (requires Node.js ≥ 18):

   ```sh
   cd obsidian-plugin
   npm install
   npm run build      # produces main.js
   ```

2. Copy the plugin folder into your vault:

   ```sh
   mkdir -p "<YourVault>/.obsidian/plugins/institute-one"
   cp manifest.json main.js "<YourVault>/.obsidian/plugins/institute-one/"
   ```

   (Copying the whole `obsidian-plugin` folder as
   `<YourVault>/.obsidian/plugins/institute-one` also works — Obsidian only
   needs `manifest.json` and `main.js`.)

3. In Obsidian: **Settings → Community plugins** → turn off Restricted mode if
   needed → enable **Institute One**.

4. Start the backend, then check the plugin settings tab if your backend is
   not at the default `http://127.0.0.1:8100`.

## Commands

| Command | What it does |
| --- | --- |
| Ask the Institute | Pick an analyst, send a prompt (prefilled with selection), insert the answer below the cursor — or into a new `Ask/<date> <prompt>.md` note when no editor is active. |
| Queue deep research | Send selection (or prompted topic) to `POST /api/research/queue`; reports dedupe/cooldown refusals. |
| Add to whiteboard topic pool | Send selection (or prompted topic) to `POST /api/whiteboard/topics`. |
| Institute: open operator UI | Open the backend web UI in your browser. |

## Status bar

`⚙︎ inst: N running / M queued` — refreshed from `GET /api/meta` every 60 s.
A red `✗ inst` means the backend is unreachable. Click it to open the operator
UI.

## Notes

- The backend itself exports finished work into the vault
  (`Research/`, `Briefing/`, `Daily/`, `Whiteboard/`) — see
  `app/vault/exporter.py`. The plugin only adds `Ask/` notes.
- All requests time out after 10 s, except "Ask the Institute", which waits
  up to 10 minutes because `POST /api/ask` is synchronous on the backend.

## Development

```sh
npm run dev   # esbuild watch mode (inline sourcemaps)
```
