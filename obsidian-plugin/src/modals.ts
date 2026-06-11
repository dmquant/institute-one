import { App, Editor, FuzzySuggestModal, Modal, Notice, Setting } from "obsidian";
import { Analyst, VaultIndexRow, errMsg } from "./api";
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
