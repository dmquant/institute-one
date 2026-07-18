import { ItemView, Notice, TFile, TFolder, WorkspaceLeaf, normalizePath } from "obsidian";
import backlog from "../../roadmap/backlog.json";
import {
	RoadmapDecisionRow,
	RoadmapGate,
	RoadmapLiveCard,
	RoadmapSessionRow,
	errMsg,
} from "./api";
import type InstituteOnePlugin from "./main";
import { PickModal, PromptModal } from "./modals";

export const VIEW_TYPE_ROADMAP = "institute-roadmap";

type RoadmapStatus =
	| "inbox"
	| "ready"
	| "in_progress"
	| "review"
	| "verify"
	| "done"
	| "parked";
type RoadmapPriority = "P0" | "P1" | "P2" | "P3";
type RoadmapRisk = "low" | "medium" | "high";
type RoadmapType = "docs" | "feature" | "schema" | "test" | "ui" | "workflow" | "ops" | "decision";

interface RoadmapCard {
	id: string;
	title: string;
	type: RoadmapType;
	phase: string;
	status: RoadmapStatus;
	priority: RoadmapPriority;
	risk: RoadmapRisk;
	summary: string;
	design_links: string[];
	expected_files: string[];
	dependencies: string[];
	acceptance: string[];
	verification: string[];
}

interface RoadmapBacklog {
	version: number;
	columns: RoadmapStatus[];
	phases: string[];
	cards: RoadmapCard[];
}

type RoadmapSession = RoadmapSessionRow;

const ROADMAP = backlog as unknown as RoadmapBacklog;
const STATUS_ZH: Record<RoadmapStatus, string> = {
	inbox: "收件箱",
	ready: "就绪",
	in_progress: "进行中",
	review: "评审",
	verify: "验证",
	done: "完成",
	parked: "暂停",
};
const STATUS_EN: Record<RoadmapStatus, string> = {
	inbox: "Inbox",
	ready: "Ready",
	in_progress: "In Progress",
	review: "Review",
	verify: "Verify",
	done: "Done",
	parked: "Parked",
};
const ACTIVE_STATUSES = new Set<RoadmapStatus>(["in_progress", "review", "verify"]);

export class RoadmapView extends ItemView {
	private plugin: InstituteOnePlugin;
	private query = "";
	private phase = "all";
	private priority = "all";
	private status = "all";
	private type = "all";
	private selectedId = ROADMAP.cards[0]?.id ?? "";
	private liveStatuses = new Map<string, RoadmapStatus>();
	private liveCards = new Map<string, RoadmapLiveCard>();
	private backendReady = false;
	/** Bumped by every refresh AND every successful/failed move: an in-flight
	 * GET whose generation is stale must be dropped, or its old snapshot would
	 * roll the board back over a move that already succeeded. */
	private roadmapGen = 0;
	private sessions: RoadmapSession[] = [];
	private sessionsCardId = "";
	private sessionsLoading = false;
	private sessionsError = "";
	/** Same idea for the sessions panel: last STARTED load wins, not last response. */
	private sessionsGen = 0;
	private livePrompt: { cardId: string; text: string } | null = null;
	// process strip (M7-006): live sessions/decisions/gates beyond the board.
	// Section fetch failures keep last-known-good data and surface an error
	// marker instead of masquerading as "no data".
	private activeSessions: RoadmapSession[] = [];
	private openDecisions: RoadmapDecisionRow[] = [];
	private liveGates: RoadmapGate[] = [];
	private processErrors: { sessions?: string; decisions?: string; gates?: string } = {};
	/** Last-started-wins for light process refreshes; a full refresh also
	 * bumps this so an in-flight light snapshot cannot overwrite it. */
	private processGen = 0;

	private summaryEl!: HTMLElement;
	private processEl!: HTMLElement;
	private boardEl!: HTMLElement;
	private detailEl!: HTMLElement;
	private gatesEl!: HTMLElement;
	private sessionsEl!: HTMLElement;

	constructor(leaf: WorkspaceLeaf, plugin: InstituteOnePlugin) {
		super(leaf);
		this.plugin = plugin;
		this.navigation = false;
	}

	getViewType(): string {
		return VIEW_TYPE_ROADMAP;
	}

	getDisplayText(): string {
		return "Institute 路线图";
	}

	getIcon(): string {
		return "columns-3";
	}

	async onOpen(): Promise<void> {
		ensureRoadmapStyles();
		const root = this.contentEl;
		root.empty();
		root.addClass("institute-roadmap");

		const head = root.createDiv({ cls: "ir-head" });
		const title = head.createDiv();
		title.createEl("h2", { text: "路线图执行" });
		title.createDiv({
			cls: "ir-subtitle",
			text: `roadmap/backlog.json v${ROADMAP.version} · Obsidian Kanban-compatible`,
		});
		const actions = head.createDiv({ cls: "ir-actions" });
		this.button(actions, "刷新", "从后端刷新路线图与 Coding Sessions", () =>
			void this.refreshLiveRoadmap(true),
		);
		this.button(actions, "导出 Kanban 笔记", "写入 Obsidian Kanban 兼容 Markdown", () =>
			void this.exportKanbanNote(),
		);

		this.summaryEl = root.createDiv({ cls: "ir-summary" });
		this.processEl = root.createDiv({ cls: "ir-process" });
		this.buildFilters(root);
		this.boardEl = root.createDiv({ cls: "ir-board" });
		this.detailEl = root.createDiv({ cls: "ir-detail" });
		this.gatesEl = root.createDiv({ cls: "ir-gates" });
		this.render();
		void this.refreshLiveRoadmap(false);
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	private buildFilters(root: HTMLElement): void {
		const filters = root.createDiv({ cls: "ir-filters" });
		const search = filters.createEl("input");
		search.type = "search";
		search.placeholder = "搜索卡片、文件、验收项...";
		search.addEventListener("input", () => {
			this.query = search.value.trim().toLowerCase();
			this.renderBoard();
		});
		this.select(filters, "阶段", ["all", ...ROADMAP.phases], (v) => {
			this.phase = v;
			this.renderBoard();
		});
		this.select(filters, "状态", ["all", ...ROADMAP.columns], (v) => {
			this.status = v;
			this.renderBoard();
		});
		this.select(filters, "优先级", ["all", "P0", "P1", "P2", "P3"], (v) => {
			this.priority = v;
			this.renderBoard();
		});
		this.select(filters, "类型", ["all", ...unique(ROADMAP.cards.map((c) => c.type))], (v) => {
			this.type = v;
			this.renderBoard();
		});
	}

	private render(): void {
		this.renderSummary();
		this.renderProcess();
		this.renderBoard();
		this.renderDetail();
		this.renderGates();
	}

	private renderSummary(): void {
		const cards = this.cards();
		const byId = mapById(cards);
		const done = cards.filter((c) => c.status === "done").length;
		const active = cards.filter((c) => ACTIVE_STATUSES.has(c.status)).length;
		const ready = cards.filter((c) => c.status === "ready" && !isBlocked(c, byId)).length;
		const blocked = cards.filter((c) => isBlocked(c, byId)).length;
		const completion = cards.length ? Math.round((done / cards.length) * 100) : 0;

		this.summaryEl.empty();
		this.stat(this.summaryEl, String(cards.length), "总卡片");
		this.stat(this.summaryEl, `${completion}%`, "完成度");
		this.stat(this.summaryEl, String(active), "活跃");
		this.stat(this.summaryEl, String(ready), "可开工");
		this.stat(this.summaryEl, String(blocked), "依赖阻塞");
	}

	/** M7-006: the global coding process at a glance — active sessions, open
	 * decisions, and blocked cards, visible without opening any card. */
	private renderProcess(): void {
		this.processEl.empty();
		if (!this.backendReady) {
			this.processEl.hide();
			return;
		}
		this.processEl.show();

		const cards = this.cards();
		const byId = mapById(cards);
		const blocked = cards
			.map((card) => {
				const live = this.liveCards.get(card.id);
				const reason = live?.blocked_reason
					? live.blocked_reason
					: isBlocked(card, byId) && card.status !== "done"
						? "依赖未完成"
						: "";
				return { card, reason };
			})
			.filter((b) => b.reason && b.card.status !== "done" && b.card.status !== "parked");

		const sessionsBox = this.processEl.createDiv({ cls: "ir-process-box" });
		sessionsBox.createEl("h4", {
			text: `活跃 Coding Sessions (${this.activeSessions.length})${this.processErrors.sessions ? " · 刷新失败，显示旧数据" : ""}`,
		});
		if (this.processErrors.sessions) {
			sessionsBox.createDiv({ cls: "ir-warning", text: this.processErrors.sessions });
		}
		if (!this.activeSessions.length && !this.processErrors.sessions) {
			sessionsBox.createDiv({ cls: "ir-empty", text: "没有进行中的 Session。" });
		}
		for (const session of this.activeSessions.slice(0, 6)) {
			const row = sessionsBox.createDiv({ cls: "ir-process-row" });
			row.createSpan({ cls: "ir-id", text: session.card_id });
			row.createSpan({ text: `${session.actor} · ${session.goal}` });
			row.addEventListener("click", () => this.selectCard(session.card_id));
		}

		const decisionsBox = this.processEl.createDiv({ cls: "ir-process-box" });
		decisionsBox.createEl("h4", {
			text: `待决策 (${this.openDecisions.length})${this.processErrors.decisions ? " · 刷新失败，显示旧数据" : ""}`,
		});
		if (this.processErrors.decisions) {
			decisionsBox.createDiv({ cls: "ir-warning", text: this.processErrors.decisions });
		}
		if (!this.openDecisions.length && !this.processErrors.decisions) {
			decisionsBox.createDiv({ cls: "ir-empty", text: "没有待决策项。" });
		}
		for (const decision of this.openDecisions.slice(0, 6)) {
			const row = decisionsBox.createDiv({ cls: "ir-process-row" });
			if (decision.card_id) row.createSpan({ cls: "ir-id", text: decision.card_id });
			row.createSpan({ text: `${decision.title} — ${decision.question}` });
			const resolve = row.createEl("button", { text: "裁决" });
			resolve.addEventListener("click", (ev) => {
				ev.stopPropagation();
				this.openResolveDecision(decision);
			});
		}

		const blockedBox = this.processEl.createDiv({ cls: "ir-process-box" });
		blockedBox.createEl("h4", { text: `阻塞卡片 (${blocked.length})` });
		if (!blocked.length) {
			blockedBox.createDiv({ cls: "ir-empty", text: "没有阻塞项。" });
		}
		for (const item of blocked.slice(0, 6)) {
			const row = blockedBox.createDiv({ cls: "ir-process-row" });
			row.createSpan({ cls: "ir-id", text: item.card.id });
			row.createSpan({ text: item.reason });
			row.addEventListener("click", () => this.selectCard(item.card.id));
		}
	}

	private selectCard(cardId: string): void {
		if (!this.cards().some((c) => c.id === cardId)) return;
		this.selectedId = cardId;
		this.renderBoard();
		this.renderDetail();
	}

	private openResolveDecision(decision: RoadmapDecisionRow): void {
		new PromptModal(
			this.app,
			`裁决：${decision.title}`,
			decision.options.length ? `可选：${decision.options.join(" / ")}` : "输入裁决内容",
			(text) => void this.resolveDecision(decision, text),
		).open();
	}

	private async resolveDecision(decision: RoadmapDecisionRow, text: string): Promise<void> {
		try {
			await this.plugin.api.resolveRoadmapDecision(decision.id, text);
			new Notice(`Institute Roadmap: 决策已裁决 — ${decision.title}`, 5000);
			await this.refreshLiveRoadmap(false);
		} catch (e) {
			new Notice(`Institute Roadmap: 无法裁决 — ${errMsg(e)}`, 8000);
		}
	}

	private renderBoard(): void {
		const cards = this.filteredCards();
		const byId = mapById(this.cards());
		this.boardEl.empty();
		for (const status of ROADMAP.columns) {
			const laneCards = cards.filter((c) => c.status === status);
			const col = this.boardEl.createDiv({ cls: "ir-column" });
			col.addEventListener("dragover", (ev) => ev.preventDefault());
			col.addEventListener("drop", (ev) => {
				ev.preventDefault();
				const id = ev.dataTransfer?.getData("text/plain") ?? "";
				const card = this.cards().find((c) => c.id === id);
				if (card) void this.moveCard(card, status);
			});

			const h = col.createDiv({ cls: "ir-column-head" });
			h.createSpan({ text: STATUS_ZH[status] });
			h.createSpan({ cls: "ir-count", text: String(laneCards.length) });

			if (!laneCards.length) {
				col.createDiv({ cls: "ir-empty", text: "无卡片" });
				continue;
			}

			for (const card of laneCards.sort(cardSort)) {
				const blocked = isBlocked(card, byId);
				const el = col.createDiv({
					cls: `ir-card status-${card.status}${blocked ? " is-blocked" : ""}${this.selectedId === card.id ? " is-selected" : ""}`,
				});
				el.draggable = true;
				el.addEventListener("dragstart", (ev) => {
					ev.dataTransfer?.setData("text/plain", card.id);
				});
				el.addEventListener("click", () => {
					this.selectedId = card.id;
					this.renderBoard();
					this.renderDetail();
				});

				const meta = el.createDiv({ cls: "ir-card-meta" });
				meta.createSpan({ cls: "ir-id", text: card.id });
				meta.createSpan({ cls: `ir-pill priority-${card.priority.toLowerCase()}`, text: card.priority });
				meta.createSpan({ cls: "ir-pill", text: card.type });
				if (blocked) meta.createSpan({ cls: "ir-pill blocked", text: "blocked" });

				el.createDiv({ cls: "ir-card-title", text: card.title });
				el.createDiv({ cls: "ir-card-summary", text: card.summary });
				const foot = el.createDiv({ cls: "ir-card-foot" });
				foot.createSpan({ text: card.phase.split(" ")[0] });
				foot.createSpan({ text: `${card.acceptance.length} checks` });
				foot.createSpan({ text: `${card.verification.length} verify` });
			}
		}
	}

	private renderDetail(): void {
		const cards = this.cards();
		const byId = mapById(cards);
		const card = cards.find((c) => c.id === this.selectedId) ?? cards[0];
		this.detailEl.empty();
		if (!card) {
			this.detailEl.createDiv({ cls: "ir-empty", text: "没有路线图卡片。" });
			return;
		}
		this.selectedId = card.id;
		const blocked = isBlocked(card, byId);

		const top = this.detailEl.createDiv({ cls: "ir-detail-head" });
		const title = top.createDiv();
		title.createEl("h3", { text: `${card.id} · ${card.title}` });
		title.createDiv({ cls: "ir-subtitle", text: `${card.phase} · ${card.type} · ${card.priority} · risk ${card.risk}` });
		const moves = top.createDiv({ cls: "ir-status-actions" });
		for (const status of ROADMAP.columns) {
			const btn = this.button(moves, STATUS_EN[status], `移动到 ${STATUS_ZH[status]}`, () =>
				void this.moveCard(card, status),
			);
			btn.disabled = card.status === status;
		}

		const body = this.detailEl.createDiv({ cls: "ir-detail-grid" });
		const main = body.createDiv();
		this.block(main, "摘要", [card.summary]);
		this.block(main, "验收标准", card.acceptance);
		this.block(main, "验证命令", card.verification, "code");
		this.block(main, "预期文件", card.expected_files, "code");
		this.block(main, "设计链接", card.design_links, "code");

		const side = body.createDiv();
		this.dependencyBlock(side, card, byId);
		this.sessionsEl = side.createDiv();
		this.renderSessionPanel(card);
		if (this.sessionsCardId !== card.id && !this.sessionsLoading) {
			void this.loadSessions(card.id);
		}
		// M7-007: backend prompt (deterministic, from live card state) when
		// available; bundled-seed rendering otherwise. Copy works for both.
		const promptText =
			this.livePrompt && this.livePrompt.cardId === card.id
				? this.livePrompt.text
				: agentPrompt(card);
		const promptBox = side.createDiv({ cls: "ir-block" });
		const promptHead = promptBox.createDiv({ cls: "ir-session-head" });
		promptHead.createEl("h4", {
			text: this.livePrompt?.cardId === card.id ? "执行提示（后端生成）" : "执行提示（本地）",
		});
		this.button(promptHead, "复制", "复制执行提示到剪贴板", () => {
			void navigator.clipboard
				.writeText(promptText)
				.then(() => new Notice("Institute Roadmap: 执行提示已复制。", 4000))
				.catch((e) => new Notice(`Institute Roadmap: 复制失败 — ${errMsg(e)}`, 6000));
		});
		promptBox.createEl("pre", { text: promptText });
		if (blocked) {
			side.createDiv({
				cls: "ir-warning",
				text: "该卡片存在未完成依赖，不能移动到 Done。",
			});
		}
	}

	private renderGates(): void {
		this.gatesEl.empty();
		const gatesLabel = this.liveGates.length ? "Release Gates（后端）" : "Release Gates（本地估算）";
		this.gatesEl.createEl("h3", {
			text: `${gatesLabel}${this.processErrors.gates ? " · 刷新失败，显示旧数据" : ""}`,
		});
		const wrap = this.gatesEl.createDiv({ cls: "ir-gate-grid" });

		if (this.liveGates.length) {
			for (const gate of this.liveGates) {
				const box = wrap.createDiv({ cls: "ir-gate" });
				box.createDiv({ cls: "ir-gate-name", text: gate.name });
				box.createDiv({ cls: "ir-gate-desc", text: gate.description });
				box.createDiv({ cls: "ir-progress" }).createDiv({
					cls: "ir-progress-bar",
					attr: { style: `width: ${gate.pct}%` },
				});
				box.createDiv({
					cls: "ir-gate-meta",
					text: `${gate.done}/${gate.total} done · ${gate.pct}%`,
				});
				// evidence readiness: remaining cards that already carry evidence
				const ready = gate.evidence_ready.length;
				const remaining = gate.remaining.length;
				box.createDiv({
					cls: "ir-gate-meta",
					text: remaining
						? `证据就绪 ${ready}/${remaining} 张未完成卡${ready ? `：${gate.evidence_ready.join(", ")}` : ""}`
						: "全部完成",
				});
			}
			return;
		}

		const cards = this.cards();
		const gates = [
			{ name: "Release A", desc: "Thesis Registry + Forecastable Research", prefixes: ["M0", "M1", "M2", "M3"] },
			{ name: "Release B", desc: "Market Data + Forecast Ledger", prefixes: ["M4", "M5", "M6"] },
			{ name: "Release C", desc: "Roadmap Control Plane", prefixes: ["M7"] },
		];
		for (const gate of gates) {
			const scoped = cards.filter((c) => gate.prefixes.some((p) => c.phase.startsWith(p)));
			const done = scoped.filter((c) => c.status === "done").length;
			const pct = scoped.length ? Math.round((done / scoped.length) * 100) : 0;
			const box = wrap.createDiv({ cls: "ir-gate" });
			box.createDiv({ cls: "ir-gate-name", text: gate.name });
			box.createDiv({ cls: "ir-gate-desc", text: gate.desc });
			box.createDiv({ cls: "ir-progress" }).createDiv({
				cls: "ir-progress-bar",
				attr: { style: `width: ${pct}%` },
			});
			box.createDiv({ cls: "ir-gate-meta", text: `${done}/${scoped.length} cards · ${pct}%` });
		}
	}

	private renderSessionPanel(card: RoadmapCard): void {
		const box = this.sessionsEl.createDiv({ cls: "ir-block ir-sessions" });
		const head = box.createDiv({ cls: "ir-session-head" });
		head.createEl("h4", { text: "Coding Sessions" });
		const actions = head.createDiv({ cls: "ir-actions" });
		this.button(actions, "开始 Session", "以当前卡片预期文件作为计划文件", () =>
			this.openStartSession(card),
		);
		this.button(actions, "刷新", "刷新该卡片的 Coding Sessions", () =>
			void this.loadSessions(card.id),
		);

		box.createDiv({
			cls: `ir-session-source ${this.backendReady ? "live" : "seed"}`,
			text: this.backendReady ? "SQLite live" : "bundled seed / backend unavailable",
		});
		if (this.sessionsLoading && this.sessionsCardId === card.id) {
			box.createDiv({ cls: "ir-empty", text: "正在加载 Sessions…" });
			return;
		}
		if (this.sessionsCardId !== card.id) {
			box.createDiv({ cls: "ir-empty", text: "等待后端数据…" });
			return;
		}
		if (this.sessionsError) {
			box.createDiv({ cls: "ir-warning", text: `Sessions 暂不可用：${this.sessionsError}` });
			return;
		}
		if (!this.sessions.length) {
			box.createDiv({ cls: "ir-empty", text: "尚无 Coding Session。" });
			return;
		}

		for (const session of this.sessions) {
			const row = box.createDiv({ cls: `ir-session status-${session.status}` });
			const meta = row.createDiv({ cls: "ir-session-meta" });
			meta.createSpan({ cls: `ir-pill session-${session.status}`, text: session.status });
			meta.createSpan({ text: session.actor });
			meta.createSpan({ text: formatSessionTime(session.started_at) });
			meta.createSpan({ text: `${session.n_commands ?? 0} commands` });
			row.createDiv({ cls: "ir-session-goal", text: session.goal });
			if (session.planned_files.length) {
				row.createDiv({
					cls: "ir-session-files",
					text: `计划：${session.planned_files.join(", ")}`,
				});
			}
			if (session.touched_files.length) {
				row.createDiv({
					cls: "ir-session-files",
					text: `实际：${session.touched_files.join(", ")}`,
				});
			}
			if (session.summary) {
				row.createDiv({ cls: "ir-session-summary", text: session.summary });
			}
			if (session.status === "active") {
				const buttons = row.createDiv({ cls: "ir-session-actions" });
				this.button(buttons, "记录命令证据", "记录验证命令并附加到卡片证据", () =>
					this.openRecordCommand(card, session),
				);
				this.button(buttons, "完成 Session", "填写总结与实际修改文件", () =>
					this.openCompleteSession(card, session),
				);
			}
		}
	}

	private async fetchProcessSections(): Promise<{
		sessions: RoadmapSession[] | null;
		decisions: RoadmapDecisionRow[] | null;
		gates: RoadmapGate[] | null;
		errors: { sessions?: string; decisions?: string; gates?: string };
	}> {
		const errors: { sessions?: string; decisions?: string; gates?: string } = {};
		const [sessions, decisions, gates] = await Promise.all([
			this.plugin.api.roadmapSessions(undefined, "active").catch((e) => {
				errors.sessions = errMsg(e);
				return null;
			}),
			this.plugin.api.roadmapDecisions("open").catch((e) => {
				errors.decisions = errMsg(e);
				return null;
			}),
			this.plugin.api.roadmapReleaseGates().catch((e) => {
				errors.gates = errMsg(e);
				return null;
			}),
		]);
		return { sessions, decisions, gates, errors };
	}

	private applyProcessSections(
		result: Awaited<ReturnType<RoadmapView["fetchProcessSections"]>>,
	): void {
		// a failed section keeps its last-known-good data; only the error
		// marker changes (a false "没有待决策项" is worse than stale data)
		if (result.sessions !== null) this.activeSessions = result.sessions;
		if (result.decisions !== null) this.openDecisions = result.decisions;
		if (result.gates !== null) this.liveGates = result.gates;
		this.processErrors = result.errors;
	}

	/** Light refresh after a mutation: strip + gates only, no card snapshot. */
	private async refreshProcess(): Promise<void> {
		if (!this.backendReady) return;
		const gen = ++this.processGen;
		const roadmapGenAtStart = this.roadmapGen;
		const result = await this.fetchProcessSections();
		// dropped when a newer light refresh started OR a full refresh ran
		if (gen !== this.processGen || roadmapGenAtStart !== this.roadmapGen) return;
		this.applyProcessSections(result);
		this.renderProcess();
		this.renderGates();
	}

	private async refreshLiveRoadmap(showNotice: boolean): Promise<void> {
		const gen = ++this.roadmapGen;
		const processGenAtStart = this.processGen;
		try {
			const rows = await this.plugin.api.roadmapCards();
			// the process strip is best-effort: its failure must not discard
			// the card snapshot we already hold
			const sections = await this.fetchProcessSections();
			if (gen !== this.roadmapGen) return; // a move or newer refresh superseded this snapshot
			this.liveStatuses.clear();
			this.liveCards.clear();
			for (const row of rows) {
				const status = normalizeStatus(row.status);
				if (status) this.liveStatuses.set(row.id, status);
				this.liveCards.set(row.id, row);
			}
			// sections apply ONLY when no light refresh started after us —
			// last-started-wins holds across full and light snapshots; the
			// bump then drops any light refresh that started before us
			if (processGenAtStart === this.processGen) {
				this.processGen++;
				this.applyProcessSections(sections);
			}
			this.backendReady = rows.length > 0;
			// backend is truth again: offline-only overrides would silently
			// resurface on the NEXT offline session as a forked board state —
			// discard them now, loudly. Persistence failures are their own
			// problem and must not masquerade as "backend unavailable".
			if (rows.length > 0) {
				const overrides = this.plugin.settings.roadmapStatusOverrides ?? {};
				const dropped = Object.keys(overrides).filter(
					(id) => this.liveStatuses.has(id) && overrides[id] !== this.liveStatuses.get(id),
				);
				if (Object.keys(overrides).length) {
					this.plugin.settings.roadmapStatusOverrides = {};
					try {
						await this.plugin.saveSettings();
					} catch (persistErr) {
						new Notice(
							`Institute Roadmap: 无法持久化设置，旧本地状态可能在重启后重现（${errMsg(persistErr)}）。`,
							9000,
						);
					}
				}
				if (dropped.length) {
					new Notice(
						`Institute Roadmap: 后端已恢复，丢弃 ${dropped.length} 个离线本地状态（${dropped.join(", ")}）。如需保留请重新移动卡片。`,
						9000,
					);
				}
			}
			this.sessionsCardId = "";
			this.render();
			if (showNotice) {
				new Notice(
					rows.length
						? `Institute Roadmap: 已同步 ${rows.length} 张后端卡片。`
						: "Institute Roadmap: 后端可达，但路线图尚未导入。",
					5000,
				);
			}
		} catch (e) {
			if (gen !== this.roadmapGen) return;
			this.backendReady = false;
			// stale live statuses must not shadow the local fallback the user
			// is about to write — offline means the live map is meaningless
			this.liveStatuses.clear();
			this.liveCards.clear();
			this.activeSessions = [];
			this.openDecisions = [];
			this.liveGates = [];
			this.processErrors = {};
			if (showNotice) {
				new Notice(`Institute Roadmap: 后端不可用，继续显示 bundled seed（${errMsg(e)}）。`, 7000);
			}
			this.render();
		}
	}

	private async loadSessions(cardId: string): Promise<void> {
		const gen = ++this.sessionsGen;
		this.sessionsLoading = true;
		this.sessionsCardId = cardId;
		this.sessionsError = "";
		if (this.selectedId === cardId) this.renderDetail();
		let sessions: RoadmapSession[] = [];
		let error = "";
		let prompt: { cardId: string; text: string } | null = null;
		try {
			sessions = await this.plugin.api.roadmapSessions(cardId);
			// the live prompt rides along; its absence is not an error
			prompt = await this.plugin.api
				.roadmapAgentPrompt(cardId)
				.then((p) => ({ cardId, text: p.prompt }))
				.catch(() => null);
		} catch (e) {
			error = errMsg(e);
		}
		if (gen !== this.sessionsGen) return; // a newer load owns the panel now
		this.sessions = sessions;
		this.sessionsError = error;
		this.sessionsLoading = false;
		this.livePrompt = prompt;
		if (this.selectedId === cardId) {
			this.renderDetail();
		} else if (this.selectedId) {
			void this.loadSessions(this.selectedId);
		}
	}

	private openStartSession(card: RoadmapCard): void {
		new PromptModal(
			this.app,
			`开始 ${card.id} Coding Session`,
			"本次实现目标",
			(goal) => void this.startSession(card, goal),
		).open();
	}

	private async startSession(card: RoadmapCard, goal: string): Promise<void> {
		try {
			await this.plugin.api.request<RoadmapSession>(
				`/api/roadmap/cards/${encodeURIComponent(card.id)}/sessions`,
				{
					method: "POST",
					body: { actor: "human", goal, planned_files: card.expected_files },
				},
			);
			this.backendReady = true;
			new Notice(`Institute Roadmap: 已开始 ${card.id} Coding Session。`, 5000);
			await this.loadSessions(card.id);
			void this.refreshProcess(); // the strip lists ALL active sessions
		} catch (e) {
			new Notice(`Institute Roadmap: 无法开始 Session — ${errMsg(e)}`, 8000);
		}
	}

	private openCompleteSession(card: RoadmapCard, session: RoadmapSession): void {
		new PromptModal(
			this.app,
			`完成 ${card.id} Session`,
			"实现内容、验证结果、未解决风险",
			(summary) => {
				new PromptModal(
					this.app,
					"记录实际修改文件",
					session.planned_files.join(", ") || "以逗号分隔文件路径",
					(files) => void this.completeSession(card, session, summary, files),
				).open();
			},
		).open();
	}

	private async completeSession(
		card: RoadmapCard,
		session: RoadmapSession,
		summary: string,
		filesText: string,
	): Promise<void> {
		const touchedFiles = unique(
			filesText
				.split(/[\n,;]+/)
				.map((item) => item.trim())
				.filter(Boolean),
		);
		try {
			await this.plugin.api.request<RoadmapSession>(
				`/api/roadmap/sessions/${encodeURIComponent(session.id)}`,
				{
					method: "PATCH",
					body: { status: "completed", summary, touched_files: touchedFiles },
				},
			);
			new Notice(`Institute Roadmap: ${card.id} Session 已完成，可移动到 Review。`, 6000);
			await this.loadSessions(card.id);
			void this.refreshProcess(); // completed sessions leave the strip
		} catch (e) {
			new Notice(`Institute Roadmap: 无法完成 Session — ${errMsg(e)}`, 8000);
		}
	}

	private openRecordCommand(card: RoadmapCard, session: RoadmapSession): void {
		if (!card.verification.length) {
			new Notice(`Institute Roadmap: ${card.id} 没有配置验证命令。`, 5000);
			return;
		}
		new PickModal<string>(
			this.app,
			card.verification,
			(command) => command,
			(command) => {
				new PromptModal(
					this.app,
					"记录命令退出码",
					"0 表示通过；非 0 表示失败",
					(exitCode) => {
						const parsed = Number(exitCode);
						if (!Number.isInteger(parsed)) {
							new Notice("Institute Roadmap: 退出码必须是整数。", 5000);
							return;
						}
						new PromptModal(
							this.app,
							"记录输出摘要",
							"测试数量、失败原因或关键输出",
							(output) => void this.recordCommand(card, session, command, parsed, output),
						).open();
					},
				).open();
			},
			"选择已执行的验证命令",
		).open();
	}

	private async recordCommand(
		card: RoadmapCard,
		session: RoadmapSession,
		command: string,
		exitCode: number,
		output: string,
	): Promise<void> {
		try {
			await this.plugin.api.request(
				`/api/roadmap/sessions/${encodeURIComponent(session.id)}/commands`,
				{
					method: "POST",
					body: {
						command_label: "verification",
						command_text: command,
						exit_code: exitCode,
						output_excerpt: output,
						attach_as_evidence: true,
					},
				},
			);
			new Notice(
				`Institute Roadmap: 命令已记录并附加为 ${exitCode === 0 ? "pass" : "fail"} 证据。`,
				6000,
			);
			await this.loadSessions(card.id);
			void this.refreshProcess(); // evidence changes gate readiness
		} catch (e) {
			new Notice(`Institute Roadmap: 无法记录命令 — ${errMsg(e)}`, 8000);
		}
	}

	private async moveCard(card: RoadmapCard, status: RoadmapStatus): Promise<void> {
		const byId = mapById(this.cards());
		if (status === "done" && isBlocked(card, byId)) {
			new Notice(`Institute Roadmap: ${card.id} 仍有未完成依赖，不能标记 Done。`, 6000);
			return;
		}
		let moved: { status: string };
		try {
			// the try covers ONLY the network call: a later settings-persist
			// failure must not be misread as "backend offline" (which would
			// wrongly clear live state and write a local fork)
			moved = await this.plugin.api.request<{ status: string }>(
				`/api/roadmap/cards/${encodeURIComponent(card.id)}/move`,
				{
					method: "POST",
					body: { status, expected_status: card.status },
				},
			);
		} catch (e) {
			const message = errMsg(e);
			if (/^HTTP 409/.test(message)) {
				// stale expected_status: the board itself is stale — resync
				new Notice(`Institute Roadmap: 卡片状态已变化，正在重新同步 — ${message}`, 8000);
				await this.refreshLiveRoadmap(false);
				return;
			}
			if (/^HTTP \d+/.test(message)) {
				new Notice(`Institute Roadmap: 后端拒绝移动 — ${message}`, 8000);
				return;
			}
			// backend unreachable: the live map is stale by definition — drop it
			// so the local fallback below is actually visible on the board
			this.roadmapGen++;
			this.backendReady = false;
			this.liveStatuses.clear();
			try {
				await this.saveLocalMove(card, status);
				new Notice(`Institute Roadmap: 后端不可用，状态仅保存在本地（${message}）。`, 7000);
			} catch (persistErr) {
				this.render();
				new Notice(
					`Institute Roadmap: 后端不可用且本地状态无法保存（${errMsg(persistErr)}）。`,
					9000,
				);
			}
			return;
		}
		const liveStatus = normalizeStatus(moved.status);
		if (!liveStatus) {
			new Notice(`Institute Roadmap: 后端返回未知状态 ${moved.status}，正在重新同步。`, 8000);
			await this.refreshLiveRoadmap(false);
			return;
		}
		this.roadmapGen++; // invalidate any in-flight GET snapshot taken before this move
		this.liveStatuses.set(card.id, liveStatus);
		this.backendReady = true;
		this.selectedId = card.id;
		const overrides = { ...(this.plugin.settings.roadmapStatusOverrides ?? {}) };
		if (card.id in overrides) {
			delete overrides[card.id];
			this.plugin.settings.roadmapStatusOverrides = overrides;
			try {
				await this.plugin.saveSettings();
			} catch (persistErr) {
				new Notice(
					`Institute Roadmap: 移动已生效，但设置持久化失败（${errMsg(persistErr)}）。`,
					8000,
				);
			}
		}
		this.render();
		void this.refreshProcess(); // a move changes gate progress and blocked lists
	}

	private async saveLocalMove(card: RoadmapCard, status: RoadmapStatus): Promise<void> {
		const overrides = { ...(this.plugin.settings.roadmapStatusOverrides ?? {}) };
		const seed = ROADMAP.cards.find((c) => c.id === card.id);
		if (seed?.status === status) {
			delete overrides[card.id];
		} else {
			overrides[card.id] = status;
		}
		this.plugin.settings.roadmapStatusOverrides = overrides;
		await this.plugin.saveSettings();
		this.selectedId = card.id;
		this.render();
	}

	private cards(): RoadmapCard[] {
		const overrides = this.plugin.settings.roadmapStatusOverrides ?? {};
		const merged = ROADMAP.cards.map((card) => ({
			...card,
			status:
				this.liveStatuses.get(card.id) ??
				normalizeStatus(overrides[card.id]) ??
				card.status,
		}));
		// cards born through the API exist only in the backend: without this
		// merge they would be invisible on the board, in the summary, and in
		// the blocked strip (the bundled backlog is just the offline seed)
		const seedIds = new Set(ROADMAP.cards.map((c) => c.id));
		for (const [id, live] of this.liveCards) {
			if (seedIds.has(id)) continue;
			merged.push({
				id,
				title: live.title,
				type: (live.type as RoadmapType) ?? "feature",
				phase: live.phase ?? "",
				status: this.liveStatuses.get(id) ?? "inbox",
				priority: (live.priority as RoadmapPriority) ?? "P2",
				risk: (live.risk as RoadmapRisk) ?? "medium",
				summary: live.summary ?? "",
				design_links: live.design_links ?? [],
				expected_files: live.expected_files ?? [],
				dependencies: live.dependencies ?? [],
				acceptance: [], // list endpoint carries no checklists; detail panel does
				verification: live.verification ?? [],
			});
		}
		return merged;
	}

	private filteredCards(): RoadmapCard[] {
		return this.cards().filter((card) => {
			if (this.status !== "all" && card.status !== this.status) return false;
			if (this.priority !== "all" && card.priority !== this.priority) return false;
			if (this.phase !== "all" && card.phase !== this.phase) return false;
			if (this.type !== "all" && card.type !== this.type) return false;
			if (!this.query) return true;
			const haystack = [
				card.id,
				card.title,
				card.summary,
				card.phase,
				card.type,
				card.priority,
				card.risk,
				...card.design_links,
				...card.expected_files,
				...card.acceptance,
				...card.verification,
			]
				.join(" ")
				.toLowerCase();
			return haystack.includes(this.query);
		});
	}

	private async exportKanbanNote(): Promise<void> {
		const rel = "Roadmap/Implementation Kanban.md";
		const path = normalizePath(this.plugin.subPath(rel));
		const folder = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
		if (folder) await this.ensureFolder(folder);
		const content = buildKanbanMarkdown(this.cards());
		const existing = this.app.vault.getAbstractFileByPath(path);
		let file: TFile;
		if (existing instanceof TFile) {
			await this.app.vault.modify(existing, content);
			file = existing;
		} else if (existing instanceof TFolder) {
			new Notice(`Institute Roadmap: ${path} 是文件夹，无法写入。`, 8000);
			return;
		} else {
			file = await this.app.vault.create(path, content);
		}
		await this.app.workspace.getLeaf(true).openFile(file);
		new Notice(`Institute Roadmap: 已写入 ${path}`, 6000);
	}

	private async ensureFolder(path: string): Promise<void> {
		let current = "";
		for (const part of normalizePath(path).split("/").filter(Boolean)) {
			current = current ? `${current}/${part}` : part;
			const existing = this.app.vault.getAbstractFileByPath(current);
			if (existing instanceof TFolder) continue;
			if (existing) throw new Error(`${current} exists and is not a folder`);
			await this.app.vault.createFolder(current);
		}
	}

	private stat(parent: HTMLElement, value: string, label: string): void {
		const box = parent.createDiv({ cls: "ir-stat" });
		box.createDiv({ cls: "ir-stat-value", text: value });
		box.createDiv({ cls: "ir-stat-label", text: label });
	}

	private block(parent: HTMLElement, title: string, rows: string[], mode: "text" | "code" | "pre" = "text"): void {
		const box = parent.createDiv({ cls: "ir-block" });
		box.createEl("h4", { text: title });
		if (!rows.length) {
			box.createDiv({ cls: "ir-empty", text: "无" });
			return;
		}
		if (mode === "pre") {
			box.createEl("pre", { text: rows.join("\n\n") });
			return;
		}
		const list = box.createEl("ul");
		for (const row of rows) {
			const li = list.createEl("li");
			if (mode === "code") li.createEl("code", { text: row });
			else li.setText(row);
		}
	}

	private dependencyBlock(parent: HTMLElement, card: RoadmapCard, byId: Map<string, RoadmapCard>): void {
		const box = parent.createDiv({ cls: "ir-block" });
		box.createEl("h4", { text: "依赖" });
		if (!card.dependencies.length) {
			box.createDiv({ cls: "ir-empty", text: "无" });
			return;
		}
		for (const dep of card.dependencies) {
			const target = byId.get(dep);
			const ok = target?.status === "done";
			const row = box.createDiv({ cls: `ir-dep ${ok ? "ok" : "blocked"}` });
			row.createSpan({ text: dep });
			row.createSpan({ text: target ? STATUS_ZH[target.status] : "缺失" });
		}
	}

	private button(parent: HTMLElement, text: string, title: string, onClick: () => void): HTMLButtonElement {
		const btn = parent.createEl("button", { text });
		btn.setAttribute("title", title);
		btn.addEventListener("click", onClick);
		return btn;
	}

	private select(parent: HTMLElement, label: string, values: string[], onChange: (value: string) => void): void {
		const wrap = parent.createDiv({ cls: "ir-select" });
		wrap.createSpan({ text: label });
		const sel = wrap.createEl("select");
		for (const value of values) {
			const text =
				value === "all"
					? "全部"
					: normalizeStatus(value)
						? STATUS_ZH[normalizeStatus(value) as RoadmapStatus]
						: value;
			sel.createEl("option", { text, value });
		}
		sel.addEventListener("change", () => onChange(sel.value));
	}
}

function normalizeStatus(value: string | undefined): RoadmapStatus | null {
	return ROADMAP.columns.includes(value as RoadmapStatus) ? (value as RoadmapStatus) : null;
}

function formatSessionTime(iso: string): string {
	const time = Date.parse(iso);
	if (Number.isNaN(time)) return iso;
	return new Date(time).toLocaleString(undefined, {
		month: "2-digit",
		day: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
	});
}

function mapById(cards: RoadmapCard[]): Map<string, RoadmapCard> {
	return new Map(cards.map((card) => [card.id, card]));
}

function isBlocked(card: RoadmapCard, byId: Map<string, RoadmapCard>): boolean {
	return card.dependencies.some((id) => byId.get(id)?.status !== "done");
}

function cardSort(a: RoadmapCard, b: RoadmapCard): number {
	const phase = ROADMAP.phases.indexOf(a.phase) - ROADMAP.phases.indexOf(b.phase);
	if (phase !== 0) return phase;
	const priority = a.priority.localeCompare(b.priority);
	if (priority !== 0) return priority;
	return a.id.localeCompare(b.id);
}

function unique<T>(items: T[]): T[] {
	return [...new Set(items)];
}

function agentPrompt(card: RoadmapCard): string {
	return [
		`Implement roadmap card ${card.id}: ${card.title}`,
		"",
		`Phase: ${card.phase}`,
		`Summary: ${card.summary}`,
		`Design links: ${card.design_links.join(", ") || "none"}`,
		`Expected files: ${card.expected_files.join(", ") || "none"}`,
		`Dependencies: ${card.dependencies.join(", ") || "none"}`,
		"",
		"Acceptance criteria:",
		...card.acceptance.map((item) => `- ${item}`),
		"",
		"Verification:",
		...card.verification.map((item) => `- ${item}`),
		"",
		"Constraints: local-only infrastructure, no git push, keep design/ ignored, preserve unrelated user changes.",
	].join("\n");
}

function buildKanbanMarkdown(cards: RoadmapCard[]): string {
	const byId = mapById(cards);
	const lines: string[] = [
		"---",
		"kanban-plugin: board",
		"institute-roadmap-source: roadmap/backlog.json",
		`updated: ${new Date().toISOString()}`,
		"---",
		"",
		"# Institute One Implementation Roadmap",
		"",
		"> Generated by the Institute One Obsidian plugin. The native roadmap view is the local control surface; this note is compatible with markdown-backed Kanban plugins.",
		"",
	];
	for (const status of ROADMAP.columns) {
		lines.push(`## ${STATUS_EN[status]}`, "");
		const lane = cards.filter((card) => card.status === status).sort(cardSort);
		if (!lane.length) {
			lines.push("");
			continue;
		}
		for (const card of lane) {
			const checked = status === "done" ? "x" : " ";
			const tags = [
				`#${card.priority.toLowerCase()}`,
				`#${card.type}`,
				`#risk-${card.risk}`,
				`#${card.phase.split(" ")[0].toLowerCase()}`,
			].join(" ");
			lines.push(`- [${checked}] **${card.id}** ${card.title} ${tags}`);
			lines.push(`  - phase: ${card.phase}`);
			if (isBlocked(card, byId)) {
				const deps = card.dependencies
					.filter((id) => byId.get(id)?.status !== "done")
					.join(", ");
				lines.push(`  - blocked by: ${deps}`);
			}
			if (card.verification[0]) lines.push(`  - verify: \`${card.verification[0]}\``);
		}
		lines.push("");
	}
	lines.push(
		"%% kanban:settings",
		'{"kanban-plugin":"board","lane-width":300,"show-checkboxes":true}',
		"%%",
		"",
	);
	return lines.join("\n");
}

function ensureRoadmapStyles(): void {
	if (document.getElementById("institute-roadmap-style")) return;
	const style = document.createElement("style");
	style.id = "institute-roadmap-style";
	style.textContent = `
.institute-roadmap {
	padding: 12px;
	font-size: 13px;
	--ir-border: var(--background-modifier-border);
	--ir-panel: var(--background-secondary);
	--ir-panel-2: var(--background-primary);
	--ir-muted: var(--text-muted);
}
.ir-head, .ir-detail-head {
	display: flex;
	align-items: flex-start;
	justify-content: space-between;
	gap: 12px;
	margin-bottom: 12px;
}
.ir-head h2, .ir-detail h3, .ir-gates h3 {
	margin: 0;
	font-size: 18px;
}
.ir-subtitle {
	color: var(--text-muted);
	font-size: 12px;
	margin-top: 2px;
}
.ir-actions, .ir-status-actions {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
}
.ir-summary {
	display: grid;
	grid-template-columns: repeat(5, minmax(88px, 1fr));
	gap: 8px;
	margin-bottom: 12px;
}
.ir-stat {
	border: 1px solid var(--ir-border);
	background: var(--ir-panel);
	border-radius: 8px;
	padding: 8px 10px;
}
.ir-stat-value {
	font-family: var(--font-monospace);
	font-weight: 700;
	font-size: 18px;
}
.ir-stat-label {
	color: var(--text-muted);
	font-size: 11px;
}
.ir-process {
	display: grid;
	grid-template-columns: repeat(3, minmax(0, 1fr));
	gap: 8px;
	margin-bottom: 12px;
}
.ir-process-box {
	border: 1px solid var(--ir-border);
	background: var(--ir-panel);
	border-radius: 8px;
	padding: 8px 10px;
	min-width: 0;
}
.ir-process-box h4 {
	margin: 0 0 6px;
	font-size: 12px;
	color: var(--text-muted);
}
.ir-process-row {
	display: flex;
	align-items: center;
	gap: 6px;
	padding: 3px 0;
	border-bottom: 1px solid var(--ir-border);
	font-size: 12px;
	cursor: pointer;
	overflow: hidden;
}
.ir-process-row:last-child {
	border-bottom: none;
}
.ir-process-row span:not(.ir-id) {
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
	color: var(--text-muted);
}
.ir-process-row button {
	margin-left: auto;
	font-size: 11px;
	padding: 1px 7px;
	flex-shrink: 0;
}
@media (max-width: 900px) {
	.ir-process {
		grid-template-columns: 1fr;
	}
}
.ir-filters {
	display: flex;
	flex-wrap: wrap;
	gap: 8px;
	align-items: center;
	margin-bottom: 12px;
}
.ir-filters input {
	min-width: min(280px, 100%);
}
.ir-select {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	color: var(--text-muted);
	font-size: 12px;
}
.ir-board {
	display: grid;
	grid-auto-flow: column;
	grid-auto-columns: minmax(250px, 300px);
	gap: 10px;
	overflow-x: auto;
	padding-bottom: 10px;
	margin-bottom: 14px;
}
.ir-column {
	min-height: 220px;
	border: 1px solid var(--ir-border);
	background: var(--ir-panel);
	border-radius: 8px;
	padding: 8px;
}
.ir-column-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	font-weight: 700;
	margin-bottom: 8px;
}
.ir-count {
	color: var(--text-muted);
	font-family: var(--font-monospace);
	font-size: 11px;
}
.ir-card {
	border: 1px solid var(--ir-border);
	border-left: 3px solid var(--color-blue);
	background: var(--ir-panel-2);
	border-radius: 8px;
	padding: 8px;
	margin-bottom: 8px;
	cursor: pointer;
}
.ir-card.is-selected {
	outline: 2px solid var(--text-accent);
}
.ir-card.is-blocked {
	border-left-color: var(--color-orange);
}
.ir-card.status-done {
	border-left-color: var(--color-green);
}
.ir-card.status-parked {
	border-left-color: var(--text-faint);
}
.ir-card-meta, .ir-card-foot {
	display: flex;
	flex-wrap: wrap;
	gap: 5px;
	align-items: center;
	color: var(--text-muted);
	font-size: 11px;
}
.ir-id {
	font-family: var(--font-monospace);
	color: var(--text-normal);
	font-weight: 700;
}
.ir-pill {
	border: 1px solid var(--ir-border);
	border-radius: 999px;
	padding: 0 6px;
	font-family: var(--font-monospace);
}
.ir-pill.priority-p0, .ir-pill.blocked {
	color: var(--color-red);
}
.ir-pill.priority-p1 {
	color: var(--color-orange);
}
.ir-card-title {
	font-weight: 650;
	margin-top: 6px;
	line-height: 1.35;
}
.ir-card-summary {
	color: var(--text-muted);
	font-size: 12px;
	margin: 5px 0 6px;
	display: -webkit-box;
	-webkit-line-clamp: 3;
	-webkit-box-orient: vertical;
	overflow: hidden;
}
.ir-detail, .ir-gates {
	border: 1px solid var(--ir-border);
	background: var(--ir-panel);
	border-radius: 8px;
	padding: 12px;
	margin-bottom: 12px;
}
.ir-detail-grid {
	display: grid;
	grid-template-columns: minmax(0, 1.4fr) minmax(260px, 0.8fr);
	gap: 14px;
}
.ir-block {
	margin-bottom: 12px;
}
.ir-block h4 {
	margin: 0 0 5px;
	font-size: 12px;
	color: var(--text-muted);
}
.ir-block ul {
	margin: 0;
	padding-left: 18px;
}
.ir-block li {
	margin: 3px 0;
}
.ir-block code, .ir-block pre {
	font-family: var(--font-monospace);
	font-size: 12px;
	white-space: pre-wrap;
}
.ir-block pre {
	max-height: 260px;
	overflow: auto;
	border: 1px solid var(--ir-border);
	border-radius: 8px;
	padding: 8px;
	background: var(--background-primary);
}
.ir-session-head, .ir-session-actions {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 6px;
	flex-wrap: wrap;
}
.ir-session-head h4 {
	margin: 0;
}
.ir-session-source {
	margin: 5px 0 7px;
	font-family: var(--font-monospace);
	font-size: 10px;
	color: var(--text-muted);
}
.ir-session-source.live {
	color: var(--color-green);
}
.ir-session {
	border: 1px solid var(--ir-border);
	border-left: 3px solid var(--color-blue);
	border-radius: 7px;
	padding: 7px;
	margin: 6px 0;
	background: var(--background-primary);
}
.ir-session.status-completed {
	border-left-color: var(--color-green);
}
.ir-session.status-blocked, .ir-session.status-partial {
	border-left-color: var(--color-orange);
}
.ir-session.status-cancelled {
	border-left-color: var(--text-faint);
}
.ir-session-meta {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 6px;
	color: var(--text-muted);
	font-size: 10px;
}
.ir-session-goal {
	font-weight: 650;
	margin: 5px 0 3px;
}
.ir-session-files, .ir-session-summary {
	color: var(--text-muted);
	font-size: 11px;
	margin-top: 3px;
	overflow-wrap: anywhere;
}
.ir-session-summary {
	border-top: 1px solid var(--ir-border);
	padding-top: 4px;
	margin-top: 5px;
	color: var(--text-normal);
}
.ir-session-actions {
	justify-content: flex-start;
	margin-top: 6px;
}
.ir-session-actions button, .ir-session-head button {
	font-size: 11px;
	padding: 2px 7px;
}
.ir-dep {
	display: flex;
	justify-content: space-between;
	gap: 10px;
	border-bottom: 1px solid var(--ir-border);
	padding: 3px 0;
	font-family: var(--font-monospace);
}
.ir-dep.ok {
	color: var(--color-green);
}
.ir-dep.blocked, .ir-warning {
	color: var(--color-orange);
}
.ir-warning {
	border: 1px solid rgba(var(--color-orange-rgb), 0.45);
	border-radius: 8px;
	padding: 8px;
}
.ir-gate-grid {
	display: grid;
	grid-template-columns: repeat(3, minmax(0, 1fr));
	gap: 10px;
}
.ir-gate {
	border: 1px solid var(--ir-border);
	border-radius: 8px;
	padding: 9px;
	background: var(--background-primary);
}
.ir-gate-name {
	font-weight: 700;
}
.ir-gate-desc, .ir-gate-meta, .ir-empty {
	color: var(--text-muted);
	font-size: 12px;
}
.ir-progress {
	height: 6px;
	background: var(--background-modifier-border);
	border-radius: 999px;
	margin: 8px 0;
	overflow: hidden;
}
.ir-progress-bar {
	height: 100%;
	background: var(--color-green);
}
@media (max-width: 900px) {
	.ir-summary, .ir-gate-grid, .ir-detail-grid {
		grid-template-columns: 1fr;
	}
}
`;
	document.head.appendChild(style);
}
