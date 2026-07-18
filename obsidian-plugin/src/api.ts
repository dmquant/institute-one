import { requestUrl } from "obsidian";

// ---------------------------------------------------------------------------
// Timeouts. POST /api/ask is synchronous on the backend (it runs the model
// and waits), so it gets a much longer budget. Everything else: 10s.
// ---------------------------------------------------------------------------

export const DEFAULT_TIMEOUT_MS = 10_000;
export const ASK_TIMEOUT_MS = 15 * 60_000;

// ---------------------------------------------------------------------------
// Backend payload shapes (derived from app/api/*.py)
// ---------------------------------------------------------------------------

/** GET /api/analysts — list[asdict(Analyst)] */
export interface Analyst {
	id: string;
	name: string;
	name_en: string;
	category: string;
	emoji: string;
	focus: string;
	persona: string;
	hand: string | null;
	model: string | null;
}

/** POST /api/ask — router.executor.Task (asdict) */
export interface AskTask {
	id: string;
	status: string;
	hand: string | null;
	output: string;
	error: string | null;
	exit_code: number | null;
}

/** GET /api/tasks — executor.list_tasks row */
export interface TaskRow {
	id: string;
	session_id: string | null;
	hand: string | null;
	requested_hand: string;
	model: string | null;
	status: string;
	source: string;
	exit_code: number | null;
	error: string | null;
	parent_run_id: string | null;
	created_at: string | null;
	started_at: string | null;
	finished_at: string | null;
}

/** meta.hands[] — registry.status_snapshot() */
export interface HandStatus {
	name: string;
	type: string;
	installed: boolean;
	available: boolean;
	degraded: boolean;
	cooldown_until: number | null; // epoch seconds
	cooldown_reason: string | null;
	consecutive_failures: number;
	fallback_chain: string[];
}

/** GET /api/meta */
export interface MetaResult {
	version?: string;
	timezone?: string;
	work_date?: string;
	hands?: HandStatus[];
	vault_configured?: boolean;
	queue?: {
		by_status?: Record<string, number>;
		running_now?: number;
	};
}

/** GET /api/analysts/daily/status — institute.analyst_daily.status() */
export interface DailyStatus {
	date: string;
	analysts: Record<string, string>; // analyst id -> completed | failed | pending
	session_id: string | null;
}

/** GET /api/research/queue — research_queue rows */
export interface ResearchQueueItem {
	id: string;
	topic: string;
	priority?: number;
	status: string;
	source?: string;
	run_id?: string | null;
	error?: string | null;
	created_at?: string | null;
	started_at?: string | null;
	finished_at?: string | null;
}

/** POST /api/research/queue — queue row, or {deduped}, or {refused: "cooldown"} */
export interface ResearchEnqueueResult {
	id?: string;
	topic?: string;
	status?: string;
	deduped?: boolean;
	refused?: string;
	last_completed_at?: string;
}

/** POST /api/whiteboard/topics — topic_pool row (INSERT OR IGNORE dedupe) */
export interface TopicPoolRow {
	id?: number;
	topic?: string;
	status?: string;
	score?: number;
}

/** GET /api/events — bus.Event.to_dict() */
export interface EventRow {
	id: number;
	type: string;
	ref_kind: string;
	ref_id: string;
	payload: Record<string, unknown>;
	created_at: string;
}

/** GET /api/archive/search — FTS rows with <b>…</b> highlighted snippets */
export interface ArchiveHit {
	path: string;
	ref_kind: string;
	ref_id: string;
	snippet: string;
}

/** GET /api/vault/index rows */
export interface VaultIndexRow {
	path: string;
	artifact_kind: string;
	artifact_id: string;
	sha256: string;
	state: string;
	written_at: string;
}

// ---------------------------------------------------------------------------
// Roadmap control plane (app/api/roadmap.py)
// ---------------------------------------------------------------------------

/** GET /api/roadmap/cards row (full card projection from list_cards). */
export interface RoadmapLiveCard {
	id: string;
	title: string;
	type: string;
	phase: string;
	status: string;
	priority: string;
	risk: string;
	owner: string | null;
	summary: string;
	blocked_reason: string | null;
	sort_order: number;
	design_links: string[];
	expected_files: string[];
	verification: string[];
	tags: string[];
	dependencies: string[];
}

/** GET /api/roadmap/sessions row. */
export interface RoadmapSessionRow {
	id: string;
	card_id: string;
	actor: string;
	goal: string;
	status: "active" | "completed" | "partial" | "blocked" | "cancelled";
	planned_files: string[];
	touched_files: string[];
	summary: string;
	started_at: string;
	finished_at: string | null;
	n_commands?: number;
}

/** GET /api/roadmap/decisions row. */
export interface RoadmapDecisionRow {
	id: string;
	card_id: string | null;
	title: string;
	question: string;
	options: string[];
	decision: string | null;
	status: string;
	created_at: string;
	resolved_at: string | null;
}

/** GET /api/roadmap/release-gates row. */
export interface RoadmapGate {
	name: string;
	description: string;
	prefixes: string[];
	total: number;
	done: number;
	pct: number;
	status: "met" | "open";
	remaining: string[];
	evidence_ready: string[];
}

/** GET /api/roadmap/cards/{id}/prompt */
export interface RoadmapPrompt {
	card_id: string;
	prompt: string;
	generated: boolean;
}

/** POST /api/roadmap/import result. */
export interface RoadmapImportResult {
	created: number;
	updated: number;
	unchanged: number;
	total: number;
}

/** GET /api/roadmap/cards/{id} — detail fields the plugin hydrates from. */
export interface RoadmapCardDetail extends RoadmapLiveCard {
	checklists: Array<{ id: string; kind: string; text: string; checked: number }>;
}

// ---------------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------------

export function errMsg(e: unknown): string {
	if (e instanceof Error) return e.message;
	return String(e);
}

function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const t = window.setTimeout(
			() => reject(new Error(`${what} 超时（${Math.round(ms / 1000)}s）`)),
			ms,
		);
		p.then(
			(v) => {
				window.clearTimeout(t);
				resolve(v);
			},
			(e) => {
				window.clearTimeout(t);
				reject(e);
			},
		);
	});
}

/**
 * Mirrors the backend exporter slug (app/vault/exporter.py:_slug):
 * keep CJK, replace path-hostile chars with "-", collapse whitespace,
 * strip " -." from both ends, cap at 80 chars.
 */
export function exportSlug(text: string, maxLen = 80): string {
	// eslint-disable-next-line no-control-regex
	let s = String(text ?? "").trim().replace(/[\\/:*?"<>|#^[\]\u0000-\u001f]+/g, "-");
	s = s.replace(/\s+/g, " ");
	s = s.replace(/-{2,}/g, "-");
	s = s.replace(/^[ \-.]+|[ \-.]+$/g, "");
	s = s.slice(0, maxLen).replace(/^[ \-.]+|[ \-.]+$/g, "");
	return s || "untitled";
}

/** Filename-safe slug for notes this plugin creates itself (Ask/). */
export function fileSlug(text: string, maxLen = 40): string {
	let s = (text || "").trim().replace(/[\\/:*?"<>|#^[\] -]+/g, "-");
	s = s.replace(/\s+/g, " ").replace(/-{2,}/g, "-").replace(/^[\s\-.]+|[\s\-.]+$/g, "");
	return s.slice(0, maxLen).replace(/^[\s\-.]+|[\s\-.]+$/g, "") || "untitled";
}

export function todayStr(): string {
	const d = new Date();
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Convert a backend ISO timestamp (UTC) to the SGT (UTC+8) calendar date. */
export function sgtDate(iso: string | null | undefined): string | null {
	if (!iso) return null;
	const t = Date.parse(iso);
	if (Number.isNaN(t)) return null;
	return new Date(t + 8 * 3600_000).toISOString().slice(0, 10);
}

/** "3m12s" style elapsed time since an ISO timestamp. */
export function fmtElapsed(iso: string | null | undefined): string {
	const t = Date.parse(iso ?? "");
	if (Number.isNaN(t)) return "—";
	let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	s = s % 60;
	if (h > 0) return `${h}h${m}m`;
	if (m > 0) return `${m}m${s}s`;
	return `${s}s`;
}

/** Countdown until an epoch-seconds deadline ("47m" / "1h12m" / "30s"). */
export function fmtCountdown(untilEpochS: number): string {
	let s = Math.max(0, Math.round(untilEpochS - Date.now() / 1000));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	s = s % 60;
	if (h > 0) return `${h}h${m}m`;
	if (m > 0) return `${m}m`;
	return `${s}s`;
}

/** "14:03" local wall-clock time from an ISO timestamp. */
export function fmtClock(iso: string | null | undefined): string {
	const t = Date.parse(iso ?? "");
	if (Number.isNaN(t)) return "--:--";
	const d = new Date(t);
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function researchStatusZh(status: string): string {
	const map: Record<string, string> = {
		pending: "待处理",
		running: "进行中",
		completed: "已完成",
		failed: "失败",
		cancelled: "已取消",
	};
	return map[status] ?? status;
}

export function researchStatusIcon(status: string): string {
	const map: Record<string, string> = {
		pending: "○",
		running: "▶",
		completed: "✓",
		failed: "✗",
		cancelled: "⊘",
	};
	return map[status] ?? "·";
}

// ---------------------------------------------------------------------------
// HTTP client. Obsidian's renderer is cross-origin to the backend and the
// backend sends no CORS headers, so fetch/EventSource would fail — every
// request MUST go through Obsidian's requestUrl (CORS-free). "Live" data is
// polled with requestUrl; no SSE.
// ---------------------------------------------------------------------------

export class InstituteApi {
	constructor(private getBaseUrl: () => string) {}

	baseUrl(): string {
		return this.getBaseUrl().replace(/\/+$/, "");
	}

	/** JSON request with a hard timeout (default 10s). Never uses fetch. */
	async request<T>(
		path: string,
		opts: { method?: string; body?: unknown; timeoutMs?: number } = {},
	): Promise<T> {
		const method = opts.method ?? "GET";
		const url = this.baseUrl() + path;
		const resp = await withTimeout(
			requestUrl({
				url,
				method,
				contentType: opts.body !== undefined ? "application/json" : undefined,
				body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
				throw: false,
			}),
			opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
			`${method} ${path}`,
		);
		if (resp.status >= 400) {
			let detail = "";
			try {
				detail = String((resp.json as { detail?: unknown })?.detail ?? resp.text ?? "");
			} catch {
				detail = resp.text ?? "";
			}
			throw new Error(`HTTP ${resp.status} — ${detail.slice(0, 300)}`);
		}
		return resp.json as T;
	}

	// ---- meta / status -----------------------------------------------------

	meta(): Promise<MetaResult> {
		return this.request<MetaResult>("/api/meta");
	}

	analysts(): Promise<Analyst[]> {
		return this.request<Analyst[]>("/api/analysts");
	}

	dailyStatus(): Promise<DailyStatus> {
		return this.request<DailyStatus>("/api/analysts/daily/status");
	}

	runAllDailies(): Promise<unknown> {
		return this.request("/api/analysts/daily/run-now", { method: "POST" });
	}

	runAnalystDaily(analystId: string): Promise<unknown> {
		return this.request(`/api/analysts/${encodeURIComponent(analystId)}/daily/run`, {
			method: "POST",
		});
	}

	// ---- tasks ---------------------------------------------------------------

	listTasks(status: string, limit = 100): Promise<TaskRow[]> {
		return this.request<TaskRow[]>(
			`/api/tasks?status=${encodeURIComponent(status)}&limit=${limit}`,
		);
	}

	cancelTask(taskId: string): Promise<{ cancelled: boolean }> {
		return this.request<{ cancelled: boolean }>(
			`/api/tasks/${encodeURIComponent(taskId)}/cancel`,
			{ method: "POST" },
		);
	}

	ask(prompt: string, analystId: string | null): Promise<AskTask> {
		return this.request<AskTask>("/api/ask", {
			method: "POST",
			body: { prompt, analyst_id: analystId || null },
			timeoutMs: ASK_TIMEOUT_MS,
		});
	}

	// ---- research --------------------------------------------------------------

	researchQueue(status?: string, limit = 100): Promise<ResearchQueueItem[]> {
		const q = status ? `?status=${encodeURIComponent(status)}&limit=${limit}` : `?limit=${limit}`;
		return this.request<ResearchQueueItem[]>(`/api/research/queue${q}`);
	}

	enqueueResearch(topic: string): Promise<ResearchEnqueueResult> {
		return this.request<ResearchEnqueueResult>("/api/research/queue", {
			method: "POST",
			body: { topic },
		});
	}

	exportResearch(queueId: string): Promise<{ exported: string }> {
		return this.request<{ exported: string }>(
			`/api/vault/export/research/${encodeURIComponent(queueId)}`,
			{ method: "POST" },
		);
	}

	// ---- whiteboard / mailbox / archive -----------------------------------------

	addWhiteboardTopic(topic: string, question = ""): Promise<TopicPoolRow> {
		return this.request<TopicPoolRow>("/api/whiteboard/topics", {
			method: "POST",
			body: { topic, question },
		});
	}

	createMailThread(subject: string, analystId: string, body: string): Promise<unknown> {
		return this.request("/api/mailbox/threads", {
			method: "POST",
			body: { subject, analyst_id: analystId, body },
		});
	}

	archiveSearch(q: string, limit = 15): Promise<ArchiveHit[]> {
		return this.request<ArchiveHit[]>(
			`/api/archive/search?q=${encodeURIComponent(q)}&limit=${limit}`,
		);
	}

	// ---- events ------------------------------------------------------------------

	events(since: number, limit: number, types?: string): Promise<EventRow[]> {
		const t = types ? `&types=${encodeURIComponent(types)}` : "";
		return this.request<EventRow[]>(`/api/events?since=${since}&limit=${limit}${t}`);
	}

	// ---- roadmap -------------------------------------------------------------------

	roadmapCards(): Promise<RoadmapLiveCard[]> {
		return this.request<RoadmapLiveCard[]>("/api/roadmap/cards");
	}

	roadmapCardDetail(cardId: string): Promise<RoadmapCardDetail> {
		return this.request<RoadmapCardDetail>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}`,
		);
	}

	roadmapImportSeed(force = false): Promise<RoadmapImportResult> {
		return this.request<RoadmapImportResult>("/api/roadmap/import", {
			method: "POST",
			body: { force },
		});
	}

	roadmapSessions(cardId?: string, status?: string, limit = 100): Promise<RoadmapSessionRow[]> {
		const params = new URLSearchParams();
		if (cardId) params.set("card_id", cardId);
		if (status) params.set("status", status);
		params.set("limit", String(limit));
		return this.request<RoadmapSessionRow[]>(`/api/roadmap/sessions?${params.toString()}`);
	}

	roadmapDecisions(status?: string, limit = 100): Promise<RoadmapDecisionRow[]> {
		const params = new URLSearchParams();
		if (status) params.set("status", status);
		params.set("limit", String(limit));
		return this.request<RoadmapDecisionRow[]>(`/api/roadmap/decisions?${params.toString()}`);
	}

	resolveRoadmapDecision(decisionId: string, decision: string): Promise<RoadmapDecisionRow> {
		return this.request<RoadmapDecisionRow>(
			`/api/roadmap/decisions/${encodeURIComponent(decisionId)}`,
			{ method: "PATCH", body: { decision } },
		);
	}

	roadmapReleaseGates(): Promise<RoadmapGate[]> {
		return this.request<RoadmapGate[]>("/api/roadmap/release-gates");
	}

	roadmapAgentPrompt(cardId: string): Promise<RoadmapPrompt> {
		return this.request<RoadmapPrompt>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}/prompt`,
		);
	}

	// ---- vault ---------------------------------------------------------------------

	vaultDoctor(): Promise<Record<string, number>> {
		return this.request<Record<string, number>>("/api/vault/doctor", { method: "POST" });
	}

	vaultIndex(state: string, limit = 200): Promise<VaultIndexRow[]> {
		return this.request<VaultIndexRow[]>(
			`/api/vault/index?state=${encodeURIComponent(state)}&limit=${limit}`,
		);
	}
}
