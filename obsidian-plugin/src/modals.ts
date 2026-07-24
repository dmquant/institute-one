import {
	App,
	Component,
	Editor,
	FuzzySuggestModal,
	MarkdownRenderer,
	Modal,
	Notice,
	Setting,
	TFile,
	normalizePath,
} from "obsidian";
import { Analyst, ClaimCheckResult, VaultIndexRow, errMsg, fileSlug, todayStr } from "./api";
import type InstituteOnePlugin from "./main";

// ---------------------------------------------------------------------------
// 提问 (Ask)
// ---------------------------------------------------------------------------

export class AskModal extends Modal {
	private analyst: Analyst | null = null;
	private roster: Analyst[] = [];
	private prompt: string;

	constructor(
		private plugin: InstituteOnePlugin,
		initialPrompt: string,
		private editor: Editor | null,
	) {
		super(plugin.app);
		this.prompt = initialPrompt;
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText("提问 (Ask the Institute)");

		new Setting(contentEl)
			.setName("分析师")
			.setDesc("以哪位分析师的人格回答（来自后端名册，记住上次选择）。")
			.addDropdown((dd) => {
				dd.addOption("", "（默认执行手，无人格）");
				dd.onChange((v) => {
					this.analyst = this.roster.find((a) => a.id === v) ?? null;
				});
				void this.plugin
					.getRoster()
					.then((roster) => {
						this.roster = roster;
						for (const a of roster) {
							dd.addOption(a.id, `${a.emoji} ${a.name}（${a.name_en}）— ${a.focus}`);
						}
						const last = this.plugin.settings.defaultAnalyst;
						if (last && roster.some((a) => a.id === last)) {
							dd.setValue(last);
							this.analyst = roster.find((a) => a.id === last) ?? null;
						}
					})
					.catch((e) => {
						new Notice(`Institute: 无法加载分析师名册 — ${errMsg(e)}`, 6000);
					});
			});

		new Setting(contentEl)
			.setName("问题")
			.setDesc("已预填当前选中文本（如有）。Cmd/Ctrl+Enter 提交。")
			.addTextArea((ta) => {
				ta.setValue(this.prompt);
				ta.setPlaceholder("想让研究所研究什么？");
				ta.onChange((v) => (this.prompt = v));
				ta.inputEl.rows = 8;
				ta.inputEl.style.width = "100%";
				ta.inputEl.addEventListener("keydown", (ev) => {
					if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
						ev.preventDefault();
						this.submit();
					}
				});
				window.setTimeout(() => ta.inputEl.focus(), 0);
			});

		new Setting(contentEl).addButton((b) =>
			b
				.setButtonText("提问")
				.setCta()
				.onClick(() => this.submit()),
		);
	}

	private submit(): void {
		const prompt = this.prompt.trim();
		if (!prompt) {
			new Notice("Institute: 问题为空。");
			return;
		}
		this.close();
		// remember the last-used analyst
		this.plugin.settings.defaultAnalyst = this.analyst?.id ?? "";
		void this.plugin.saveSettings();
		void this.plugin.runAsk(prompt, this.analyst, this.editor);
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

// ---------------------------------------------------------------------------
// Single-line prompt (research topic / whiteboard topic / archive query)
// ---------------------------------------------------------------------------

export class PromptModal extends Modal {
	private value = "";

	constructor(
		app: App,
		private title: string,
		private placeholder: string,
		private onSubmit: (value: string) => void,
	) {
		super(app);
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText(this.title);

		new Setting(contentEl).setName("内容").addText((t) => {
			t.setPlaceholder(this.placeholder);
			t.onChange((v) => (this.value = v));
			t.inputEl.style.width = "100%";
			t.inputEl.addEventListener("keydown", (ev) => {
				if (ev.key === "Enter") {
					ev.preventDefault();
					this.submit();
				}
			});
			window.setTimeout(() => t.inputEl.focus(), 0);
		});

		new Setting(contentEl).addButton((b) =>
			b
				.setButtonText("提交")
				.setCta()
				.onClick(() => this.submit()),
		);
	}

	private submit(): void {
		const value = this.value.trim();
		if (!value) {
			new Notice("Institute: 内容为空。");
			return;
		}
		this.close();
		this.onSubmit(value);
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

// ---------------------------------------------------------------------------
// Generic fuzzy picker (research queue, archive hits, analysts…)
// ---------------------------------------------------------------------------

export class PickModal<T> extends FuzzySuggestModal<T> {
	constructor(
		app: App,
		private items: T[],
		private toText: (item: T) => string,
		private onPick: (item: T) => void,
		placeholder = "",
	) {
		super(app);
		if (placeholder) this.setPlaceholder(placeholder);
	}

	getItems(): T[] {
		return this.items;
	}

	getItemText(item: T): string {
		return this.toText(item);
	}

	onChooseItem(item: T): void {
		this.onPick(item);
	}
}

// ---------------------------------------------------------------------------
// 写信给分析师 (mailbox)
// ---------------------------------------------------------------------------

export class MailModal extends Modal {
	private analystId: string;
	private subject = "";
	private body: string;

	constructor(
		private plugin: InstituteOnePlugin,
		initialBody: string,
	) {
		super(plugin.app);
		this.body = initialBody;
		this.analystId = plugin.settings.defaultAnalyst;
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText("写信给分析师");

		new Setting(contentEl)
			.setName("收件人")
			.setDesc("信件会开一个信箱会话，分析师在后台回复。")
			.addDropdown((dd) => {
				dd.addOption("", "（选择分析师）");
				dd.onChange((v) => (this.analystId = v));
				void this.plugin
					.getRoster()
					.then((roster) => {
						for (const a of roster) {
							dd.addOption(a.id, `${a.emoji} ${a.name}（${a.name_en}）`);
						}
						if (this.analystId && roster.some((a) => a.id === this.analystId)) {
							dd.setValue(this.analystId);
						} else {
							this.analystId = "";
						}
					})
					.catch((e) => {
						new Notice(`Institute: 无法加载分析师名册 — ${errMsg(e)}`, 6000);
					});
			});

		new Setting(contentEl).setName("主题").addText((t) => {
			t.setPlaceholder("信件主题…");
			t.onChange((v) => (this.subject = v));
			t.inputEl.style.width = "100%";
		});

		new Setting(contentEl)
			.setName("正文")
			.setDesc("已预填当前选中文本（如有）。")
			.addTextArea((ta) => {
				ta.setValue(this.body);
				ta.setPlaceholder("想问什么、想让对方核实什么…");
				ta.onChange((v) => (this.body = v));
				ta.inputEl.rows = 8;
				ta.inputEl.style.width = "100%";
			});

		new Setting(contentEl).addButton((b) =>
			b
				.setButtonText("发送")
				.setCta()
				.onClick(() => this.submit()),
		);
	}

	private submit(): void {
		if (!this.analystId) {
			new Notice("Institute: 请选择分析师。");
			return;
		}
		if (!this.subject.trim()) {
			new Notice("Institute: 请填写主题。");
			return;
		}
		if (!this.body.trim()) {
			new Notice("Institute: 请填写正文。");
			return;
		}
		this.close();
		void this.send();
	}

	private async send(): Promise<void> {
		try {
			await this.plugin.api.createMailThread(
				this.subject.trim(),
				this.analystId,
				this.body.trim(),
			);
			new Notice("Institute: 已发送，回复将出现在操作台信箱。", 6000);
		} catch (e) {
			new Notice(`Institute: 发送失败 — ${errMsg(e)}`, 8000);
		}
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

// ---------------------------------------------------------------------------
// 查证选中文本 (claim check before write, Phase 3)
// ---------------------------------------------------------------------------

const CLAIM_CHECK_MODE_ZH: Record<string, string> = {
	"vector+keyword": "向量近邻 + 关键词",
	keyword: "关键词（向量层不可用，已降级）",
	error: "检查失败（向量与关键词两条腿都不可用）",
	none: "文本为空",
};

export class ClaimCheckModal extends Modal {
	constructor(
		app: App,
		private checkedText: string,
		private result: ClaimCheckResult,
	) {
		super(app);
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText("查证结果 (claim check)");

		// what was checked, collapsed to one line
		const src = contentEl.createDiv();
		src.style.color = "var(--text-muted)";
		src.style.fontSize = "12px";
		src.style.marginBottom = "8px";
		src.style.overflow = "hidden";
		src.style.whiteSpace = "nowrap";
		src.style.textOverflow = "ellipsis";
		const flat = this.checkedText.replace(/\s+/g, " ").trim();
		src.setText(`已查证：${flat.length > 80 ? flat.slice(0, 80) + "…" : flat}`);
		src.setAttribute("title", flat.slice(0, 500));

		const mode = contentEl.createDiv();
		mode.style.fontSize = "12px";
		mode.style.marginBottom = "10px";
		mode.setText(`匹配方式：${CLAIM_CHECK_MODE_ZH[this.result.mode] ?? this.result.mode}`);
		if (this.result.mode === "keyword" || this.result.mode === "error") {
			mode.style.color = "var(--color-orange)";
		} else {
			mode.style.color = "var(--text-muted)";
		}

		const hits = this.result.hits ?? [];
		if (!hits.length) {
			const ok = contentEl.createDiv({
				text: "未命中任何已核查事实 — 该文本不与事实库中的 VERIFIED/DISPUTED 论断重叠。",
			});
			ok.style.color = "var(--text-muted)";
			return;
		}

		const disputed = hits.filter((h) => h.verdict === "DISPUTED").length;
		if (disputed > 0) {
			const warn = contentEl.createDiv({
				text: `⚠️ ${disputed} 条命中为「有争议 (DISPUTED)」— 写入前请复核。`,
			});
			warn.style.color = "var(--text-error)";
			warn.style.fontWeight = "600";
			warn.style.marginBottom = "8px";
		}

		for (const h of hits) {
			const isDisputed = h.verdict === "DISPUTED";
			const row = contentEl.createDiv();
			row.style.padding = "6px 8px";
			row.style.marginBottom = "6px";
			row.style.borderRadius = "6px";
			row.style.border = "1px solid var(--background-modifier-border)";
			if (isDisputed) {
				row.style.background = "rgba(var(--color-red-rgb), 0.12)";
				row.style.borderColor = "var(--color-red)";
			}

			const head = row.createDiv();
			head.style.display = "flex";
			head.style.alignItems = "baseline";
			head.style.gap = "8px";
			head.style.marginBottom = "2px";

			const badge = head.createSpan({
				text: isDisputed ? "有争议 DISPUTED" : "已核实 VERIFIED",
			});
			badge.style.fontSize = "11px";
			badge.style.fontWeight = "700";
			badge.style.color = isDisputed ? "var(--text-error)" : "var(--color-green)";

			const simPct = Number.isFinite(h.similarity)
				? `${(h.similarity * 100).toFixed(1)}%`
				: "?";
			const meta = head.createSpan({
				text: `相似度 ${simPct} · ${h.category} · ${h.source === "vector" ? "向量" : "关键词"}`,
			});
			meta.style.fontSize = "11px";
			meta.style.color = "var(--text-muted)";

			const claim = row.createDiv({ text: h.claim });
			claim.style.fontSize = "13px";
			if (isDisputed) claim.style.color = "var(--text-error)";

			const cardId = row.createDiv({ text: `卡片：${h.fact_card_id}` });
			cardId.style.fontSize = "10px";
			cardId.style.color = "var(--text-faint)";
		}
	}

	onClose(): void {
		this.contentEl.empty();
	}
}

// ---------------------------------------------------------------------------
// Digest 展示（/api/institute/*.md — 渲染 markdown，可另存为笔记）
// ---------------------------------------------------------------------------

export class DigestModal extends Modal {
	private renderHost = new Component();

	constructor(
		app: App,
		private title: string,
		private markdown: string,
	) {
		super(app);
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText(this.title);
		this.renderHost.load();

		const body = contentEl.createDiv();
		body.style.maxHeight = "60vh";
		body.style.overflowY = "auto";
		body.style.padding = "4px 2px";
		void MarkdownRenderer.render(this.app, this.markdown, body, "", this.renderHost);

		const actions = contentEl.createDiv();
		actions.style.display = "flex";
		actions.style.gap = "8px";
		actions.style.marginTop = "12px";

		const save = actions.createEl("button", { text: "另存为笔记" });
		save.addEventListener("click", () => void this.saveAsNote());
		const copy = actions.createEl("button", { text: "复制 Markdown" });
		copy.addEventListener("click", () => {
			void navigator.clipboard.writeText(this.markdown);
			new Notice("Institute: 已复制。", 3000);
		});
	}

	private async saveAsNote(): Promise<void> {
		const folder = "Ask";
		if (!this.app.vault.getAbstractFileByPath(folder)) {
			try {
				await this.app.vault.createFolder(folder);
			} catch {
				/* already exists / race */
			}
		}
		const base = `${todayStr()} ${fileSlug(this.title)}`;
		let path = normalizePath(`${folder}/${base}.md`);
		let n = 1;
		while (this.app.vault.getAbstractFileByPath(path)) {
			path = normalizePath(`${folder}/${base} ${++n}.md`);
		}
		try {
			const file: TFile = await this.app.vault.create(path, this.markdown);
			this.close();
			await this.app.workspace.getLeaf(true).openFile(file);
		} catch (e) {
			new Notice(`Institute: 保存失败 — ${errMsg(e)}`, 8000);
		}
	}

	onClose(): void {
		this.renderHost.unload();
		this.contentEl.empty();
	}
}

// ---------------------------------------------------------------------------
// 仓库体检 (vault doctor)
// ---------------------------------------------------------------------------

const DOCTOR_LABELS: [string, string][] = [
	["total", "总计"],
	["clean", "清洁"],
	["conflict", "冲突"],
	["missing", "缺失"],
	["drifted", "漂移"],
];

export class VaultDoctorModal extends Modal {
	constructor(
		private plugin: InstituteOnePlugin,
		private report: Record<string, number>,
		private conflicts: VaultIndexRow[],
	) {
		super(plugin.app);
	}

	onOpen(): void {
		const { contentEl } = this;
		this.titleEl.setText("仓库体检 (vault doctor)");

		const table = contentEl.createEl("table");
		table.style.width = "100%";
		table.style.borderCollapse = "collapse";
		for (const [key, label] of DOCTOR_LABELS) {
			const tr = table.createEl("tr");
			const td1 = tr.createEl("td", { text: label });
			td1.style.padding = "2px 12px 2px 0";
			td1.style.color = "var(--text-muted)";
			const value = this.report[key] ?? 0;
			const td2 = tr.createEl("td", { text: String(value) });
			td2.style.padding = "2px 0";
			if (key === "conflict" && value > 0) td2.style.color = "var(--color-orange)";
			if ((key === "missing" || key === "drifted") && value > 0) {
				td2.style.color = "var(--color-red)";
			}
		}

		if (this.conflicts.length > 0) {
			const h = contentEl.createEl("h5", {
				text: "冲突笔记（人工编辑过，后端已另存「institute update」副本）",
			});
			h.style.marginBottom = "6px";
			for (const row of this.conflicts) {
				const line = contentEl.createDiv();
				line.style.display = "flex";
				line.style.alignItems = "center";
				line.style.gap = "8px";
				line.style.padding = "2px 0";
				const path = line.createSpan({ text: row.path });
				path.style.flex = "1";
				path.style.fontSize = "12px";
				path.style.wordBreak = "break-all";
				const btn = line.createEl("button", { text: "打开" });
				btn.addEventListener("click", () => {
					this.close();
					void this.plugin.openVaultRel(row.path);
				});
			}
		} else if ((this.report["conflict"] ?? 0) === 0) {
			const ok = contentEl.createDiv({ text: "没有冲突笔记。" });
			ok.style.color = "var(--text-muted)";
			ok.style.marginTop = "8px";
		}
	}

	onClose(): void {
		this.contentEl.empty();
	}
}
