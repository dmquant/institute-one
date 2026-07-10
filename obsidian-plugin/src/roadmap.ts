import { App, ItemView, Modal, Notice, TFile, TFolder, WorkspaceLeaf, normalizePath } from "obsidian";
import backlog from "../../roadmap/backlog.json";
import { ApiError, RoadmapApiCard, RoadmapReleaseGate, RoadmapSession, errMsg } from "./api";
import type InstituteOnePlugin from "./main";

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
	/** backend ordering (API mode only) */
	sort_order?: number;
	/** operator-set block (API mode only) — the backend rejects forward moves while set */
	blocked_reason?: string | null;
}

interface RoadmapBacklog {
	version: number;
	columns: RoadmapStatus[];
	phases: string[];
	cards: RoadmapCard[];
}

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
const PRIORITY_VALUES = new Set(["P0", "P1", "P2", "P3"]);
// roadmap_coding_sessions statuses (roadmap/06-agent-protocol.md completion states)
const SESSION_STATUS_ZH: Record<string, string> = {
	active: "进行中",
	completed: "已完成",
	partial: "部分完成",
	blocked: "受阻",
	cancelled: "已取消",
};
const SESSION_FINISH_STATUSES = ["completed", "partial", "blocked", "cancelled"];

export class RoadmapView extends ItemView {
	private plugin: InstituteOnePlugin;
	private query = "";
	private queryRaw = "";
	private phase = "all";
	private priority = "all";
	private status = "all";
	private type = "all";
	private selectedId = ROADMAP.cards[0]?.id ?? "";

	/** "api" = backend rows are truth; "offline" = bundled seed + local overrides. */
	private mode: "api" | "offline" = "offline";
	private apiCards: RoadmapCard[] | null = null;
	private apiGates: RoadmapReleaseGate[] | null = null;
	private loading = false;
	private importTried = false;
	/** card ids whose checklists have been hydrated from GET /cards/{id} */
	private detailFetched = new Set<string>();
	/** card ids whose sessions have been hydrated from GET /sessions?card_id= */
	private sessionsFetched = new Set<string>();
	/** null = the fetch failed; the panel renders a retry row instead of 加载中 */
	private sessionsByCard = new Map<string, RoadmapSession[] | null>();

	private subtitleEl!: HTMLElement;
	private summaryEl!: HTMLElement;
	private filtersEl!: HTMLElement;
	private boardEl!: HTMLElement;
	private detailEl!: HTMLElement;
	private gatesEl!: HTMLElement;

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
		this.subtitleEl = title.createDiv({ cls: "ir-subtitle" });
		const actions = head.createDiv({ cls: "ir-actions" });
		this.button(actions, "刷新", "重新从后端加载路线图", () => void this.reload());
		this.button(actions, "导出 Kanban 笔记", "写入 Obsidian Kanban 兼容 Markdown", () =>
			void this.exportKanbanNote(),
		);

		this.summaryEl = root.createDiv({ cls: "ir-summary" });
		this.filtersEl = root.createDiv({ cls: "ir-filters" });
		this.boardEl = root.createDiv({ cls: "ir-board" });
		this.detailEl = root.createDiv({ cls: "ir-detail" });
		this.gatesEl = root.createDiv({ cls: "ir-gates" });
		this.render(); // instant paint from the bundled seed…
		void this.reload(); // …then prefer the live backend
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	/** Probe the backend: render its rows when it has cards, seed it when empty,
	 * and fall back to the bundled backlog + local overrides when unreachable. */
	private async reload(): Promise<void> {
		this.loading = true;
		this.renderSubtitle();
		try {
			let rows = await this.plugin.api.listCards();
			if (!rows.length && !this.importTried) {
				// reachable but empty: seed it from the bundled backlog once
				try {
					const res = await this.plugin.api.importSeed();
					// only a successful import counts — a failure retries on the next 刷新
					this.importTried = true;
					new Notice(
						`Institute Roadmap: 后端为空，已导入种子（新建 ${res.created} / 更新 ${res.updated} / 共 ${res.total}）。`,
						6000,
					);
					rows = await this.plugin.api.listCards();
				} catch (e) {
					new Notice(`Institute Roadmap: 种子导入失败 — ${errMsg(e)}`, 8000);
				}
			}
			if (rows.length) {
				this.apiCards = rows.map(fromApiCard);
				this.mode = "api";
				this.detailFetched.clear();
				this.sessionsFetched.clear();
				this.sessionsByCard.clear();
				this.apiGates = await this.plugin.api.releaseGates().catch(() => null);
			} else {
				this.apiCards = null;
				this.apiGates = null;
				this.mode = "offline";
			}
		} catch {
			this.apiCards = null;
			this.apiGates = null;
			this.mode = "offline";
		}
		this.loading = false;
		this.render();
	}

	private renderFilters(): void {
		const cards = this.cards();
		const phases = this.mode === "api" ? unique(cards.map((c) => c.phase)) : ROADMAP.phases;
		const types = unique(cards.map((c) => c.type));
		if (this.phase !== "all" && !phases.includes(this.phase)) this.phase = "all";
		if (this.type !== "all" && !types.includes(this.type as RoadmapType)) this.type = "all";

		const filters = this.filtersEl;
		filters.empty();
		const search = filters.createEl("input");
		search.type = "search";
		search.placeholder = "搜索卡片、文件、验收项...";
		search.value = this.queryRaw;
		search.addEventListener("input", () => {
			this.queryRaw = search.value;
			this.query = search.value.trim().toLowerCase();
			this.renderBoard();
		});
		this.select(filters, "阶段", ["all", ...phases], this.phase, (v) => {
			this.phase = v;
			this.renderBoard();
		});
		this.select(filters, "状态", ["all", ...ROADMAP.columns], this.status, (v) => {
			this.status = v;
			this.renderBoard();
		});
		this.select(filters, "优先级", ["all", "P0", "P1", "P2", "P3"], this.priority, (v) => {
			this.priority = v;
			this.renderBoard();
		});
		this.select(filters, "类型", ["all", ...types], this.type, (v) => {
			this.type = v;
			this.renderBoard();
		});
	}

	private render(): void {
		this.renderSubtitle();
		this.renderSummary();
		this.renderFilters();
		this.renderBoard();
		this.renderDetail();
		this.renderGates();
	}

	private renderSubtitle(): void {
		this.subtitleEl.toggleClass("offline", this.mode === "offline" && !this.loading);
		if (this.mode === "api") {
			this.subtitleEl.setText(
				`后端实时 · ${this.plugin.api.baseUrl()}/api/roadmap · Obsidian Kanban-compatible`,
			);
		} else if (this.loading) {
			this.subtitleEl.setText(
				`连接后端中… 暂用内置 roadmap/backlog.json v${ROADMAP.version}`,
			);
		} else {
			this.subtitleEl.setText(
				`离线模式 · 内置 roadmap/backlog.json v${ROADMAP.version} · 状态改动仅保存在本地`,
			);
		}
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
		this.hydrateDetail(card);
		this.hydrateSessions(card);
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
		if (this.mode === "api") this.sessionsBlock(side, card);
		this.block(side, "执行提示", [agentPrompt(card)], "pre");
		if (blocked) {
			const reasons: string[] = [];
			if (hasOpenDeps(card, byId)) {
				reasons.push(
					this.mode === "api"
						? "该卡片存在未完成依赖，移动到 Done 会被后端拒绝（可 override 强制）。"
						: "该卡片存在未完成依赖，不能移动到 Done。",
				);
			}
			if (card.blocked_reason) {
				reasons.push(`后端标记阻塞：${card.blocked_reason}（向前移动会被拒绝，可 override 强制）。`);
			}
			side.createDiv({ cls: "ir-warning", text: reasons.join(" ") });
		}
	}

	/** API mode: the list endpoint carries no checklists, so lazily hydrate the
	 * selected card's acceptance + blocked_reason from GET /cards/{id}. The
	 * seed-derived values stay as the fallback when the fetch fails. */
	private hydrateDetail(card: RoadmapCard): void {
		if (this.mode !== "api" || this.detailFetched.has(card.id)) return;
		this.detailFetched.add(card.id);
		void this.plugin.api.getCard(card.id).then(
			(detail) => {
				const target = this.apiCards?.find((c) => c.id === card.id);
				if (!target) return;
				target.acceptance = detail.checklists
					.filter((item) => item.kind === "acceptance")
					.map((item) => item.text);
				target.blocked_reason = detail.blocked_reason;
				if (this.selectedId === card.id) {
					this.renderBoard();
					this.renderDetail();
				}
			},
			() => {
				this.detailFetched.delete(card.id); // keep the seed fallback; retry on reselect
			},
		);
	}

	/** API mode: lazily hydrate the selected card's coding sessions from
	 * GET /api/roadmap/sessions?card_id= (list rows carry n_commands). */
	private hydrateSessions(card: RoadmapCard): void {
		if (this.mode !== "api" || this.sessionsFetched.has(card.id)) return;
		this.sessionsFetched.add(card.id);
		void this.plugin.api.listSessions(card.id).then(
			(rows) => {
				this.sessionsByCard.set(card.id, rows);
				if (this.selectedId === card.id) this.renderDetail();
			},
			() => {
				// keep the fetched flag — a bare re-render must not refetch in a
				// loop; the null marker renders an explicit retry button instead
				this.sessionsByCard.set(card.id, null);
				if (this.selectedId === card.id) this.renderDetail();
			},
		);
	}

	/** Sessions panel (API mode only): the backend refuses moving a card to
	 * Review until a non-cancelled session carries a summary (override escapes). */
	private sessionsBlock(parent: HTMLElement, card: RoadmapCard): void {
		const box = parent.createDiv({ cls: "ir-block" });
		const head = box.createDiv({ cls: "ir-sessions-head" });
		head.createEl("h4", { text: "编码会话" });
		this.button(head, "开始会话", "开始一个编码会话（actor + goal）", () => {
			new SessionStartModal(this.app, (actor, goal) => void this.startSession(card, actor, goal)).open();
		});
		const sessions = this.sessionsByCard.get(card.id);
		if (sessions === undefined) {
			box.createDiv({ cls: "ir-empty", text: "加载中…" });
			return;
		}
		if (sessions === null) {
			box.createDiv({ cls: "ir-empty", text: "会话加载失败。" });
			this.button(box, "重试", "重新加载编码会话", () => this.refreshSessions(card));
			return;
		}
		if (!sessions.length) {
			box.createDiv({ cls: "ir-empty", text: "无会话 — 移动到 Review 需要带总结的会话。" });
			return;
		}
		for (const sess of sessions) {
			const row = box.createDiv({ cls: "ir-session" });
			const meta = row.createDiv({ cls: "ir-card-meta" });
			meta.createSpan({ cls: "ir-id", text: sess.actor });
			meta.createSpan({
				cls: `ir-pill session-${sess.status}`,
				text: SESSION_STATUS_ZH[sess.status] ?? sess.status,
			});
			meta.createSpan({ text: `${sess.n_commands ?? 0} 条命令` });
			row.createDiv({ cls: "ir-session-goal", text: sess.goal });
			row.createDiv({
				cls: `ir-session-summary${sess.summary.trim() ? "" : " is-missing"}`,
				text: sess.summary.trim() || "（尚无总结）",
			});
			if (sess.status === "active") {
				this.button(row, "完成会话", "填写总结并结束会话", () => {
					new SessionFinishModal(this.app, sess, (status, summary) =>
						void this.finishSession(card, sess, status, summary),
					).open();
				});
			}
		}
	}

	private async startSession(card: RoadmapCard, actor: string, goal: string): Promise<void> {
		try {
			await this.plugin.api.createSession(card.id, actor, goal);
			this.refreshSessions(card);
		} catch (e) {
			new Notice(`Institute Roadmap: 开始会话失败 — ${errMsg(e)}`, 8000);
		}
	}

	private async finishSession(
		card: RoadmapCard,
		sess: RoadmapSession,
		status: string,
		summary: string,
	): Promise<void> {
		try {
			await this.plugin.api.updateSession(sess.id, { status, summary });
			this.refreshSessions(card);
		} catch (e) {
			if (e instanceof ApiError && e.status === 409) {
				new Notice(`Institute Roadmap: 会话 ${sess.id} 已被并发结束，正在重新加载。`, 6000);
				this.refreshSessions(card);
			} else {
				new Notice(`Institute Roadmap: 结束会话失败 — ${errMsg(e)}`, 8000);
			}
		}
	}

	private refreshSessions(card: RoadmapCard): void {
		this.sessionsFetched.delete(card.id);
		this.sessionsByCard.delete(card.id);
		this.renderDetail(); // hydrateSessions refetches and re-renders
	}

	private renderGates(): void {
		this.gatesEl.empty();
		this.gatesEl.createEl("h3", { text: "Release Gates" });
		// API mode renders the backend projection (GET /api/roadmap/release-gates);
		// offline recomputes the same gates locally from the seed + overrides.
		const gates =
			this.mode === "api" && this.apiGates ? this.apiGates : localReleaseGates(this.cards());
		const wrap = this.gatesEl.createDiv({ cls: "ir-gate-grid" });
		for (const gate of gates) {
			const box = wrap.createDiv({ cls: "ir-gate" });
			box.createDiv({ cls: "ir-gate-name", text: gate.name });
			box.createDiv({ cls: "ir-gate-desc", text: gate.description });
			box.createDiv({ cls: "ir-progress" }).createDiv({
				cls: "ir-progress-bar",
				attr: { style: `width: ${gate.pct}%` },
			});
			box.createDiv({ cls: "ir-gate-meta", text: `${gate.done}/${gate.total} cards · ${gate.pct}%` });
		}
	}

	private async moveCard(card: RoadmapCard, status: RoadmapStatus): Promise<void> {
		if (card.status === status) return;
		if (this.mode === "api") {
			await this.moveCardRemote(card, status, false);
			return;
		}
		const byId = mapById(this.cards());
		if (status === "done" && isBlocked(card, byId)) {
			new Notice(`Institute Roadmap: ${card.id} 仍有未完成依赖，不能标记 Done。`, 6000);
			return;
		}
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

	/** Persist a move through POST /api/roadmap/cards/{id}/move.
	 * 400 = rule rejection → Notice with an override retry;
	 * 409 = concurrent change (expected_status stale) → reload. */
	private async moveCardRemote(
		card: RoadmapCard,
		status: RoadmapStatus,
		override: boolean,
	): Promise<void> {
		this.selectedId = card.id;
		try {
			await this.plugin.api.moveCard(card.id, status, override, card.status);
			await this.reload();
		} catch (e) {
			if (e instanceof ApiError && e.status === 409) {
				new Notice(`Institute Roadmap: ${card.id} 已被并发修改，正在重新加载。`, 6000);
				await this.reload();
			} else if (e instanceof ApiError && e.status === 400 && !override) {
				this.offerOverride(card, status, errMsg(e));
			} else {
				new Notice(`Institute Roadmap: 移动 ${card.id} 失败 — ${errMsg(e)}`, 8000);
			}
		}
	}

	private offerOverride(card: RoadmapCard, status: RoadmapStatus, detail: string): void {
		new OverrideModal(
			this.app,
			`${card.id} → ${STATUS_EN[status]} 被后端拒绝`,
			detail,
			() => void this.moveCardRemote(card, status, true),
		).open();
	}

	private cards(): RoadmapCard[] {
		if (this.mode === "api" && this.apiCards) return this.apiCards;
		const overrides = this.plugin.settings.roadmapStatusOverrides ?? {};
		return ROADMAP.cards.map((card) => ({
			...card,
			status: normalizeStatus(overrides[card.id]) ?? card.status,
		}));
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
		const source =
			this.mode === "api"
				? `${this.plugin.api.baseUrl()}/api/roadmap`
				: "roadmap/backlog.json (offline)";
		const content = buildKanbanMarkdown(this.cards(), source);
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

	private select(
		parent: HTMLElement,
		label: string,
		values: string[],
		current: string,
		onChange: (value: string) => void,
	): void {
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
		if (values.includes(current)) sel.value = current;
		sel.addEventListener("change", () => onChange(sel.value));
	}
}

/** Project a backend list row onto the view's card shape. The list endpoint
 * carries dependency ids but no checklists, so acceptance starts from the
 * bundled seed and is hydrated from GET /cards/{id} on selection
 * (RoadmapView.hydrateDetail). */
function fromApiCard(row: RoadmapApiCard): RoadmapCard {
	const seed = ROADMAP.cards.find((c) => c.id === row.id);
	return {
		id: row.id,
		title: row.title,
		type: row.type as RoadmapType,
		phase: row.phase,
		status: normalizeStatus(row.status) ?? "inbox",
		priority: (PRIORITY_VALUES.has(row.priority) ? row.priority : "P2") as RoadmapPriority,
		risk: row.risk as RoadmapRisk,
		summary: row.summary ?? "",
		design_links: row.design_links ?? [],
		expected_files: row.expected_files ?? [],
		dependencies: row.dependencies ?? [],
		acceptance: seed?.acceptance ?? [],
		verification: row.verification ?? [],
		sort_order: row.sort_order,
		blocked_reason: row.blocked_reason,
	};
}

/** Move rejection + override confirmation. A Notice auto-dismisses (and hides on
 * any click inside it), losing the override affordance — a Modal keeps it up
 * until the operator decides. */
class OverrideModal extends Modal {
	constructor(
		app: App,
		private heading: string,
		private detail: string,
		private onConfirm: () => void,
	) {
		super(app);
	}

	onOpen(): void {
		this.titleEl.setText(this.heading);
		this.contentEl.createDiv({ text: this.detail });
		const row = this.contentEl.createDiv({ cls: "ir-modal-actions" });
		const cancel = row.createEl("button", { text: "取消" });
		cancel.addEventListener("click", () => this.close());
		const confirm = row.createEl("button", { text: "强制移动（override）", cls: "mod-warning" });
		confirm.addEventListener("click", () => {
			this.close();
			this.onConfirm();
		});
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

/** Start a coding session: actor (who is coding) + goal (one session, one goal). */
class SessionStartModal extends Modal {
	constructor(
		app: App,
		private onSubmit: (actor: string, goal: string) => void,
	) {
		super(app);
	}

	onOpen(): void {
		this.titleEl.setText("开始编码会话");
		const actor = this.contentEl.createEl("input");
		actor.type = "text";
		actor.placeholder = "actor（human / claude / codex / …）";
		actor.value = "human";
		actor.addClass("ir-modal-input");
		const goal = this.contentEl.createEl("input");
		goal.type = "text";
		goal.placeholder = "本次会话的单一目标";
		goal.addClass("ir-modal-input");
		const row = this.contentEl.createDiv({ cls: "ir-modal-actions" });
		const cancel = row.createEl("button", { text: "取消" });
		cancel.addEventListener("click", () => this.close());
		const confirm = row.createEl("button", { text: "开始会话", cls: "mod-cta" });
		confirm.addEventListener("click", () => {
			if (!actor.value.trim() || !goal.value.trim()) {
				new Notice("Institute Roadmap: 会话需要 actor 和 goal。", 6000);
				return;
			}
			this.close();
			this.onSubmit(actor.value.trim(), goal.value.trim());
		});
		goal.focus();
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

/** Finish a session: completion state + the summary the Review gate requires. */
class SessionFinishModal extends Modal {
	constructor(
		app: App,
		private session: RoadmapSession,
		private onSubmit: (status: string, summary: string) => void,
	) {
		super(app);
	}

	onOpen(): void {
		this.titleEl.setText(`结束会话 · ${this.session.actor}`);
		this.contentEl.createDiv({ cls: "ir-subtitle", text: this.session.goal });
		const status = this.contentEl.createEl("select");
		status.addClass("ir-modal-input");
		for (const value of SESSION_FINISH_STATUSES) {
			status.createEl("option", { text: SESSION_STATUS_ZH[value] ?? value, value });
		}
		const summary = this.contentEl.createEl("textarea");
		summary.placeholder = "总结：改了什么、验证结果、遗留风险（Review 移动的前置条件）";
		summary.rows = 5;
		summary.addClass("ir-modal-input");
		summary.value = this.session.summary ?? "";
		const row = this.contentEl.createDiv({ cls: "ir-modal-actions" });
		const cancel = row.createEl("button", { text: "取消" });
		cancel.addEventListener("click", () => this.close());
		const confirm = row.createEl("button", { text: "结束会话", cls: "mod-cta" });
		confirm.addEventListener("click", () => {
			// cancelled = abandoned attempt: it never opens the Review gate, so
			// don't force a summary; every other terminal status documents work
			if (!summary.value.trim() && status.value !== "cancelled") {
				new Notice("Institute Roadmap: 结束会话需要总结（取消除外）。", 6000);
				return;
			}
			this.close();
			this.onSubmit(status.value, summary.value.trim());
		});
		summary.focus();
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

function normalizeStatus(value: string | undefined): RoadmapStatus | null {
	return ROADMAP.columns.includes(value as RoadmapStatus) ? (value as RoadmapStatus) : null;
}

function mapById(cards: RoadmapCard[]): Map<string, RoadmapCard> {
	return new Map(cards.map((card) => [card.id, card]));
}

function isBlocked(card: RoadmapCard, byId: Map<string, RoadmapCard>): boolean {
	return hasOpenDeps(card, byId) || Boolean(card.blocked_reason);
}

function hasOpenDeps(card: RoadmapCard, byId: Map<string, RoadmapCard>): boolean {
	return card.dependencies.some((id) => byId.get(id)?.status !== "done");
}

/** Offline fallback for GET /api/roadmap/release-gates — mirrors
 * app/institute/roadmap.py RELEASE_GATES + _phase_token (exact leading token). */
const RELEASE_GATES = [
	{ name: "Release A", description: "Thesis Registry + Forecastable Research", prefixes: ["M0", "M1", "M2", "M3"] },
	{ name: "Release B", description: "Market Data + Forecast Ledger", prefixes: ["M4", "M5", "M6"] },
	{ name: "Release C", description: "Roadmap Control Plane", prefixes: ["M7"] },
];

function localReleaseGates(cards: RoadmapCard[]): RoadmapReleaseGate[] {
	return RELEASE_GATES.map((gate) => {
		const scoped = cards.filter((c) => gate.prefixes.includes(c.phase.split(" ")[0]));
		const done = scoped.filter((c) => c.status === "done").length;
		return {
			...gate,
			total: scoped.length,
			done,
			pct: scoped.length ? Math.round((done / scoped.length) * 100) : 0,
			status: scoped.length && done === scoped.length ? "met" : "open",
			remaining: scoped.filter((c) => c.status !== "done").map((c) => c.id).sort(),
		};
	});
}

function cardSort(a: RoadmapCard, b: RoadmapCard): number {
	// API mode: the backend's sort_order is the operator-controlled ordering
	if (a.sort_order !== undefined && b.sort_order !== undefined && a.sort_order !== b.sort_order) {
		return a.sort_order - b.sort_order;
	}
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

function buildKanbanMarkdown(cards: RoadmapCard[], source: string): string {
	const byId = mapById(cards);
	const lines: string[] = [
		"---",
		"kanban-plugin: board",
		`institute-roadmap-source: ${source}`,
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
			if (hasOpenDeps(card, byId)) {
				const deps = card.dependencies
					.filter((id) => byId.get(id)?.status !== "done")
					.join(", ");
				lines.push(`  - blocked by: ${deps}`);
			}
			if (card.blocked_reason) lines.push(`  - blocked: ${card.blocked_reason}`);
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
.ir-subtitle.offline {
	color: var(--color-orange);
}
.ir-modal-actions {
	display: flex;
	gap: 8px;
	justify-content: flex-end;
	margin-top: 12px;
}
.ir-modal-input {
	display: block;
	width: 100%;
	margin-top: 8px;
}
.ir-sessions-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 8px;
}
.ir-sessions-head h4 {
	margin: 0 0 5px;
	font-size: 12px;
	color: var(--text-muted);
}
.ir-session {
	border: 1px solid var(--ir-border);
	border-radius: 8px;
	background: var(--ir-panel-2);
	padding: 8px;
	margin-bottom: 8px;
}
.ir-pill.session-active {
	color: var(--color-blue);
}
.ir-pill.session-blocked, .ir-pill.session-cancelled {
	color: var(--color-orange);
}
.ir-pill.session-completed {
	color: var(--color-green);
}
.ir-session-goal {
	font-weight: 650;
	margin-top: 6px;
	line-height: 1.35;
}
.ir-session-summary {
	color: var(--text-muted);
	font-size: 12px;
	margin: 5px 0 6px;
	white-space: pre-wrap;
}
.ir-session-summary.is-missing {
	color: var(--color-orange);
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
