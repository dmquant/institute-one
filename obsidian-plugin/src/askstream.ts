import { ItemView, Notice, WorkspaceLeaf } from "obsidian";
import { ASK_TIMEOUT_MS, Analyst, ApiError, AskStreamFrame, AskTask, errMsg, isMissingEndpoint } from "./api";
import type InstituteOnePlugin from "./main";

export const VIEW_TYPE_ASK_STREAM = "institute-ask-stream";

// ---------------------------------------------------------------------------
// 流式问答 (streaming ask) — the ONE deliberate fetch() exception.
//
// CLAUDE.md says plugin HTTP must go through Obsidian's requestUrl because
// the backend sends no CORS headers — but requestUrl buffers the whole
// response and cannot stream. POST /api/ask/stream is NDJSON, so this view
// tries window.fetch against the loopback backend first:
//   - environments where the renderer doesn't enforce CORS for the local
//     backend get live streaming;
//   - environments that DO enforce CORS make fetch throw (TypeError on the
//     failed preflight — the backend has no CORS middleware, so OPTIONS is
//     405), and we fall back automatically to the synchronous requestUrl
//     /api/ask path. Missing-endpoint statuses (404/405/501 — older backend)
//     fall back the same way.
// The primary "Institute: 提问 (Ask)" command stays requestUrl-only.
// ---------------------------------------------------------------------------

export class AskStreamView extends ItemView {
	private plugin: InstituteOnePlugin;
	private roster: Analyst[] = [];
	private analystId = "";
	private prompt = "";
	private running = false;
	private abort: AbortController | null = null;

	private selectEl!: HTMLSelectElement;
	private inputEl!: HTMLTextAreaElement;
	private askBtn!: HTMLButtonElement;
	private stopBtn!: HTMLButtonElement;
	private statusEl!: HTMLElement;
	private outEl!: HTMLElement;
	/** accumulated stdout of the current run (for the copy button) */
	private answer = "";

	constructor(leaf: WorkspaceLeaf, plugin: InstituteOnePlugin) {
		super(leaf);
		this.plugin = plugin;
		this.navigation = false;
	}

	getViewType(): string {
		return VIEW_TYPE_ASK_STREAM;
	}

	getDisplayText(): string {
		return "Institute 流式问答";
	}

	getIcon(): string {
		return "message-square";
	}

	async onOpen(): Promise<void> {
		const root = this.contentEl;
		root.empty();
		root.style.display = "flex";
		root.style.flexDirection = "column";
		root.style.height = "100%";
		root.style.padding = "8px 12px";
		root.style.fontSize = "13px";

		// ---- controls -----------------------------------------------------
		const controls = root.createDiv();
		controls.style.flexShrink = "0";

		this.selectEl = controls.createEl("select");
		this.selectEl.style.width = "100%";
		this.selectEl.style.marginBottom = "6px";
		this.selectEl.createEl("option", { text: "（默认执行手，无人格）", value: "" });
		this.selectEl.addEventListener("change", () => {
			this.analystId = this.selectEl.value;
		});
		void this.plugin
			.getRoster()
			.then((roster) => {
				this.roster = roster;
				for (const a of roster) {
					this.selectEl.createEl("option", {
						text: `${a.emoji} ${a.name}（${a.name_en}）`,
						value: a.id,
					});
				}
				const last = this.plugin.settings.defaultAnalyst;
				if (last && roster.some((a) => a.id === last)) {
					this.selectEl.value = last;
					this.analystId = last;
				}
			})
			.catch(() => {
				/* backend offline: leave the empty option; ask will surface the error */
			});

		this.inputEl = controls.createEl("textarea");
		this.inputEl.rows = 3;
		this.inputEl.placeholder = "想让研究所研究什么？（Cmd/Ctrl+Enter 提问）";
		this.inputEl.style.width = "100%";
		this.inputEl.style.resize = "vertical";
		this.inputEl.addEventListener("input", () => {
			this.prompt = this.inputEl.value;
		});
		this.inputEl.addEventListener("keydown", (ev) => {
			if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
				ev.preventDefault();
				void this.submit();
			}
		});

		const btns = controls.createDiv();
		btns.style.display = "flex";
		btns.style.gap = "8px";
		btns.style.margin = "6px 0";
		this.askBtn = btns.createEl("button", { text: "提问" });
		this.askBtn.addEventListener("click", () => void this.submit());
		this.stopBtn = btns.createEl("button", { text: "停止" });
		this.stopBtn.style.display = "none";
		this.stopBtn.addEventListener("click", () => this.stop());
		const copyBtn = btns.createEl("button", { text: "复制回答" });
		copyBtn.addEventListener("click", () => {
			if (!this.answer.trim()) {
				new Notice("Institute: 尚无回答可复制。");
				return;
			}
			void navigator.clipboard.writeText(this.answer);
			new Notice("Institute: 已复制。", 3000);
		});

		this.statusEl = controls.createDiv();
		this.statusEl.style.color = "var(--text-muted)";
		this.statusEl.style.fontSize = "11px";
		this.statusEl.style.marginBottom = "6px";
		this.statusEl.setText("空闲 — 流式输出优先，不可用时自动回落同步模式。");

		// ---- output -----------------------------------------------------------
		this.outEl = root.createDiv();
		this.outEl.style.flex = "1";
		this.outEl.style.overflowY = "auto";
		this.outEl.style.whiteSpace = "pre-wrap";
		this.outEl.style.wordBreak = "break-word";
		this.outEl.style.fontFamily = "var(--font-monospace)";
		this.outEl.style.fontSize = "12px";
		this.outEl.style.border = "1px solid var(--background-modifier-border)";
		this.outEl.style.borderRadius = "6px";
		this.outEl.style.padding = "8px";
		this.outEl.style.background = "var(--background-secondary)";
	}

	async onClose(): Promise<void> {
		this.abort?.abort();
		this.contentEl.empty();
	}

	// ---- run lifecycle ---------------------------------------------------------

	private setRunning(running: boolean): void {
		this.running = running;
		this.askBtn.disabled = running;
		this.stopBtn.style.display = running ? "" : "none";
	}

	private stop(): void {
		this.abort?.abort();
		this.status(
			"已停止接收 — 后端任务继续运行，结果稍后可在任务记录（GET /api/tasks）查到。",
		);
		this.setRunning(false);
	}

	private status(text: string): void {
		this.statusEl.setText(text);
	}

	private append(text: string, cls: "stdout" | "stderr" | "status"): void {
		if (!text) return;
		const span = this.outEl.createSpan({ text });
		if (cls === "stderr") span.style.color = "var(--color-orange)";
		if (cls === "status") {
			span.style.color = "var(--text-muted)";
			span.style.fontStyle = "italic";
		}
		this.outEl.scrollTop = this.outEl.scrollHeight;
	}

	private async submit(): Promise<void> {
		const prompt = this.prompt.trim();
		if (!prompt) {
			new Notice("Institute: 问题为空。");
			return;
		}
		if (this.running) {
			new Notice("Institute: 上一问仍在进行，先停止或等待完成。");
			return;
		}
		// remember the analyst choice like the Ask modal does
		this.plugin.settings.defaultAnalyst = this.analystId;
		void this.plugin.saveSettings();

		this.outEl.empty();
		this.answer = "";
		this.setRunning(true);
		const started = Date.now();
		try {
			const streamed = await this.tryStream(prompt, started);
			if (!streamed) await this.fallbackSync(prompt, started);
		} finally {
			this.setRunning(false);
		}
	}

	/**
	 * fetch + NDJSON 流式路径。返回 false = 环境/后端不支持流式（CORS 拦截、
	 * 404/405/501），调用方回落同步；其他错误（含中途断流）就地报告，不回落。
	 * 设置了访问令牌时带 Authorization Bearer；整个流与同步 /api/ask 共享同一
	 * 总预算（ASK_TIMEOUT_MS）——超时按「任务可能已在后端运行」处理，绝不重跑。
	 */
	private async tryStream(prompt: string, started: number): Promise<boolean> {
		const ctrl = new AbortController();
		this.abort = ctrl;
		let timedOut = false;
		const timeout = window.setTimeout(() => {
			timedOut = true;
			ctrl.abort();
		}, ASK_TIMEOUT_MS);
		try {
			return await this.streamOnce(prompt, started, ctrl, () => timedOut);
		} finally {
			window.clearTimeout(timeout);
		}
	}

	private async streamOnce(
		prompt: string,
		started: number,
		ctrl: AbortController,
		timedOut: () => boolean,
	): Promise<boolean> {
		const url = this.plugin.api.baseUrl() + "/api/ask/stream";
		const headers: Record<string, string> = { "Content-Type": "application/json" };
		const token = this.plugin.settings.token.trim();
		if (token) headers["Authorization"] = `Bearer ${token}`;
		let resp: Response;
		try {
			resp = await fetch(url, {
				method: "POST",
				headers,
				body: JSON.stringify({ prompt, analyst_id: this.analystId || null }),
				signal: ctrl.signal,
			});
		} catch (e) {
			if (ctrl.signal.aborted) {
				if (timedOut()) {
					this.status(
						`流式请求超时（${Math.round(ASK_TIMEOUT_MS / 60_000)} 分钟）— 任务可能已在后端运行，结果见任务记录。`,
					);
				}
				return true; // user hit 停止 during connect, or the total budget expired
			}
			// TypeError: CORS preflight rejected / network refused — fall back
			this.status(`流式不可用（${errMsg(e).slice(0, 120)}）— 回落同步模式…`);
			return false;
		}
		if (resp.status === 401) {
			// the endpoint answered: the token was rejected — don't re-run
			this.append(
				"鉴权失败：HTTP 401 — 后端启用了 INSTITUTE_TOKEN，请在插件设置中填写或核对访问令牌 (bearer token)。\n",
				"stderr",
			);
			this.status("鉴权失败（401）— 请检查访问令牌。");
			new Notice(
				"Institute: 流式提问鉴权失败（401）— 请在设置中检查访问令牌 (bearer token)。",
				8000,
			);
			return true;
		}
		if (resp.status === 404 || resp.status === 405 || resp.status === 501) {
			this.status(`后端无流式端点（HTTP ${resp.status}）— 回落同步模式…`);
			return false;
		}
		if (!resp.ok || !resp.body) {
			const detail = (await resp.text().catch(() => "")).slice(0, 300);
			this.append(`请求失败：HTTP ${resp.status} — ${detail}\n`, "stderr");
			this.status("失败。");
			return true; // the endpoint answered: the error is real, don't re-run
		}

		this.status("流式接收中…");
		const reader = resp.body.getReader();
		const decoder = new TextDecoder();
		let buf = "";
		// object container: assignment happens inside the closure below, and TS
		// narrowing on a plain `let` would type later reads as `null`
		const final: { task: AskTask | null } = { task: null };
		const handleLine = (line: string) => {
			if (!line.trim()) return;
			let frame: AskStreamFrame;
			try {
				frame = JSON.parse(line) as AskStreamFrame;
			} catch {
				return; // tolerate a torn frame rather than killing the stream
			}
			if (frame.type === "done") {
				final.task = frame.task ?? null;
			} else if (frame.type === "stdout") {
				this.answer += frame.text ?? "";
				this.append(frame.text ?? "", "stdout");
			} else if (frame.type === "stderr") {
				this.append(frame.text ?? "", "stderr");
			} else {
				this.append(`\n[${frame.text ?? ""}]\n`, "status");
			}
		};
		try {
			for (;;) {
				const chunk = await reader.read();
				if (chunk.done) break;
				buf += decoder.decode(chunk.value, { stream: true });
				let idx: number;
				while ((idx = buf.indexOf("\n")) >= 0) {
					handleLine(buf.slice(0, idx));
					buf = buf.slice(idx + 1);
				}
			}
			buf += decoder.decode();
			if (buf.trim()) handleLine(buf);
		} catch (e) {
			if (ctrl.signal.aborted) {
				if (timedOut()) {
					this.append(
						`\n[流超时（${Math.round(ASK_TIMEOUT_MS / 60_000)} 分钟）— 完整输出在后端任务记录]\n`,
						"stderr",
					);
					this.status("流超时。");
				}
				return true; // user-initiated stop, or the total budget expired
			}
			this.append(`\n[流中断：${errMsg(e)} — 完整输出在后端任务记录]\n`, "stderr");
			this.status("流中断。");
			return true; // the run is already submitted server-side: never re-run
		}

		const s = Math.round((Date.now() - started) / 1000);
		const task = final.task;
		if (task) {
			// streamed stdout may be empty (some hands only write at the end):
			// fall back to the done-frame output so the answer is never blank
			if (!this.answer.trim() && (task.output ?? "").trim()) {
				this.answer = task.output;
				this.append(task.output, "stdout");
			}
			if (task.error) this.append(`\n[错误：${task.error}]\n`, "stderr");
			this.status(
				`完成（任务 ${task.id ?? "?"}，${task.status}，${s}s，流式）。`,
			);
		} else {
			this.status(`流结束但缺少 done 帧（${s}s）— 结果以后端任务记录为准。`);
		}
		return true;
	}

	/** requestUrl 同步回落：一次性拿完整输出（与 Ask 命令同一后端路径）。 */
	private async fallbackSync(prompt: string, started: number): Promise<void> {
		const analyst = this.roster.find((a) => a.id === this.analystId) ?? null;
		const who = analyst ? `${analyst.emoji} ${analyst.name}` : "Institute";
		this.append(`[同步模式] ${who} 思考中，完成前无输出…\n\n`, "status");
		try {
			const task = await this.plugin.api.ask(prompt, this.analystId || null);
			const out = (task.output ?? "").trim();
			this.answer = out;
			if (out) this.append(out, "stdout");
			if (task.error) this.append(`\n[错误：${task.error}]\n`, "stderr");
			const s = Math.round((Date.now() - started) / 1000);
			this.status(`完成（任务 ${task.id}，${task.status}，${s}s，同步回落）。`);
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.status("后端不可达或过旧 — 请确认 Institute One 已启动。");
			} else {
				this.status(`失败 — ${errMsg(e).slice(0, 200)}`);
			}
			this.append(`\n[提问失败：${errMsg(e)}]\n`, "stderr");
			if (e instanceof ApiError && e.status >= 500) {
				new Notice(`Institute: 提问失败 — ${errMsg(e)}`, 8000);
			}
		}
	}
}
