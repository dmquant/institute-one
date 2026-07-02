import { ItemView, Notice, TFile, TFolder, WorkspaceLeaf, normalizePath } from "obsidian";
import backlog from "../../roadmap/backlog.json";
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

export class RoadmapView extends ItemView {
	private plugin: InstituteOnePlugin;
	private query = "";
	private phase = "all";
	private priority = "all";
	private status = "all";
	private type = "all";
	private selectedId = ROADMAP.cards[0]?.id ?? "";

	private summaryEl!: HTMLElement;
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
		title.createDiv({
			cls: "ir-subtitle",
			text: `roadmap/backlog.json v${ROADMAP.version} · Obsidian Kanban-compatible`,
		});
		const actions = head.createDiv({ cls: "ir-actions" });
		this.button(actions, "刷新", "重新渲染当前路线图", () => this.render());
		this.button(actions, "导出 Kanban 笔记", "写入 Obsidian Kanban 兼容 Markdown", () =>
			void this.exportKanbanNote(),
		);

		this.summaryEl = root.createDiv({ cls: "ir-summary" });
		this.buildFilters(root);
		this.boardEl = root.createDiv({ cls: "ir-board" });
		this.detailEl = root.createDiv({ cls: "ir-detail" });
		this.gatesEl = root.createDiv({ cls: "ir-gates" });
		this.render();
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
		this.block(side, "执行提示", [agentPrompt(card)], "pre");
		if (blocked) {
			side.createDiv({
				cls: "ir-warning",
				text: "该卡片存在未完成依赖，不能移动到 Done。",
			});
		}
	}

	private renderGates(): void {
		const cards = this.cards();
		this.gatesEl.empty();
		this.gatesEl.createEl("h3", { text: "Release Gates" });
		const gates = [
			{ name: "Release A", desc: "Thesis Registry + Forecastable Research", prefixes: ["M0", "M1", "M2", "M3"] },
			{ name: "Release B", desc: "Market Data + Forecast Ledger", prefixes: ["M4", "M5", "M6"] },
			{ name: "Release C", desc: "Roadmap Control Plane", prefixes: ["M7"] },
		];
		const wrap = this.gatesEl.createDiv({ cls: "ir-gate-grid" });
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

	private async moveCard(card: RoadmapCard, status: RoadmapStatus): Promise<void> {
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

	private cards(): RoadmapCard[] {
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
