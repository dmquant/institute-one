# Task: capture UI screenshots of the institute-one operator SPA

The SPA is served by a Vite dev server at `http://127.0.0.1:5180` (already running, proxying API calls to the live backend — pages show real data). Capture one PNG per page below and save into THIS directory (`institute-one/docs/screenshots/`), overwriting any existing files.

| URL | Output file |
|---|---|
| http://127.0.0.1:5180/ | dashboard.png |
| http://127.0.0.1:5180/analysts | analysts.png |
| http://127.0.0.1:5180/tasks | tasks.png |
| http://127.0.0.1:5180/workflows | workflows.png |
| http://127.0.0.1:5180/research | research.png |
| http://127.0.0.1:5180/whiteboard | whiteboard.png |
| http://127.0.0.1:5180/mailbox | mailbox.png |
| http://127.0.0.1:5180/settings | settings.png |

Requirements:
- Viewport 1440x900, no scrollbars.
- The pages are a client-side React SPA: after navigation you MUST wait for network/XHR to settle (>= 3 seconds after load, or networkidle) before capturing, otherwise you get empty loading states.
- Verify each PNG: file size should be > 40 KB (a dark-themed UI with content); if a capture is tiny or shows "This site can't be reached" / a blank page, retry it.
- Recommended approach: a small Node script using puppeteer-core with the system Chrome at "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" (use a throwaway --user-data-dir under /tmp), `page.goto(url, {waitUntil: "networkidle0"})` plus a 2s sleep, `page.screenshot()`. You may `npm install puppeteer-core` in a temp directory (e.g. /tmp/shotwork). Plain `chrome --headless --screenshot` has been hanging on this machine — avoid it.
- When all 8 PNGs are verified, print a final line: `SCREENSHOTS_DONE <total bytes>`.
