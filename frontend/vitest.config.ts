import { defineConfig } from "vitest/config";

// jsdom: the useSSE tests render real hooks through react-dom/client and
// drive fetch/timers with vitest fakes — no browser needed.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
