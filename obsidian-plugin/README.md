# Institute One — Obsidian plugin

Talk to the [Institute One](../README.md) backend from inside Obsidian: ask an
analyst, queue deep research, feed the whiteboard topic pool, track roadmap
execution, and keep an eye on the task queue from the status bar.

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

Command names in Obsidian's palette are Chinese (e.g. "Institute: 打开路线图");
the table below uses English glosses. Typing "Institute" in the palette lists
them all.

| Command | What it does |
| --- | --- |
| Ask the Institute | Pick an analyst, send a prompt (prefilled with selection), insert the answer below the cursor — or into a new `Ask/<date> <prompt>.md` note when no editor is active. |
| Queue deep research | Send selection (or prompted topic) to `POST /api/research/queue`; reports dedupe/cooldown refusals. |
| Add to whiteboard topic pool | Send selection (or prompted topic) to `POST /api/whiteboard/topics`. |
| Institute: open roadmap | Open the local roadmap Kanban view seeded from `roadmap/backlog.json`. |
| Institute: open operator UI | Open the backend web UI in your browser. |

## Roadmap Kanban

The roadmap view is native to this plugin: `roadmap/backlog.json` is bundled
into `main.js` at build time (rebuild to pick up board changes), and card-status
moves persist as local overrides in plugin data until the backend roadmap API
exists. It can also write `Institute/Roadmap/Implementation Kanban.md` in a
markdown-backed Kanban shape, so users with an existing Obsidian Kanban plugin
can open the same roadmap as a normal board note.

## Status bar

`⚙︎ inst: N运行/M排队` (plus a `·日报X/Y` suffix while analyst dailies are
pending) — refreshed from `GET /api/meta` every 60 s. A red `✗ inst` means the
backend is unreachable. Click it to open the dashboard view.

## Notes

- The backend itself exports finished work into the vault
  (`Research/`, `Briefing/`, `Daily/`, `Analysts/`, `Whiteboard/`) — see
  `app/vault/exporter.py`. The plugin only adds `Ask/` notes and the optional
  roadmap Kanban export.
- All requests time out after 10 s, except "Ask the Institute", which waits
  up to 15 minutes because `POST /api/ask` is synchronous on the backend.

## Development

```sh
npm run dev   # esbuild watch mode (inline sourcemaps)
```
