import {
	App,
	Editor,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	TFile,
	TFolder,
	normalizePath,
} from "obsidian";
import {
	Analyst,
	ArchiveHit,
	InstituteApi,
	ResearchQueueItem,
	VaultIndexRow,
	errMsg,
	exportSlug,
	fileSlug,
	isMissingEndpoint,
	researchStatusIcon,
	researchStatusZh,
	sgtDate,
	todayStr,
} from "./api";
import { AskStreamView, VIEW_TYPE_ASK_STREAM } from "./askstream";
import { InstituteDashboardView, VIEW_TYPE_DASHBOARD } from "./dashboard";
import {
	AskModal,
	ClaimCheckModal,
	DigestModal,
	MailModal,
	PickModal,
	PromptModal,
	VaultDoctorModal,
} from "./modals";
import { RoadmapView, VIEW_TYPE_ROADMAP } from "./roadmap";

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface InstituteSettings {
	baseUrl: string;
	/** Optional bearer token matching the backend's INSTITUTE_TOKEN. */
	token: string;
	/** Obsidian-vault subfolder that maps to the backend's vault_dir. */
	vaultSubfolder: string;
	/** Last-used / default analyst id ("" = default hand, no persona). */
	defaultAnalyst: string;
	insertStyle: "callout" | "plain";
	/** Dashboard poll interval in seconds (min 5). */
	pollIntervalS: number;
	/** Offline-only roadmap status overrides — used when the backend roadmap API is unreachable. */
	roadmapStatusOverrides: Record<string, string>;
}

export const DEFAULT_SETTINGS: InstituteSettings = {
	baseUrl: "http://127.0.0.1:8100",
	token: "",
	vaultSubfolder: "Institute",
	defaultAnalyst: "",
	insertStyle: "callout",
	pollIntervalS: 10,
	roadmapStatusOverrides: {},
};

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------

export default class InstituteOnePlugin extends Plugin {
	settings: InstituteSettings = { ...DEFAULT_SETTINGS };
	api!: InstituteApi;

	private statusBar!: HTMLElement;
	private roster: Analyst[] | null = null;
	private rosterAt = 0;

	async onload(): Promise<void> {
		await this.loadSettings();
		this.api = new InstituteApi(
			() => this.settings.baseUrl,
			() => this.settings.token,
		);
		this.addSettingTab(new InstituteSettingTab(this.app, this));

		// ---- dashboard view ------------------------------------------------
		this.registerView(VIEW_TYPE_DASHBOARD, (leaf) => new InstituteDashboardView(leaf, this));
		this.registerView(VIEW_TYPE_ROADMAP, (leaf) => new RoadmapView(leaf, this));
		this.registerView(VIEW_TYPE_ASK_STREAM, (leaf) => new AskStreamView(leaf, this));
		this.addRibbonIcon("gauge", "Institute 仪表盘", () => void this.activateDashboard());
		this.addRibbonIcon("columns-3", "Institute 路线图", () => void this.activateRoadmap());

		// ---- commands ----------------------------------------------------
		this.addCommand({
			id: "open-dashboard",
			name: "Institute: 打开仪表盘",
			callback: () => void this.activateDashboard(),
		});

		this.addCommand({
			id: "open-roadmap",
			name: "Institute: 打开路线图",
			callback: () => void this.activateRoadmap(),
		});

		this.addCommand({
			id: "ask",
			name: "Institute: 提问 (Ask)",
			callback: () => this.openAskModal(),
		});

		this.addCommand({
			id: "ask-stream",
			name: "Institute: 流式问答（侧边栏）",
			callback: () => void this.activateAskStream(),
		});

		this.addCommand({
			id: "claim-check-selection",
			name: "Institute: 查证选中文本",
			editorCallback: (editor) => void this.claimCheckCommand(editor),
		});

		this.addCommand({
			id: "digest-recent-reports",
			name: "Institute: 打开今日简报摘要",
			callback: () => void this.openDigestCommand("reports"),
		});

		this.addCommand({
			id: "digest-analyst-memory",
			name: "Institute: 我的分析师记忆",
			callback: () => void this.openDigestCommand("memory"),
		});

		this.addCommand({
			id: "digest-analyst-disputes",
			name: "Institute: 争议清单",
			callback: () => void this.openDigestCommand("disputes"),
		});

		this.addCommand({
			id: "queue-research",
			name: "Institute: 深度研究",
			callback: () => this.openResearchPrompt(),
		});

		this.addCommand({
			id: "research-queue-status",
			name: "Institute: 研究队列状态",
			callback: () => void this.showResearchQueue(),
		});

		this.addCommand({
			id: "whiteboard-topic",
			name: "Institute: 加入白板议题",
			callback: () => this.openWhiteboardCommand(),
		});

		this.addCommand({
			id: "export-research",
			name: "Institute: 导出研究报告到仓库",
			callback: () => void this.exportResearchCommand(),
		});

		this.addCommand({
			id: "vault-doctor",
			name: "Institute: 仓库体检 (vault doctor)",
			callback: () => void this.vaultDoctorCommand(),
		});

		this.addCommand({
			id: "mailbox-compose",
			name: "Institute: 写信给分析师",
			callback: () => {
				const selection =
					this.app.workspace.activeEditor?.editor?.getSelection() ?? "";
				new MailModal(this, selection).open();
			},
		});

		this.addCommand({
			id: "archive-search",
			name: "Institute: 检索研究档案",
			callback: () => {
				const editor = this.app.workspace.activeEditor?.editor ?? null;
				new PromptModal(this.app, "检索研究档案", "关键词…", (q) =>
					void this.searchArchive(q, editor),
				).open();
			},
		});

		this.addCommand({
			id: "run-analyst-daily",
			name: "Institute: 运行某位分析师日报",
			callback: () => void this.runAnalystDailyCommand(),
		});

		this.addCommand({
			id: "open-operator-ui",
			name: "Institute: 打开操作台 (web)",
			callback: () => this.openOperatorUi(),
		});

		// ---- status bar ---------------------------------------------------
		this.statusBar = this.addStatusBarItem();
		this.statusBar.addClass("mod-clickable");
		this.statusBar.setText("⚙︎ inst: …");
		this.statusBar.onClickEvent(() => void this.activateDashboard());
		void this.refreshStatus();
		this.registerInterval(
			window.setInterval(() => void this.refreshStatus(), 60_000),
		);
	}

	openOperatorUi(): void {
		window.open(this.settings.baseUrl);
	}

	// ---- dashboard ----------------------------------------------------------

	async activateDashboard(): Promise<void> {
		const { workspace } = this.app;
		const existing = workspace.getLeavesOfType(VIEW_TYPE_DASHBOARD);
		if (existing.length > 0) {
			await workspace.revealLeaf(existing[0]);
			return;
		}
		const leaf = workspace.getRightLeaf(false);
		if (!leaf) return;
		await leaf.setViewState({ type: VIEW_TYPE_DASHBOARD, active: true });
		await workspace.revealLeaf(leaf);
	}

	async activateRoadmap(): Promise<void> {
		const { workspace } = this.app;
		const existing = workspace.getLeavesOfType(VIEW_TYPE_ROADMAP);
		if (existing.length > 0) {
			await workspace.revealLeaf(existing[0]);
			return;
		}
		const leaf = workspace.getRightLeaf(false);
		if (!leaf) return;
		await leaf.setViewState({ type: VIEW_TYPE_ROADMAP, active: true });
		await workspace.revealLeaf(leaf);
	}

	async activateAskStream(): Promise<void> {
		const { workspace } = this.app;
		const existing = workspace.getLeavesOfType(VIEW_TYPE_ASK_STREAM);
		if (existing.length > 0) {
			await workspace.revealLeaf(existing[0]);
			return;
		}
		const leaf = workspace.getRightLeaf(false);
		if (!leaf) return;
		await leaf.setViewState({ type: VIEW_TYPE_ASK_STREAM, active: true });
		await workspace.revealLeaf(leaf);
	}

	// ---- 查证选中文本 (claim check, Phase 3) -------------------------------------

	async claimCheckCommand(editor: Editor): Promise<void> {
		const selection = editor.getSelection()?.trim() ?? "";
		const text = selection || paragraphAround(editor);
		if (!text.trim()) {
			new Notice("Institute: 没有选中文本，光标也不在段落内。");
			return;
		}
		const notice = new Notice("Institute: 查证中…", 0);
		try {
			const result = await this.api.claimCheck(text);
			notice.hide();
			new ClaimCheckModal(this.app, text, result).open();
		} catch (e) {
			notice.hide();
			if (isMissingEndpoint(e)) {
				new Notice(
					"Institute: 后端未启用写作时查证（fact-check v2）— 请升级并重启后端。",
					8000,
				);
			} else {
				new Notice(`Institute: 查证失败 — ${errMsg(e)}`, 8000);
			}
		}
	}

	// ---- digest 快捷命令（/api/institute/*.md） ------------------------------------

	async openDigestCommand(kind: "reports" | "memory" | "disputes"): Promise<void> {
		if (kind === "reports") {
			await this.showDigest("近期报告摘要 (recent reports)", () =>
				this.api.digestRecentReports(),
			);
			return;
		}
		// memory / disputes are per-analyst: pick one (default analyst listed first)
		let roster: Analyst[];
		try {
			roster = await this.getRoster();
		} catch (e) {
			new Notice(`Institute: 无法加载分析师名册 — ${errMsg(e)}`, 8000);
			return;
		}
		if (!roster.length) {
			new Notice("Institute: 名册为空。");
			return;
		}
		const preferred = this.settings.defaultAnalyst;
		const items = [...roster].sort((a, b) =>
			a.id === preferred ? -1 : b.id === preferred ? 1 : 0,
		);
		new PickModal(
			this.app,
			items,
			(a) => `${a.emoji} ${a.name}（${a.id}）— ${a.focus}`,
			(a) => {
				if (kind === "memory") {
					void this.showDigest(`分析师记忆 — ${a.name}`, () =>
						this.api.digestAnalystMemory(a.id),
					);
				} else {
					void this.showDigest(`争议清单 — ${a.name}`, () =>
						this.api.digestAnalystDisputes(a.id),
					);
				}
			},
			"选择分析师…",
		).open();
	}

	private async showDigest(title: string, fetchMd: () => Promise<string>): Promise<void> {
		const notice = new Notice("Institute: 获取摘要中…", 0);
		try {
			const md = await fetchMd();
			notice.hide();
			if (!md.trim()) {
				new Notice("Institute: 摘要为空。", 5000);
				return;
			}
			new DigestModal(this.app, title, md).open();
		} catch (e) {
			notice.hide();
			if (isMissingEndpoint(e)) {
				new Notice("Institute: 后端未启用该摘要端点 — 请升级并重启后端。", 8000);
			} else {
				new Notice(`Institute: 获取摘要失败 — ${errMsg(e)}`, 8000);
			}
		}
	}

	// ---- roster cache ----------------------------------------------------------

	async getRoster(maxAgeMs = 10 * 60_000): Promise<Analyst[]> {
		const now = Date.now();
		if (this.roster && now - this.rosterAt < maxAgeMs) return this.roster;
		this.roster = await this.api.analysts();
		this.rosterAt = now;
		return this.roster;
	}

	/** Sync lookup against the cached roster (may miss before the first fetch). */
	analystById(id: string): Analyst | null {
		return this.roster?.find((a) => a.id === id) ?? null;
	}

	// ---- 提问 (Ask) ----------------------------------------------------------------

	openAskModal(): void {
		const editor = this.app.workspace.activeEditor?.editor ?? null;
		const selection = editor?.getSelection() ?? "";
		new AskModal(this, selection, editor).open();
	}

	async runAsk(prompt: string, analyst: Analyst | null, editor: Editor | null): Promise<void> {
		prompt = prompt.trim();
		if (!prompt) {
			new Notice("Institute: 问题为空。");
			return;
		}
		const who = analyst ? `${analyst.emoji} ${analyst.name}` : "Institute";
		const started = Date.now();
		const notice = new Notice(`Institute: ${who} 思考中… 0s`, 0);
		const tick = window.setInterval(() => {
			const s = Math.round((Date.now() - started) / 1000);
			notice.setMessage(`Institute: ${who} 思考中… ${s}s`);
		}, 1000);
		try {
			const task = await this.api.ask(prompt, analyst?.id ?? null);
			const out = (task.output ?? "").trim();
			let text: string;
			if (!out) {
				text = task.error
					? `> [!failure] 任务 ${task.id} ${task.status}\n> ${task.error}`
					: `（任务 ${task.id} ${task.status}：无输出）`;
			} else if (this.settings.insertStyle === "callout") {
				const header = `> [!quote] ${who} · ${todayStr()}`;
				text = header + "\n" + out.split("\n").map((l) => `> ${l}`).join("\n");
			} else {
				text = out;
			}
			if (editor) {
				const cursor = editor.getCursor("to");
				const lineEnd = { line: cursor.line, ch: editor.getLine(cursor.line).length };
				editor.replaceRange(`\n\n${text}\n`, lineEnd);
			} else {
				await this.createAskNote(prompt, analyst, text);
			}
			const s = Math.round((Date.now() - started) / 1000);
			notice.setMessage(`Institute: 完成（任务 ${task.id}，${task.status}，${s}s）。`);
			window.setTimeout(() => notice.hide(), 5000);
		} catch (e) {
			notice.hide();
			new Notice(`Institute: 提问失败 — ${errMsg(e)}`, 8000);
		} finally {
			window.clearInterval(tick);
		}
	}

	private async createAskNote(
		prompt: string,
		analyst: Analyst | null,
		output: string,
	): Promise<void> {
		const folder = "Ask";
		if (!this.app.vault.getAbstractFileByPath(folder)) {
			try {
				await this.app.vault.createFolder(folder);
			} catch {
				/* already exists / race — vault.create below will surface real errors */
			}
		}
		const base = `${todayStr()} ${fileSlug(prompt)}`;
		let path = normalizePath(`${folder}/${base}.md`);
		let n = 1;
		while (this.app.vault.getAbstractFileByPath(path)) {
			path = normalizePath(`${folder}/${base} ${++n}.md`);
		}
		const quoted = prompt.replace(/\n/g, "\n> ");
		const header = analyst
			? `> [!question] 提问（分析师：${analyst.emoji} ${analyst.name}）`
			: "> [!question] 提问";
		const file: TFile = await this.app.vault.create(
			path,
			`${header}\n> ${quoted}\n\n${output}\n`,
		);
		await this.app.workspace.getLeaf(true).openFile(file);
	}

	// ---- 深度研究 -----------------------------------------------------------------

	openResearchPrompt(): void {
		const sel = this.app.workspace.activeEditor?.editor?.getSelection()?.trim();
		if (sel) {
			void this.queueResearch(sel);
		} else {
			new PromptModal(this.app, "深度研究", "研究主题…", (topic) =>
				void this.queueResearch(topic),
			).open();
		}
	}

	async queueResearch(topic: string): Promise<void> {
		topic = topic.trim();
		if (!topic) {
			new Notice("Institute: 主题为空。");
			return;
		}
		try {
			const res = await this.api.enqueueResearch(topic);
			if (res.refused === "cooldown") {
				new Notice(
					`Institute: 「${topic}」被拒 — 冷却中（上次完成于 ${res.last_completed_at ?? "最近"}）。`,
					8000,
				);
			} else if (res.deduped) {
				new Notice(
					`Institute: 「${topic}」已在队列中（状态：${researchStatusZh(res.status ?? "pending")}，id：${res.id ?? "?"}）。`,
					8000,
				);
			} else {
				new Notice(
					`Institute: 已排队深度研究「${topic}」（id：${res.id ?? "?"}）。完成后可在仪表盘打开报告。`,
					6000,
				);
			}
		} catch (e) {
			new Notice(`Institute: 深度研究排队失败 — ${errMsg(e)}`, 8000);
		}
	}

	async showResearchQueue(): Promise<void> {
		let items: ResearchQueueItem[];
		try {
			items = await this.api.researchQueue();
		} catch (e) {
			new Notice(`Institute: 无法获取研究队列 — ${errMsg(e)}`, 8000);
			return;
		}
		if (!items.length) {
			new Notice("Institute: 研究队列为空。");
			return;
		}
		new PickModal(
			this.app,
			items,
			(it) => {
				const date = sgtDate(it.finished_at) ?? sgtDate(it.created_at) ?? "";
				return `${researchStatusIcon(it.status)} ${it.topic} · ${researchStatusZh(it.status)}${date ? ` · ${date}` : ""}`;
			},
			(it) => {
				if (it.status === "completed") {
					void this.openResearchNote(it.topic, sgtDate(it.finished_at));
				} else {
					new Notice(
						`Institute: 「${it.topic}」状态：${researchStatusZh(it.status)}${it.error ? ` — ${it.error.slice(0, 120)}` : ""}`,
						6000,
					);
				}
			},
			"选择研究条目（已完成的会打开报告笔记）…",
		).open();
	}

	// ---- 白板 ------------------------------------------------------------------------

	openWhiteboardCommand(): void {
		const info = this.app.workspace.activeEditor;
		const editor = info?.editor ?? null;
		const file = info?.file ?? null;
		const sel = editor?.getSelection()?.trim() ?? "";
		if (file) {
			// topic = note title; question = selection or first content line
			const question = sel || (editor ? firstContentLine(editor.getValue()) : "");
			void this.addWhiteboardTopic(file.basename, question);
		} else if (sel) {
			void this.addWhiteboardTopic(sel, "");
		} else {
			new PromptModal(this.app, "加入白板议题", "议题…", (topic) =>
				void this.addWhiteboardTopic(topic, ""),
			).open();
		}
	}

	async addWhiteboardTopic(topic: string, question: string): Promise<void> {
		topic = topic.trim();
		if (!topic) {
			new Notice("Institute: 议题为空。");
			return;
		}
		try {
			const row = await this.api.addWhiteboardTopic(topic, question.trim());
			new Notice(
				`Institute: 「${topic}」已加入白板议题池（#${row.id ?? "?"}，状态：${row.status ?? "pending"}）。`,
				6000,
			);
		} catch (e) {
			new Notice(`Institute: 加入议题失败 — ${errMsg(e)}`, 8000);
		}
	}

	// ---- 导出研究 / 仓库体检 -----------------------------------------------------------

	async exportResearchCommand(): Promise<void> {
		let items: ResearchQueueItem[];
		try {
			items = await this.api.researchQueue("completed");
		} catch (e) {
			new Notice(`Institute: 无法获取已完成研究 — ${errMsg(e)}`, 8000);
			return;
		}
		if (!items.length) {
			new Notice("Institute: 没有已完成的研究可导出。");
			return;
		}
		new PickModal(
			this.app,
			items,
			(it) => `${it.topic} · ${sgtDate(it.finished_at) ?? "?"}`,
			(it) => void this.doExportResearch(it),
			"选择要导出到仓库的研究…",
		).open();
	}

	private async doExportResearch(item: ResearchQueueItem): Promise<void> {
		const notice = new Notice(`Institute: 正在导出「${item.topic}」…`, 0);
		try {
			const res = await this.api.exportResearch(item.id);
			notice.hide();
			new Notice(`Institute: 已导出 ${res.exported}`, 6000);
			await this.openVaultRel(res.exported);
		} catch (e) {
			notice.hide();
			new Notice(`Institute: 导出失败 — ${errMsg(e)}`, 8000);
		}
	}

	async vaultDoctorCommand(): Promise<void> {
		const notice = new Notice("Institute: 仓库体检中…", 0);
		let report: Record<string, number>;
		try {
			report = await this.api.vaultDoctor();
		} catch (e) {
			notice.hide();
			new Notice(`Institute: 体检失败 — ${errMsg(e)}`, 8000);
			return;
		}
		let conflicts: VaultIndexRow[] = [];
		if ((report["conflict"] ?? 0) > 0) {
			try {
				conflicts = await this.api.vaultIndex("conflict");
			} catch {
				/* show counts without the list */
			}
		}
		notice.hide();
		new VaultDoctorModal(this, report, conflicts).open();
	}

	// ---- 检索研究档案 ----------------------------------------------------------------

	async searchArchive(query: string, editor: Editor | null): Promise<void> {
		let hits: ArchiveHit[];
		try {
			hits = await this.api.archiveSearch(query, 15);
		} catch (e) {
			new Notice(`Institute: 检索失败 — ${errMsg(e)}`, 8000);
			return;
		}
		if (!hits.length) {
			new Notice(`Institute: 没有找到与「${query}」相关的档案。`, 6000);
			return;
		}
		const plain = (s: string) => s.replace(/<\/?b>/g, "").replace(/\s+/g, " ").trim();
		new PickModal(
			this.app,
			hits,
			(h) => `${plain(h.snippet)} — ${h.path}`,
			(h) => {
				if (!editor) {
					new Notice("Institute: 没有活动的编辑器，无法插入引用。", 6000);
					return;
				}
				const text = `> 引用：${plain(h.snippet)}\n> — institute archive: ${h.path}\n`;
				editor.replaceRange(text, editor.getCursor());
			},
			"选择要引用的档案片段…",
		).open();
	}

	// ---- 运行某位分析师日报 ------------------------------------------------------------

	async runAnalystDailyCommand(): Promise<void> {
		let roster: Analyst[];
		let marks: Record<string, string> = {};
		try {
			roster = await this.getRoster();
		} catch (e) {
			new Notice(`Institute: 无法加载分析师名册 — ${errMsg(e)}`, 8000);
			return;
		}
		try {
			marks = (await this.api.dailyStatus()).analysts ?? {};
		} catch {
			/* marks are cosmetic */
		}
		const items = roster.filter((a) => a.category !== "ops");
		if (!items.length) {
			new Notice("Institute: 名册中没有可运行日报的分析师。");
			return;
		}
		const mark = (id: string) => {
			const s = marks[id];
			return s === "completed" ? "✓" : s === "failed" ? "✗" : "○";
		};
		new PickModal(
			this.app,
			items,
			(a) => `${mark(a.id)} ${a.emoji} ${a.name}（${a.id}）— ${a.focus}`,
			(a) => void this.runOneDaily(a),
			"选择分析师（✓ 今日已完成 / ○ 待运行）…",
		).open();
	}

	private async runOneDaily(a: Analyst): Promise<void> {
		try {
			await this.api.runAnalystDaily(a.id);
			new Notice(
				`Institute: 已启动 ${a.emoji} ${a.name} 的日报（后台运行，完成后自动导出）。`,
				6000,
			);
		} catch (e) {
			new Notice(`Institute: 启动日报失败 — ${errMsg(e)}`, 8000);
		}
	}

	// ---- vault note opening -------------------------------------------------------------
	// The backend exports notes RELATIVE to its vault_dir; settings.vaultSubfolder
	// maps that root into this Obsidian vault (default "Institute").

	subPath(rel: string): string {
		const sub = this.settings.vaultSubfolder.trim().replace(/^\/+|\/+$/g, "");
		return normalizePath(sub ? `${sub}/${rel}` : rel);
	}

	private async openFileAt(path: string): Promise<boolean> {
		const f = this.app.vault.getAbstractFileByPath(normalizePath(path));
		if (f instanceof TFile) {
			await this.app.workspace.getLeaf().openFile(f);
			return true;
		}
		return false;
	}

	/**
	 * Fallback: list the folder and open the best (name-descending) markdown
	 * file containing every fragment. With fallbackAny, a topic-specific folder
	 * may fall back to its newest file when fragments don't match.
	 */
	private async openBestMatch(
		folderRel: string,
		fragments: string[],
		fallbackAny = false,
	): Promise<boolean> {
		const folder = this.app.vault.getAbstractFileByPath(
			normalizePath(this.subPath(folderRel)),
		);
		if (!(folder instanceof TFolder)) return false;
		const files = folder.children.filter(
			(c): c is TFile => c instanceof TFile && c.extension === "md",
		);
		let pool = files.filter((f) => fragments.every((s) => s && f.name.includes(s)));
		if (!pool.length && fallbackAny) pool = files;
		if (!pool.length) return false;
		pool.sort((a, b) => b.name.localeCompare(a.name));
		await this.app.workspace.getLeaf().openFile(pool[0]);
		return true;
	}

	async openVaultRel(rel: string): Promise<void> {
		if (await this.openFileAt(this.subPath(rel))) return;
		new Notice(
			`Institute: 在 vault 中找不到 ${this.subPath(rel)} — 请检查「Vault 子目录」设置。`,
			7000,
		);
	}

	async openResearchNote(topic: string, date?: string | null): Promise<void> {
		const folder = `Research/${exportSlug(topic)}`;
		if (date && (await this.openFileAt(this.subPath(`${folder}/${date} 深度报告.md`)))) return;
		if (await this.openBestMatch(folder, ["深度报告"], true)) return;
		new Notice("Institute: 笔记尚未导出。", 5000);
	}

	async openAnalystDailyNote(analystId: string, date?: string | null): Promise<void> {
		const folder = `Analysts/${exportSlug(analystId)}`;
		if (date && (await this.openFileAt(this.subPath(`${folder}/${date} 日报.md`)))) return;
		if (await this.openBestMatch(folder, date ? [date] : ["日报"], true)) return;
		new Notice("Institute: 笔记尚未导出。", 5000);
	}

	async openWhiteboardNote(topic: string, date?: string | null): Promise<void> {
		const slug = exportSlug(topic);
		if (date && (await this.openFileAt(this.subPath(`Whiteboard/${date} ${slug}.md`)))) return;
		if (await this.openBestMatch("Whiteboard", [slug.slice(0, 16)])) return;
		new Notice("Institute: 笔记尚未导出。", 5000);
	}

	// ---- Status bar ----------------------------------------------------------

	async refreshStatus(): Promise<void> {
		try {
			const meta = await this.api.meta();
			const by = meta.queue?.by_status ?? {};
			const running = by["running"] ?? 0;
			const queued = by["queued"] ?? 0;
			let dailyTxt = "";
			try {
				const ds = await this.api.dailyStatus();
				const vals = Object.values(ds.analysts ?? {});
				const done = vals.filter((v) => v === "completed").length;
				if (vals.length > 0 && done < vals.length) dailyTxt = ` ·日报${done}/${vals.length}`;
			} catch {
				/* meta reachable but daily status optional */
			}
			this.statusBar.setText(`⚙︎ inst: ${running}运行/${queued}排队${dailyTxt}`);
			this.statusBar.style.color = "";
			this.statusBar.setAttribute(
				"aria-label",
				`Institute One v${meta.version ?? "?"} @ ${this.settings.baseUrl} — 点击打开仪表盘`,
			);
		} catch {
			this.statusBar.setText("✗ inst");
			this.statusBar.style.color = "var(--text-error)";
			this.statusBar.setAttribute(
				"aria-label",
				`Institute One 无法连接（${this.settings.baseUrl}）— 点击打开仪表盘`,
			);
		}
	}

	// ---- settings io ------------------------------------------------------

	async loadSettings(): Promise<void> {
		this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
		if (!Number.isFinite(this.settings.pollIntervalS) || this.settings.pollIntervalS < 5) {
			this.settings.pollIntervalS = DEFAULT_SETTINGS.pollIntervalS;
		}
		if (!this.settings.roadmapStatusOverrides) {
			this.settings.roadmapStatusOverrides = {};
		}
	}

	async saveSettings(): Promise<void> {
		await this.saveData(this.settings);
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** The paragraph around the cursor: expand from the cursor line to the
 * nearest blank lines in both directions ("" when the cursor sits on a
 * blank line). Used by claim-check when there is no selection. */
function paragraphAround(editor: Editor): string {
	const cur = editor.getCursor().line;
	if (!editor.getLine(cur).trim()) return "";
	let start = cur;
	while (start > 0 && editor.getLine(start - 1).trim()) start--;
	let end = cur;
	const last = editor.lineCount() - 1;
	while (end < last && editor.getLine(end + 1).trim()) end++;
	const lines: string[] = [];
	for (let i = start; i <= end; i++) lines.push(editor.getLine(i));
	return lines.join("\n").trim();
}

/** First non-empty content line of a note body (skips frontmatter, strips #). */
function firstContentLine(body: string): string {
	let lines = body.split("\n");
	if (lines[0]?.trim() === "---") {
		const end = lines.findIndex((l, i) => i > 0 && l.trim() === "---");
		if (end > 0) lines = lines.slice(end + 1);
	}
	for (const raw of lines) {
		const line = raw.replace(/^#+\s*/, "").replace(/^>\s*/, "").trim();
		if (line) return line.slice(0, 200);
	}
	return "";
}

// ---------------------------------------------------------------------------
// Settings tab
// ---------------------------------------------------------------------------

class InstituteSettingTab extends PluginSettingTab {
	constructor(
		app: App,
		private plugin: InstituteOnePlugin,
	) {
		super(app, plugin);
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();

		new Setting(containerEl)
			.setName("后端地址 (base URL)")
			.setDesc("Institute One 后端运行的地址。")
			.addText((t) =>
				t
					.setPlaceholder(DEFAULT_SETTINGS.baseUrl)
					.setValue(this.plugin.settings.baseUrl)
					.onChange(async (v) => {
						this.plugin.settings.baseUrl =
							v.trim().replace(/\/+$/, "") || DEFAULT_SETTINGS.baseUrl;
						await this.plugin.saveSettings();
						void this.plugin.refreshStatus();
					}),
			);

		new Setting(containerEl)
			.setName("访问令牌 (bearer token)")
			.setDesc("后端设置 INSTITUTE_TOKEN 时填写同一令牌；未启用鉴权时留空。")
			.addText((t) => {
				t.inputEl.type = "password";
				t.setPlaceholder("未设置")
					.setValue(this.plugin.settings.token)
					.onChange(async (v) => {
						this.plugin.settings.token = v.trim();
						await this.plugin.saveSettings();
					});
			});

		new Setting(containerEl)
			.setName("Vault 子目录")
			.setDesc(
				"后端导出笔记所在的 vault 子目录（对应后端的 vault_dir）。留空表示 vault 根目录。",
			)
			.addText((t) =>
				t
					.setPlaceholder(DEFAULT_SETTINGS.vaultSubfolder)
					.setValue(this.plugin.settings.vaultSubfolder)
					.onChange(async (v) => {
						this.plugin.settings.vaultSubfolder = v.trim().replace(/^\/+|\/+$/g, "");
						await this.plugin.saveSettings();
					}),
			);

		new Setting(containerEl)
			.setName("默认分析师")
			.setDesc("提问/写信时预选的分析师（提问后会自动记住上次选择）。")
			.addDropdown((dd) => {
				dd.addOption("", "（默认执行手，无人格）");
				dd.setValue("");
				dd.onChange(async (v) => {
					this.plugin.settings.defaultAnalyst = v;
					await this.plugin.saveSettings();
				});
				void this.plugin
					.getRoster()
					.then((roster) => {
						for (const a of roster) {
							dd.addOption(a.id, `${a.emoji} ${a.name}（${a.name_en}）`);
						}
						if (roster.some((a) => a.id === this.plugin.settings.defaultAnalyst)) {
							dd.setValue(this.plugin.settings.defaultAnalyst);
						}
					})
					.catch(() => {
						/* backend offline: leave only the empty option */
					});
			});

		new Setting(containerEl)
			.setName("回答插入样式")
			.setDesc("提问的回答以 callout 引用块还是纯文本插入。")
			.addDropdown((dd) => {
				dd.addOption("callout", "Callout 引用块（推荐）");
				dd.addOption("plain", "纯文本");
				dd.setValue(this.plugin.settings.insertStyle);
				dd.onChange(async (v) => {
					this.plugin.settings.insertStyle = v === "plain" ? "plain" : "callout";
					await this.plugin.saveSettings();
				});
			});

		new Setting(containerEl)
			.setName("仪表盘轮询间隔（秒）")
			.setDesc("仪表盘可见时的自动刷新间隔，最小 5 秒。重新打开仪表盘后生效。")
			.addText((t) =>
				t
					.setPlaceholder(String(DEFAULT_SETTINGS.pollIntervalS))
					.setValue(String(this.plugin.settings.pollIntervalS))
					.onChange(async (v) => {
						const n = parseInt(v, 10);
						this.plugin.settings.pollIntervalS = Number.isFinite(n)
							? Math.max(5, n)
							: DEFAULT_SETTINGS.pollIntervalS;
						await this.plugin.saveSettings();
					}),
			);

		// ---- read-only vault layout ------------------------------------------
		const sub = this.plugin.settings.vaultSubfolder || "(vault 根目录)";
		const desc = document.createDocumentFragment();
		desc.appendText(`只读说明：后端把完成的工作导出到「${sub}」下，布局如下：`);
		const ul = desc.createEl("ul");
		for (const line of [
			"Research/<主题>/<日期> 深度报告.md — 深度研究报告",
			"Briefing/<日期> 晨会简报.md — 晨会简报",
			"Daily/<日期> 每日日报.md — 每日日报",
			"Analysts/<分析师>/<日期> 日报.md — 分析师观察日报",
			"Whiteboard/<日期> <主题>.md — 白板（每位分析师一节）",
			"Ask/<日期> <提问>.md — 本插件在无编辑器时创建（在 vault 根目录）",
		]) {
			ul.createEl("li", { text: line });
		}
		new Setting(containerEl)
			.setName("Vault 笔记布局")
			.setDesc(desc)
			.setDisabled(true);
	}
}
