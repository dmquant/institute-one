import { ItemView, Notice, WorkspaceLeaf } from "obsidian";
import {
	DailyStatus,
	EventRow,
	ForecastRow,
	HandStatus,
	MetaResult,
	NavRow,
	OperatorAction,
	OperatorActionsResult,
	PaperPositionRow,
	ResearchTreeDetail,
	ResearchTreeNode,
	ResearchTreeRow,
	TaskRow,
	TriageResult,
	errMsg,
	fmtClock,
	fmtCountdown,
	fmtElapsed,
	isMissingEndpoint,
	sgtDate,
} from "./api";
import type InstituteOnePlugin from "./main";

export const VIEW_TYPE_DASHBOARD = "institute-dashboard";

// Human-readable zh labels. Events whose type is not listed here are not
// rendered in the feed (e.g. per-step task.queued/task.running noise).
const EVENT_LABELS: Record<string, string> = {
	"task.completed": "任务完成",
	"task.failed": "任务失败",
	"task.cancelled": "任务取消",
	"research.queued": "研究排队",
	"research.completed": "深度研究完成",
	"research.failed": "深度研究失败",
	"research.followups": "研究跟进",
	"whiteboard.board_opened": "白板开题",
	"whiteboard.board_completed": "白板完成",
	"analyst_daily.completed": "分析师日报",
	"analyst_daily.failed": "分析师日报失败",
	"analyst_daily.sweep_completed": "日报全员完成",
	"mailbox.reply": "信箱回复",
	"topic_pool.added": "议题入池",
	"vault.conflict": "⚠️ 仓库冲突",
	"workflow.completed": "工作流完成",
	"workflow.failed": "工作流失败",
	"workflow.cancelled": "工作流取消",
};

// Server-side prefix filter matching the label table above (keeps polling cheap).
const EVENT_TYPES_FILTER = [
	"task.completed",
	"task.failed",
	"task.cancelled",
	"research.",
	"whiteboard.board_opened",
	"whiteboard.board_completed",
	"analyst_daily.",
	"mailbox.reply",
	"topic_pool.added",
	"vault.conflict",
	"workflow.completed",
	"workflow.failed",
	"workflow.cancelled",
].join(",");

const MAX_EVENTS_KEPT = 20;
const DAILY_STATUS_ZH: Record<string, string> = {
	completed: "已完成",
	failed: "失败",
	pending: "待运行",
};
const FORECAST_DIRECTION_ZH: Record<string, string> = {
	long: "看多",
	short: "看空",
	neutral: "中性",
};
const FORECAST_STATUS_ZH: Record<string, string> = {
	open: "待结算",
	settled: "已结算",
	invalid: "无效",
};
const FORECAST_VERDICT_ZH: Record<string, string> = {
	hit: "命中",
	miss: "落空",
	partial: "部分命中",
	invalid: "无效",
};
const TREE_STATUS_ZH: Record<string, string> = {
	pending: "待探索",
	exploring: "探索中",
	completed: "已完成",
	stopped: "已停止",
	failed: "失败",
};
const TREE_NODE_STATUS_ZH: Record<string, string> = {
	pending: "待处理",
	running: "运行中",
	completed: "已完成",
	failed: "失败",
	pruned: "已剪枝",
};

export class InstituteDashboardView extends ItemView {
	private plugin: InstituteOnePlugin;
	private refreshing = false;
	/** details sections expanded at least once — collapsed ones are not fetched
	 * before their first expand (see watchExtra) */
	private extrasOpened = new Set<string>();

	// events cursor (durable ids from the backend events table)
	private cursor = 0;
	private bootstrapped = false;
	private events: EventRow[] = []; // newest first, capped at MAX_EVENTS_KEPT

	private bannerEl!: HTMLElement;
	private headerEl!: HTMLElement;
	private queueEl!: HTMLElement;
	private handsEl!: HTMLElement;
	private dailyEl!: HTMLElement;
	private runningEl!: HTMLElement;
	private eventsEl!: HTMLElement;
	// collapsible extras (hidden entirely when the backend lacks the endpoints)
	private inboxWrapEl!: HTMLDetailsElement;
	private inboxSummaryEl!: HTMLElement;
	private inboxBodyEl!: HTMLElement;
	private forecastsWrapEl!: HTMLDetailsElement;
	private forecastsSummaryEl!: HTMLElement;
	private forecastsBodyEl!: HTMLElement;
	private treesWrapEl!: HTMLDetailsElement;
	private treesSummaryEl!: HTMLElement;
	private treesBodyEl!: HTMLElement;
	private triageWrapEl!: HTMLDetailsElement;
	private triageSummaryEl!: HTMLElement;
	private triageBodyEl!: HTMLElement;
	private bookWrapEl!: HTMLDetailsElement;
	private bookSummaryEl!: HTMLElement;
	private bookBodyEl!: HTMLElement;

	constructor(leaf: WorkspaceLeaf, plugin: InstituteOnePlugin) {
		super(leaf);
		this.plugin = plugin;
		this.navigation = false;
	}

	getViewType(): string {
		return VIEW_TYPE_DASHBOARD;
	}

	getDisplayText(): string {
		return "Institute 仪表盘";
	}

	getIcon(): string {
		return "gauge";
	}

	async onOpen(): Promise<void> {
		const root = this.contentEl;
		root.empty();
		root.style.padding = "8px 12px";
		root.style.fontSize = "13px";

		this.bannerEl = root.createDiv();
		this.bannerEl.style.display = "none";
		this.bannerEl.style.padding = "6px 10px";
		this.bannerEl.style.marginBottom = "8px";
		this.bannerEl.style.borderRadius = "6px";
		this.bannerEl.style.background = "rgba(var(--color-red-rgb), 0.15)";
		this.bannerEl.style.color = "var(--text-error)";
		this.bannerEl.style.fontWeight = "600";

		this.headerEl = root.createDiv();
		this.headerEl.style.marginBottom = "10px";
		this.headerEl.setText("状态：连接中…");

		this.queueEl = this.section(root, "队列");
		this.handsEl = this.section(root, "执行手");
		this.dailyEl = this.section(root, "今日日报");
		this.runningEl = this.section(root, "进行中");

		[this.inboxWrapEl, this.inboxSummaryEl, this.inboxBodyEl] =
			this.collapsible(root, "Operator 收件箱");
		[this.forecastsWrapEl, this.forecastsSummaryEl, this.forecastsBodyEl] =
			this.collapsible(root, "预测账本速览");
		[this.treesWrapEl, this.treesSummaryEl, this.treesBodyEl] =
			this.collapsible(root, "研究树监控");
		[this.triageWrapEl, this.triageSummaryEl, this.triageBodyEl] =
			this.collapsible(root, "操作台 triage");
		[this.bookWrapEl, this.bookSummaryEl, this.bookBodyEl] =
			this.collapsible(root, "纸面账本");

		// collapsed <details> fetch nothing until their first expand
		this.watchExtra("inbox", this.inboxWrapEl, () => this.refreshOperatorInbox());
		this.watchExtra("forecasts", this.forecastsWrapEl, () => this.refreshForecasts());
		this.watchExtra("trees", this.treesWrapEl, () => this.refreshResearchTrees());
		this.watchExtra("triage", this.triageWrapEl, () => this.refreshTriage());
		this.watchExtra("book", this.bookWrapEl, () => this.refreshBook());

		this.eventsEl = this.section(root, "最近事件");

		// quick actions
		const actions = root.createDiv();
		actions.style.display = "flex";
		actions.style.gap = "8px";
		actions.style.marginTop = "12px";
		actions.style.flexWrap = "wrap";
		this.actionButton(actions, "提问", () => this.plugin.openAskModal());
		this.actionButton(actions, "深度研究", () => this.plugin.openResearchPrompt());
		this.actionButton(actions, "路线图", () => void this.plugin.activateRoadmap());
		this.actionButton(actions, "打开操作台", () => this.plugin.openOperatorUi());

		// auto-poll while visible (isShown() === false skips the fetch);
		// registerInterval is cleared when the view unloads
		void this.refresh(true); // first paint even if the leaf is still attaching
		const everyMs = Math.max(5, this.plugin.settings.pollIntervalS || 10) * 1000;
		this.registerInterval(window.setInterval(() => void this.refresh(), everyMs));
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	// ---- skeleton helpers ----------------------------------------------------

	private section(root: HTMLElement, title: string): HTMLElement {
		const wrap = root.createDiv();
		wrap.style.marginBottom = "12px";
		const h = wrap.createDiv({ text: title });
		h.style.fontWeight = "600";
		h.style.fontSize = "11px";
		h.style.letterSpacing = "0.05em";
		h.style.color = "var(--text-muted)";
		h.style.marginBottom = "4px";
		return wrap.createDiv();
	}

	/**
	 * Collapsible section built ONCE (a native <details> keeps its open state
	 * across refreshes because we only rewrite summary/body contents).
	 * Returns [wrap, summaryText, body]; hide `wrap` when the backend lacks
	 * the endpoint.
	 */
	private collapsible(
		root: HTMLElement,
		title: string,
	): [HTMLDetailsElement, HTMLElement, HTMLElement] {
		const wrap = root.createEl("details");
		wrap.style.marginBottom = "12px";
		const summary = wrap.createEl("summary");
		summary.style.cursor = "pointer";
		summary.style.listStyle = "revert";
		const label = summary.createSpan({ text: title });
		label.style.fontWeight = "600";
		label.style.fontSize = "11px";
		label.style.letterSpacing = "0.05em";
		label.style.color = "var(--text-muted)";
		const summaryText = summary.createSpan({ text: "" });
		summaryText.style.fontSize = "12px";
		summaryText.style.marginLeft = "6px";
		const body = wrap.createDiv();
		body.style.padding = "4px 0 0 12px";
		return [wrap, summaryText, body];
	}

	private actionButton(parent: HTMLElement, text: string, onClick: () => void): void {
		const btn = parent.createEl("button", { text });
		btn.addEventListener("click", onClick);
	}

	private chip(parent: HTMLElement, text: string, color: string, title: string): void {
		const s = parent.createSpan({ text });
		s.style.display = "inline-block";
		s.style.border = "1px solid";
		s.style.borderRadius = "10px";
		s.style.padding = "0 8px";
		s.style.margin = "0 6px 4px 0";
		s.style.fontSize = "12px";
		s.style.color = color;
		s.setAttribute("aria-label", title);
		s.setAttribute("title", title);
	}

	// ---- polling ---------------------------------------------------------------

	/**
	 * Lazy-load a collapsible section: a collapsed <details> is not fetched
	 * until the user expands it the first time; afterwards the regular poll
	 * keeps it fresh (also when re-collapsed).
	 */
	private watchExtra(key: string, wrap: HTMLDetailsElement, load: () => Promise<void>): void {
		wrap.addEventListener("toggle", () => {
			if (wrap.open && !this.extrasOpened.has(key)) {
				this.extrasOpened.add(key);
				void load();
			}
		});
	}

	private extraReady(key: string, wrap: HTMLDetailsElement): boolean {
		return wrap.open || this.extrasOpened.has(key);
	}

	private async refresh(force = false): Promise<void> {
		if (this.refreshing) return;
		if (!force && !this.containerEl.isShown()) return; // hidden leaf: skip the network round
		this.refreshing = true;
		try {
			let meta: MetaResult;
			try {
				meta = await this.plugin.getMeta();
			} catch (e) {
				this.bannerEl.style.display = "block";
				this.bannerEl.setText(
					`无法连接后端 ${this.plugin.api.baseUrl()} — ${errMsg(e)}`,
				);
				this.headerEl.setText("状态：离线");
				return;
			}
			this.bannerEl.style.display = "none";
			this.renderHeader(meta);
			this.renderQueue(meta);
			this.renderHands(meta.hands ?? []);

			try {
				await this.plugin.getRoster();
			} catch {
				/* roster is cosmetic here; tolerate */
			}

			const [daily, running, queued] = await Promise.all([
				this.plugin.getDailyStatus().catch(() => null),
				this.plugin.api.listTasks("running").catch(() => [] as TaskRow[]),
				this.plugin.api.listTasks("queued").catch(() => [] as TaskRow[]),
			]);
			this.renderDaily(daily);
			this.renderRunning(running, queued);

			await Promise.all([
				this.refreshOperatorInbox(),
				this.refreshForecasts(),
				this.refreshResearchTrees(),
				this.refreshTriage(),
				this.refreshBook(),
			]);

			try {
				await this.pollEvents();
			} catch {
				/* keep the previous feed on transient errors */
			}
			this.renderEvents();
		} finally {
			this.refreshing = false;
		}
	}

	// ---- Operator 收件箱（裁决仍只在 SPA 中进行） ------------------------------------

	private async refreshOperatorInbox(): Promise<void> {
		if (!this.extraReady("inbox", this.inboxWrapEl)) return;
		let result: OperatorActionsResult;
		try {
			result = await this.plugin.api.operatorActions("open", 1000);
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.inboxWrapEl.style.display = "none";
			} else {
				this.inboxWrapEl.style.display = "";
				this.inboxSummaryEl.setText("（获取失败）");
				this.inboxBodyEl.empty();
				this.muted(this.inboxBodyEl, errMsg(e).slice(0, 120));
			}
			return;
		}
		this.inboxWrapEl.style.display = "";
		this.inboxBodyEl.empty();

		const count = result.count;
		this.inboxSummaryEl.setText(`${count >= 1000 ? "1000+" : count} 待裁决`);
		this.inboxSummaryEl.style.color =
			count > 0 ? "var(--color-orange)" : "var(--text-muted)";

		if (!result.actions.length) {
			this.muted(this.inboxBodyEl, "没有待裁决 action。");
			return;
		}
		for (const action of result.actions.slice(0, 5)) {
			const line = this.inboxBodyEl.createDiv({
				text: `${action.priority > 1 ? "⚠️ " : ""}${action.title}`,
			});
			line.style.padding = "2px 0";
			line.style.color = "var(--text-accent)";
			line.style.cursor = "pointer";
			line.setAttribute("title", "在 Web 操作台中处理");
			line.addEventListener("click", () => this.openOperatorAction(action));
		}
		if (count > 5) {
			this.muted(this.inboxBodyEl, `… 还有 ${count - 5} 条，点击标题前往操作台`);
		}
	}

	private openOperatorAction(action: OperatorAction): void {
		new Notice(`Institute: action #${action.id} 的裁决只在 Web 操作台进行。`, 5000);
		window.open(`${this.plugin.api.baseUrl()}/operator`);
	}

	// ---- 预测账本速览（API 无 stats；聚合近 5 条的 detail verdict） ----------------------

	private async refreshForecasts(): Promise<void> {
		if (!this.extraReady("forecasts", this.forecastsWrapEl)) return;
		let rows: ForecastRow[];
		try {
			const recent = await this.plugin.api.forecasts(5);
			rows = await Promise.all(
				recent.map((row) =>
					row.status === "open" ? Promise.resolve(row) : this.plugin.api.forecast(row.id),
				),
			);
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.forecastsWrapEl.style.display = "none";
			} else {
				this.forecastsWrapEl.style.display = "";
				this.forecastsSummaryEl.setText("（获取失败）");
				this.forecastsBodyEl.empty();
				this.muted(this.forecastsBodyEl, errMsg(e).slice(0, 120));
			}
			return;
		}
		this.forecastsWrapEl.style.display = "";
		this.forecastsBodyEl.empty();

		const evaluated = rows.filter(
			(row) => row.settlement && row.settlement.verdict !== "invalid",
		);
		const hits = evaluated.filter((row) => row.settlement?.verdict === "hit").length;
		const partial = evaluated.filter((row) => row.settlement?.verdict === "partial").length;
		const rate = evaluated.length ? `${Math.round((hits / evaluated.length) * 100)}%` : "—";
		this.forecastsSummaryEl.setText(
			`近 ${rows.length} 条 · 命中率 ${rate}` +
				(evaluated.length ? `（${hits}/${evaluated.length}）` : ""),
		);
		this.forecastsSummaryEl.style.color = "var(--text-muted)";

		if (!rows.length) {
			this.muted(this.forecastsBodyEl, "还没有预测记录。");
			return;
		}
		for (const row of rows) {
			const verdict = row.settlement?.verdict;
			const state = verdict
				? (FORECAST_VERDICT_ZH[verdict] ?? verdict)
				: (FORECAST_STATUS_ZH[row.status] ?? row.status);
			const direction = FORECAST_DIRECTION_ZH[row.direction] ?? row.direction;
			const date = sgtDate(row.made_at) ?? "";
			const line = this.forecastsBodyEl.createDiv({
				text: `${direction} · ${row.claim}${date ? ` · ${date}` : ""} · ${state}`,
			});
			line.style.padding = "1px 0";
			line.setAttribute("title", row.claim);
		}
		const denominator = `口径：近 5 条中的命中/落空/部分命中，invalid 不计`;
		this.muted(
			this.forecastsBodyEl,
			partial > 0 ? `${denominator}；部分命中 ${partial}` : denominator,
		);
	}

	// ---- 研究树监控 ----------------------------------------------------------------

	private async refreshResearchTrees(): Promise<void> {
		if (!this.extraReady("trees", this.treesWrapEl)) return;
		let active: ResearchTreeRow[];
		let activeTruncated = false;
		let detail: ResearchTreeDetail | null;
		let latest: ResearchTreeRow | null;
		try {
			const [pending, exploring] = await Promise.all([
				this.plugin.api.researchTrees("pending", 200),
				this.plugin.api.researchTrees("exploring", 200),
			]);
			active = [...pending, ...exploring].sort((a, b) =>
				b.created_at.localeCompare(a.created_at),
			);
			activeTruncated = pending.length >= 200 || exploring.length >= 200;
			if (active.length) {
				latest = active[0];
			} else {
				const recent = await this.plugin.api.researchTrees(undefined, 1);
				latest = recent[0] ?? null;
			}
			detail = latest ? await this.plugin.api.researchTree(latest.id) : null;
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.treesWrapEl.style.display = "none";
			} else {
				this.treesWrapEl.style.display = "";
				this.treesSummaryEl.setText("（获取失败）");
				this.treesBodyEl.empty();
				this.muted(this.treesBodyEl, errMsg(e).slice(0, 120));
			}
			return;
		}
		this.treesWrapEl.style.display = "";
		this.treesBodyEl.empty();

		this.treesSummaryEl.setText(
			`${active.length}${activeTruncated ? "+" : ""} 活跃` +
				(detail ? ` · 最新 ${TREE_STATUS_ZH[detail.status] ?? detail.status}` : ""),
		);
		this.treesSummaryEl.style.color =
			active.length > 0 ? "var(--color-green)" : "var(--text-muted)";

		if (!latest || !detail) {
			this.muted(this.treesBodyEl, "还没有研究树。");
			return;
		}
		const progress = `${latest.nodes_completed ?? 0}/${latest.nodes_total ?? detail.nodes.length}`;
		const treeLine = this.treesBodyEl.createDiv({
			text: `${detail.root_topic} · 节点 ${progress}`,
		});
		treeLine.style.padding = "1px 0";

		const latestNode = this.latestTreeNode(detail.nodes);
		if (latestNode) {
			const state = TREE_NODE_STATUS_ZH[latestNode.status] ?? latestNode.status;
			const nodeLine = this.treesBodyEl.createDiv({
				text: `最新节点：${latestNode.topic} · ${state}`,
			});
			nodeLine.style.padding = "1px 0";
			nodeLine.style.color =
				latestNode.status === "failed"
					? "var(--color-red)"
					: latestNode.status === "running"
						? "var(--color-green)"
						: "";
		} else {
			this.muted(this.treesBodyEl, "该树尚无节点。");
		}
	}

	private latestTreeNode(nodes: ResearchTreeNode[]): ResearchTreeNode | null {
		let latest: ResearchTreeNode | null = null;
		let latestAt = Number.NEGATIVE_INFINITY;
		for (const node of nodes) {
			const at = Date.parse(node.finished_at ?? node.created_at);
			const rank = Number.isNaN(at) ? 0 : at;
			if (rank >= latestAt) {
				latest = node;
				latestAt = rank;
			}
		}
		return latest;
	}

	// ---- 状态 / 队列 / 执行手 -----------------------------------------------------

	private renderHeader(meta: MetaResult): void {
		this.headerEl.empty();
		const dot = this.headerEl.createSpan({ text: "● " });
		dot.style.color = "var(--color-green)";
		this.headerEl.createSpan({
			text: `Institute One v${meta.version ?? "?"} · ${meta.work_date ?? "?"}`,
		});
		if (meta.vault_configured === false) {
			const warn = this.headerEl.createSpan({ text: "（vault 未配置）" });
			warn.style.color = "var(--color-orange)";
		}
	}

	private renderQueue(meta: MetaResult): void {
		this.queueEl.empty();
		const by = meta.queue?.by_status ?? {};
		const running = by["running"] ?? 0;
		const queued = by["queued"] ?? 0;
		const completed = by["completed"] ?? 0;
		this.queueEl.setText(`运行 ${running} · 排队 ${queued} · 已完成 ${completed}`);
	}

	private renderHands(hands: HandStatus[]): void {
		this.handsEl.empty();
		if (!hands.length) {
			this.muted(this.handsEl, "（无执行手）");
			return;
		}
		const rank = (h: HandStatus): number => {
			if (h.cooldown_until) return 1;
			if (h.degraded) return 2;
			if (h.available) return 0;
			return 3;
		};
		for (const h of [...hands].sort((a, b) => rank(a) - rank(b))) {
			if (h.cooldown_until) {
				this.chip(
					this.handsEl,
					`${h.name} ⏳${fmtCountdown(h.cooldown_until)}`,
					"var(--color-orange)",
					`冷却中：${h.cooldown_reason ?? "rate limit"}`,
				);
			} else if (h.degraded) {
				this.chip(
					this.handsEl,
					h.name,
					"var(--color-red)",
					`已降级（连续失败 ${h.consecutive_failures} 次）`,
				);
			} else if (h.available) {
				this.chip(this.handsEl, h.name, "var(--color-green)", "可用");
			} else {
				this.chip(
					this.handsEl,
					h.name,
					"var(--text-muted)",
					h.installed ? "不可用" : "未安装",
				);
			}
		}
	}

	// ---- 今日日报 -------------------------------------------------------------------

	private renderDaily(status: DailyStatus | null): void {
		this.dailyEl.empty();
		if (!status) {
			this.muted(this.dailyEl, "（无法获取日报状态）");
			return;
		}
		const entries = Object.entries(status.analysts ?? {});
		const done = entries.filter(([, s]) => s === "completed").length;

		const head = this.dailyEl.createDiv();
		head.style.display = "flex";
		head.style.alignItems = "center";
		head.style.gap = "8px";
		head.createSpan({ text: `${done}/${entries.length} 完成` });
		const btn = head.createEl("button", { text: "运行全员日报" });
		btn.style.fontSize = "11px";
		btn.style.padding = "0 6px";
		btn.addEventListener("click", () => void this.runAllDailies());

		const dots = this.dailyEl.createDiv();
		dots.style.marginTop = "4px";
		for (const [id, s] of entries) {
			const a = this.plugin.analystById(id);
			const mark = s === "completed" ? "✓" : s === "failed" ? "✗" : "○";
			const span = dots.createSpan({ text: mark });
			span.style.marginRight = "6px";
			span.style.color =
				s === "completed"
					? "var(--color-green)"
					: s === "failed"
						? "var(--color-red)"
						: "var(--text-muted)";
			const who = a ? `${a.emoji} ${a.name}` : id;
			span.setAttribute("title", `${who} · ${DAILY_STATUS_ZH[s] ?? s}`);
			span.setAttribute("aria-label", `${who} · ${DAILY_STATUS_ZH[s] ?? s}`);
			if (s === "completed") {
				span.style.cursor = "pointer";
				span.addEventListener("click", () =>
					void this.plugin.openAnalystDailyNote(id, status.date),
				);
			}
		}
	}

	private async runAllDailies(): Promise<void> {
		try {
			await this.plugin.api.runAllDailies();
			this.plugin.invalidateStatusCaches();
			new Notice("Institute: 已启动全员日报（后台运行，完成后自动导出）。", 6000);
		} catch (e) {
			new Notice(`Institute: 启动全员日报失败 — ${errMsg(e)}`, 8000);
		}
	}

	// ---- 操作台 triage（Phase 6；旧后端 404 时整块隐藏） -------------------------------

	private async refreshTriage(): Promise<void> {
		if (!this.extraReady("triage", this.triageWrapEl)) return;
		let t: TriageResult;
		try {
			t = await this.plugin.api.triage();
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.triageWrapEl.style.display = "none";
			} else {
				this.triageWrapEl.style.display = "";
				this.triageSummaryEl.setText("（获取失败）");
				this.triageBodyEl.empty();
				this.muted(this.triageBodyEl, errMsg(e).slice(0, 120));
			}
			return;
		}
		this.triageWrapEl.style.display = "";
		this.triageBodyEl.empty();

		const open = t.actions?.open ?? 0;
		const paused = t.maintenance?.paused === true;
		this.triageSummaryEl.setText(
			`${open} 待处理 · ${paused ? "维护中 ⏸" : "运行中"}`,
		);
		this.triageSummaryEl.style.color = paused
			? "var(--color-orange)"
			: open > 0
				? "var(--color-orange)"
				: "var(--text-muted)";

		const line = (text: string, warn = false) => {
			const el = this.triageBodyEl.createDiv({ text });
			el.style.padding = "1px 0";
			if (warn) el.style.color = "var(--color-orange)";
			return el;
		};

		if (paused) {
			line(`维护暂停中 — 排队深度 ${t.maintenance?.drain_depth ?? 0}`, true);
		}
		const byKind = t.actions?.open_by_kind ?? {};
		const kinds = Object.entries(byKind).sort((a, b) => b[1] - a[1]);
		if (kinds.length) {
			line(`待处理 action：${kinds.map(([k, n]) => `${k} ${n}`).join(" · ")}`);
		} else {
			line("没有待处理 action。");
		}
		const failing = t.cron?.failing ?? [];
		if (failing.length) {
			line(`失败的定时任务：${failing.join("、")}`, true);
		}
		const conflicts = t.vault?.conflicts ?? 0;
		if (conflicts > 0) {
			line(`vault 冲突笔记：${conflicts}`, true);
		}
		const switches = Object.entries(t.feature_switches ?? {});
		const off = switches.filter(([, v]) => !v).map(([k]) => k);
		if (off.length) {
			line(`已关闭的开关：${off.join("、")}`);
		}
	}

	// ---- 纸面账本（paper book；旧后端 404 时整块隐藏） -----------------------------------

	private async refreshBook(): Promise<void> {
		if (!this.extraReady("book", this.bookWrapEl)) return;
		let nav: NavRow[];
		let positions: PaperPositionRow[];
		try {
			[nav, positions] = await Promise.all([
				this.plugin.api.bookNav(30),
				this.plugin.api.bookPositions("open"),
			]);
		} catch (e) {
			if (isMissingEndpoint(e)) {
				this.bookWrapEl.style.display = "none";
			} else {
				this.bookWrapEl.style.display = "";
				this.bookSummaryEl.setText("（获取失败）");
				this.bookBodyEl.empty();
				this.muted(this.bookBodyEl, errMsg(e).slice(0, 120));
			}
			return;
		}
		this.bookWrapEl.style.display = "";
		this.bookBodyEl.empty();

		const latest = nav.length ? nav[nav.length - 1] : null;
		const fmtNav = (v: number | null | undefined) =>
			v == null || !Number.isFinite(v) ? "—" : v.toFixed(4);
		this.bookSummaryEl.setText(
			latest
				? `NAV ${fmtNav(latest.nav)} · 持仓 ${positions.length}`
				: `尚无 NAV · 持仓 ${positions.length}`,
		);
		this.bookSummaryEl.style.color =
			latest && latest.nav < 1 ? "var(--color-red)" : "var(--text-muted)";

		const line = (text: string, warn = false) => {
			const el = this.bookBodyEl.createDiv({ text });
			el.style.padding = "1px 0";
			if (warn) el.style.color = "var(--color-orange)";
			return el;
		};

		if (latest) {
			line(
				`${latest.work_date} · NAV ${fmtNav(latest.nav)}` +
					(latest.benchmark_nav != null
						? ` · 基准 ${fmtNav(latest.benchmark_nav)}`
						: "") +
					` · 累计已实现 ${fmtNav(latest.realized_pnl_cum)}`,
			);
			if (latest.n_unpriced > 0) {
				line(`⚠️ ${latest.n_unpriced} 个仓位无法定价（NAV 为部分口径）`, true);
			}
		} else {
			line("MTM 任务尚未写入 NAV 历史。");
		}
		if (positions.length) {
			for (const p of positions.slice(0, 6)) {
				const dir = p.direction === "short" ? "空" : "多";
				line(
					`${dir} ${p.security_id ?? "?"} @ ${p.entry_price} · ${p.entry_date}`,
				);
			}
			if (positions.length > 6) {
				this.muted(this.bookBodyEl, `… 还有 ${positions.length - 6} 个持仓`);
			}
		} else {
			line("当前无未平仓位。");
		}
	}

	// ---- 进行中 --------------------------------------------------------------------

	private renderRunning(running: TaskRow[], queued: TaskRow[]): void {
		this.runningEl.empty();
		const rows = [...running, ...queued];
		if (!rows.length) {
			this.muted(this.runningEl, "（暂无运行中或排队的任务）");
			return;
		}
		for (const t of rows.slice(0, 12)) {
			const line = this.runningEl.createDiv();
			line.style.display = "flex";
			line.style.alignItems = "center";
			line.style.gap = "6px";
			line.style.padding = "1px 0";

			const icon = t.status === "running" ? "▶" : "⏸";
			const hand = t.hand ?? t.requested_hand ?? "?";
			const since = t.status === "running" ? (t.started_at ?? t.created_at) : t.created_at;
			const label = line.createSpan({
				text: `${icon} ${t.source} · ${hand} · ${fmtElapsed(since)}`,
			});
			label.style.flex = "1";
			label.setAttribute("title", `任务 ${t.id}（${t.status}）`);

			const cancel = line.createEl("button", { text: "✕" });
			cancel.style.fontSize = "10px";
			cancel.style.padding = "0 5px";
			cancel.setAttribute("title", "取消任务");
			cancel.addEventListener("click", () => void this.cancelTask(t.id));
		}
		if (rows.length > 12) {
			this.muted(this.runningEl, `… 还有 ${rows.length - 12} 个任务`);
		}
	}

	private async cancelTask(taskId: string): Promise<void> {
		try {
			const res = await this.plugin.api.cancelTask(taskId);
			new Notice(
				res.cancelled
					? `Institute: 已取消任务 ${taskId}。`
					: `Institute: 任务 ${taskId} 无法取消（可能已结束）。`,
				5000,
			);
			this.plugin.invalidateStatusCaches();
			void this.refresh();
		} catch (e) {
			new Notice(`Institute: 取消失败 — ${errMsg(e)}`, 8000);
		}
	}

	// ---- 最近事件 --------------------------------------------------------------------

	private async pollEvents(): Promise<void> {
		if (!this.bootstrapped) {
			// /api/events replays oldest-first from the cursor, so on first load
			// we page forward to the tail and keep only the most recent items.
			for (let i = 0; i < 40; i++) {
				const page = await this.plugin.api.events(this.cursor, 200, EVENT_TYPES_FILTER);
				if (!page.length) break;
				this.cursor = page[page.length - 1].id;
				this.pushEvents(page);
				if (page.length < 200) break;
			}
			this.bootstrapped = true;
			return;
		}
		const page = await this.plugin.api.events(this.cursor, 30, EVENT_TYPES_FILTER);
		if (page.length) {
			this.cursor = page[page.length - 1].id;
			this.pushEvents(page);
		}
	}

	/** Append an ascending page; keep the feed newest-first and capped. */
	private pushEvents(page: EventRow[]): void {
		const interesting = page.filter((e) => EVENT_LABELS[e.type] !== undefined);
		if (!interesting.length) return;
		this.events = [...interesting.reverse(), ...this.events].slice(0, MAX_EVENTS_KEPT);
	}

	private renderEvents(): void {
		this.eventsEl.empty();
		if (!this.events.length) {
			this.muted(this.eventsEl, "（暂无事件）");
			return;
		}
		for (const e of this.events) {
			const line = this.eventsEl.createDiv();
			line.style.display = "flex";
			line.style.alignItems = "baseline";
			line.style.gap = "6px";
			line.style.padding = "1px 0";

			const time = line.createSpan({ text: fmtClock(e.created_at) });
			time.style.color = "var(--text-muted)";
			time.style.fontSize = "11px";

			line.createSpan({ text: EVENT_LABELS[e.type] ?? e.type });

			const detail = this.eventDetail(e);
			if (detail) {
				const d = line.createSpan({
					text: detail.length > 36 ? detail.slice(0, 36) + "…" : detail,
				});
				d.style.color = "var(--text-muted)";
				d.style.flex = "1";
				d.style.overflow = "hidden";
				d.style.whiteSpace = "nowrap";
				d.style.textOverflow = "ellipsis";
				d.setAttribute("title", detail);
			}

			if (this.canOpenNote(e)) {
				const link = line.createSpan({ text: "打开笔记" });
				link.style.color = "var(--text-accent)";
				link.style.cursor = "pointer";
				link.style.fontSize = "11px";
				link.style.whiteSpace = "nowrap";
				link.addEventListener("click", () => void this.openEventNote(e));
			}
		}
	}

	private eventDetail(e: EventRow): string {
		const p = e.payload ?? {};
		if (e.type.startsWith("analyst_daily.") && e.ref_id) {
			const a = this.plugin.analystById(e.ref_id);
			return a ? `${a.emoji} ${a.name}` : e.ref_id;
		}
		const topic = p["topic"] ?? p["subject"];
		if (typeof topic === "string" && topic) return topic;
		return e.ref_id ?? "";
	}

	private canOpenNote(e: EventRow): boolean {
		return (
			e.type === "research.completed" ||
			e.type === "analyst_daily.completed" ||
			e.type === "whiteboard.board_completed"
		);
	}

	private async openEventNote(e: EventRow): Promise<void> {
		const p = e.payload ?? {};
		const date = sgtDate(e.created_at);
		if (e.type === "research.completed") {
			await this.plugin.openResearchNote(String(p["topic"] ?? e.ref_id ?? ""), date);
		} else if (e.type === "analyst_daily.completed") {
			await this.plugin.openAnalystDailyNote(e.ref_id, String(p["date"] ?? date ?? ""));
		} else if (e.type === "whiteboard.board_completed") {
			await this.plugin.openWhiteboardNote(String(p["topic"] ?? e.ref_id ?? ""), date);
		}
	}

	// ---- misc -----------------------------------------------------------------------

	private muted(parent: HTMLElement, text: string): void {
		const el = parent.createDiv({ text });
		el.style.color = "var(--text-muted)";
	}
}
