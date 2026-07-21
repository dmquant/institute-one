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

/** GET /api/roadmap/cards — institute.roadmap.list_cards() rows */
export interface RoadmapApiCard {
	id: string;
	title: string;
	type: string;
	phase: string;
	status: string;
	priority: string;
	risk: string;
	summary: string;
	problem: string;
	implementation: string;
	agent_prompt: string;
	owner: string | null;
	blocked_reason: string | null;
	sort_order: number;
	design_links: string[];
	expected_files: string[];
	verification: string[];
	tags: string[];
	/** list endpoint: dependency ids; the detail endpoint replaces this with rows */
	dependencies: string[];
	created_at: string;
	updated_at: string;
	completed_at: string | null;
}

/** roadmap_checklists row (kind: acceptance | implementation | review) */
export interface RoadmapChecklistItem {
	id: string;
	card_id: string;
	kind: string;
	text: string;
	checked: number; // sqlite 0/1
	sort_order: number;
	created_at: string;
	updated_at: string;
}

/** roadmap_dependencies row joined with the dependency's live status */
export interface RoadmapDependencyRow {
	id: string;
	card_id: string;
	depends_on_id: string;
	relation: string;
	created_at: string;
	depends_on_status: string | null;
}

/** roadmap_coding_sessions row; GET /api/roadmap/sessions adds n_commands,
 * GET /api/roadmap/process additionally joins card_title */
export interface RoadmapSession {
	id: string;
	card_id: string;
	actor: string;
	goal: string;
	status: string; // active | completed | partial | blocked | cancelled
	planned_files: string[];
	touched_files: string[];
	summary: string;
	started_at: string;
	finished_at: string | null;
	n_commands?: number;
	card_title?: string;
}

/** GET /api/roadmap/cards/{id} — also returned by POST …/move */
export interface RoadmapApiCardDetail extends Omit<RoadmapApiCard, "dependencies"> {
	checklists: RoadmapChecklistItem[];
	dependencies: RoadmapDependencyRow[];
	evidence: Record<string, unknown>[];
	sessions: RoadmapSession[];
}

/** POST /api/roadmap/import — institute.roadmap.import_backlog() */
export interface RoadmapImportResult {
	created: number;
	updated: number;
	unchanged: number;
	total: number;
}

/** GET /api/roadmap/release-gates — institute.roadmap.release_gates() rows */
export interface RoadmapReleaseGate {
	name: string;
	description: string;
	prefixes: string[];
	total: number;
	done: number;
	pct: number;
	status: string; // met | open
	remaining: string[];
}

/** roadmap_decisions row; GET /api/roadmap/process joins card_title */
export interface RoadmapDecision {
	id: string;
	card_id: string | null;
	title: string;
	question: string;
	options: string[];
	decision: string | null;
	status: string; // open | resolved
	created_at: string;
	resolved_at: string | null;
	card_title?: string | null;
}

/** GET /api/roadmap/process — release-gate readiness (card status + evidence) */
export interface RoadmapProcessGate {
	gate: string;
	description: string;
	prefixes: string[];
	cards_total: number;
	cards_done: number;
	/** scoped cards carrying at least one pass-verdict evidence row */
	evidence_ready: number;
	/** not-done scoped cards blocked by blocked_reason or open dependencies */
	blockers: string[];
	ready: boolean;
}

/** GET /api/roadmap/process — blocked card row (visible without opening the card) */
export interface RoadmapBlockedCard {
	id: string;
	title: string;
	phase: string;
	status: string;
	owner: string | null;
	blocked_reason: string | null;
	open_dependencies: string[];
}

/** GET /api/roadmap/process — institute.roadmap.process_overview() (M7-006) */
export interface RoadmapProcessOverview {
	active_sessions: RoadmapSession[];
	open_decisions: RoadmapDecision[];
	release_gates: RoadmapProcessGate[];
	blocked_cards: RoadmapBlockedCard[];
}

/** POST /api/meta/claim_check_before_write hit (institute.factcheck.claim_check) */
export interface ClaimCheckHit {
	fact_card_id: string;
	claim: string;
	category: string;
	verdict: string; // VERIFIED | DISPUTED
	similarity: number; // cosine or keyword-overlap, 0..1
	source: string; // vector | keyword
}

/** POST /api/meta/claim_check_before_write — never raises server-side */
export interface ClaimCheckResult {
	mode: string; // none | vector+keyword | keyword | error
	hits: ClaimCheckHit[];
}

/** GET /api/operator/triage — one aggregate for the triage panel */
export interface TriageResult {
	maintenance?: {
		paused?: boolean;
		drain_depth?: number;
		queue?: { by_status?: Record<string, number> };
	};
	feature_switches?: Record<string, boolean>;
	hand_weights?: { configured?: number; by_scope?: Record<string, Record<string, number>> };
	cron?: { window_days?: number; jobs?: number; failing?: string[] };
	vault?: { ledger_total?: number; conflicts?: number };
	actions?: {
		by_status?: Record<string, number>;
		open_by_kind?: Record<string, number>;
		open?: number;
	};
}

/** GET /api/operator/actions — operator_actions rows with dispositions inlined. */
export type OperatorActionStatus = "open" | "in_progress" | "done" | "dismissed";

export interface OperatorActionDisposition {
	id: number;
	action_id: number;
	proposed_by: string;
	disposition: string;
	confidence: number | null;
	shadow: number;
	flags: string;
	created_at: string;
}

export interface OperatorAction {
	id: number;
	kind: string;
	ref: string;
	title: string;
	detail: string;
	status: OperatorActionStatus;
	priority: number;
	created_at: string;
	updated_at: string;
	resolved_at: string | null;
	resolution: string | null;
	dispositions: OperatorActionDisposition[];
}

export interface OperatorActionsResult {
	actions: OperatorAction[];
	count: number;
}

/** GET /api/book/nav — nav_history rows, ascending by work_date */
export interface NavRow {
	work_date: string;
	nav: number;
	benchmark_nav: number | null;
	gross_exposure: number;
	n_open: number;
	n_unpriced: number;
	realized_pnl_cum: number;
}

/** GET /api/book/positions — paper_positions rows */
export interface PaperPositionRow {
	id: string;
	forecast_id: string;
	security_id: string | null;
	direction: string; // long | short
	entry_date: string;
	entry_price: number;
	size: number;
	status: string; // open | closed
	opened_at: string;
	closed_at: string | null;
	close_reason: string | null;
	close_price: number | null;
	realized_pnl: number | null;
}

/** GET /api/forecasts — forecast ledger row. */
export interface ForecastRow {
	id: string;
	thesis_id: string;
	security_id: string | null;
	claim: string;
	direction: "long" | "short" | "neutral";
	conviction: number | null;
	horizon_days: number;
	settlement_rule: { type: string; threshold?: number; benchmark_id?: string } | string;
	made_at: string;
	expires_at: string;
	status: "open" | "settled" | "invalid";
	created_at: string;
	updated_at: string;
	settlement?: ForecastSettlement | null;
}

/** GET /api/forecasts/{id} — settlement is present on the detail response. */
export interface ForecastSettlement {
	id: string;
	forecast_id: string;
	verdict: "hit" | "miss" | "partial" | "invalid";
	settled_at: string;
	benchmark_return: number | null;
	actual_return: number | null;
	note: string;
	created_at: string;
}

/** GET /api/research/trees — list row with node-count aggregates. */
export type ResearchTreeStatus = "pending" | "exploring" | "completed" | "stopped" | "failed";
export type ResearchTreeNodeStatus = "pending" | "running" | "completed" | "failed" | "pruned";

export interface ResearchTreeRow {
	id: string;
	root_topic: string;
	status: ResearchTreeStatus;
	max_depth: number;
	max_nodes: number;
	created_at: string;
	finished_at: string | null;
	announced_at: string | null;
	nodes_total?: number;
	nodes_completed?: number;
}

export interface ResearchTreeNode {
	id: string;
	tree_id: string;
	parent_id: string | null;
	depth: number;
	topic: string;
	question: string;
	status: ResearchTreeNodeStatus;
	task_id: string | null;
	summary: string | null;
	score: number | null;
	created_at: string;
	finished_at: string | null;
}

export interface ResearchTreeDetail extends ResearchTreeRow {
	nodes: ResearchTreeNode[];
}

/** One NDJSON frame from POST /api/ask/stream (app/api/ask_stream.py) */
export interface AskStreamFrame {
	type: string; // stdout | stderr | status | done
	text?: string;
	task?: AskTask;
}

// ---------------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------------

export function errMsg(e: unknown): string {
	if (e instanceof Error) return e.message;
	return String(e);
}

/** HTTP error carrying the status code so callers can branch on 400/409. */
export class ApiError extends Error {
	constructor(
		message: string,
		readonly status: number,
	) {
		super(message);
	}
}

/**
 * True when an error means "this backend doesn't serve that endpoint yet"
 * (plugin newer than backend): unknown route (404), wrong method on an
 * existing prefix (405), or an explicit not-implemented (501). Callers hide
 * the feature or show a "后端未启用" notice instead of an error.
 */
export function isMissingEndpoint(e: unknown): boolean {
	return e instanceof ApiError && (e.status === 404 || e.status === 405 || e.status === 501);
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
	constructor(
		private getBaseUrl: () => string,
		private getToken: () => string,
	) {}

	baseUrl(): string {
		return this.getBaseUrl().replace(/\/+$/, "");
	}

	private authHeaders(): Record<string, string> | undefined {
		const token = this.getToken().trim();
		return token ? { Authorization: `Bearer ${token}` } : undefined;
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
				headers: this.authHeaders(),
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
			throw new ApiError(`HTTP ${resp.status} — ${detail.slice(0, 300)}`, resp.status);
		}
		return resp.json as T;
	}

	/** Plain-text GET (the /api/institute/*.md digests return text/markdown). */
	async requestText(path: string, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<string> {
		const url = this.baseUrl() + path;
		const resp = await withTimeout(
			requestUrl({ url, method: "GET", headers: this.authHeaders(), throw: false }),
			timeoutMs,
			`GET ${path}`,
		);
		if (resp.status >= 400) {
			throw new ApiError(
				`HTTP ${resp.status} — ${(resp.text ?? "").slice(0, 300)}`,
				resp.status,
			);
		}
		return resp.text ?? "";
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

	async archiveSearch(q: string, limit = 15): Promise<ArchiveHit[]> {
		// backend now returns {mode, results} (vector-search upgrade); keep the
		// ArchiveHit[] contract for callers
		const resp = await this.request<{ mode: string; results: ArchiveHit[] }>(
			`/api/archive/search?q=${encodeURIComponent(q)}&limit=${limit}`,
		);
		return resp.results ?? [];
	}

	// ---- roadmap -----------------------------------------------------------------

	listCards(): Promise<RoadmapApiCard[]> {
		return this.request<RoadmapApiCard[]>("/api/roadmap/cards");
	}

	getCard(cardId: string): Promise<RoadmapApiCardDetail> {
		return this.request<RoadmapApiCardDetail>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}`,
		);
	}

	/**
	 * POST /api/roadmap/cards/{id}/move. Backend rejections: 400 = move rule
	 * (open dependencies / missing owner / no evidence — retry with override),
	 * 409 = concurrent change (expected_status mismatch or lost claim — reload).
	 */
	moveCard(
		cardId: string,
		to: string,
		override = false,
		expected?: string | null,
	): Promise<RoadmapApiCardDetail> {
		const body: Record<string, unknown> = { status: to, override };
		if (expected != null) body.expected_status = expected;
		return this.request<RoadmapApiCardDetail>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}/move`,
			{ method: "POST", body },
		);
	}

	/** POST /api/roadmap/import — idempotent upsert of roadmap/backlog.json by card id. */
	importSeed(force = false): Promise<RoadmapImportResult> {
		return this.request<RoadmapImportResult>("/api/roadmap/import", {
			method: "POST",
			body: { force },
		});
	}

	/** GET /api/roadmap/sessions?card_id= — newest first, rows carry n_commands. */
	listSessions(cardId: string): Promise<RoadmapSession[]> {
		return this.request<RoadmapSession[]>(
			`/api/roadmap/sessions?card_id=${encodeURIComponent(cardId)}`,
		);
	}

	/** POST /api/roadmap/cards/{id}/sessions — opens an active coding session. */
	createSession(
		cardId: string,
		actor: string,
		goal: string,
		plannedFiles: string[] = [],
	): Promise<RoadmapSession> {
		return this.request<RoadmapSession>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}/sessions`,
			{ method: "POST", body: { actor, goal, planned_files: plannedFiles } },
		);
	}

	/**
	 * PATCH /api/roadmap/sessions/{id}. A terminal status (completed/partial/
	 * blocked/cancelled) sets finished_at exactly once — 409 on a lost claim.
	 */
	updateSession(
		sessionId: string,
		patch: {
			status?: string;
			goal?: string;
			summary?: string;
			planned_files?: string[];
			touched_files?: string[];
		},
	): Promise<RoadmapSession> {
		return this.request<RoadmapSession>(
			`/api/roadmap/sessions/${encodeURIComponent(sessionId)}`,
			{ method: "PATCH", body: patch },
		);
	}

	/** GET /api/roadmap/release-gates — gate progress projected from card phases. */
	releaseGates(): Promise<RoadmapReleaseGate[]> {
		return this.request<RoadmapReleaseGate[]>("/api/roadmap/release-gates");
	}

	/** GET /api/roadmap/cards/{id}/prompt — deterministic agent prompt (M7-007). */
	cardPrompt(cardId: string): Promise<{ prompt: string }> {
		return this.request<{ prompt: string }>(
			`/api/roadmap/cards/${encodeURIComponent(cardId)}/prompt`,
		);
	}

	/** GET /api/roadmap/process — sessions + decisions + gates + blocked cards (M7-006). */
	processOverview(): Promise<RoadmapProcessOverview> {
		return this.request<RoadmapProcessOverview>("/api/roadmap/process");
	}

	// ---- events ------------------------------------------------------------------

	events(since: number, limit: number, types?: string): Promise<EventRow[]> {
		const t = types ? `&types=${encodeURIComponent(types)}` : "";
		return this.request<EventRow[]>(`/api/events?since=${since}&limit=${limit}${t}`);
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

	// ---- fact-check (Phase 3) --------------------------------------------------

	/** POST /api/meta/claim_check_before_write — writing-time claim check. */
	claimCheck(text: string, k = 8): Promise<ClaimCheckResult> {
		return this.request<ClaimCheckResult>("/api/meta/claim_check_before_write", {
			method: "POST",
			// backend caps text at 20K (422 above it) — pre-slice so a huge
			// paragraph degrades to a partial check instead of an error
			body: { text: text.slice(0, 20000), k },
			timeoutMs: 30_000, // embedding leg can be slow on first call
		});
	}

	// ---- operator triage (Phase 6) ----------------------------------------------

	/** GET /api/operator/triage — maintenance/actions/cron/vault aggregate. */
	triage(): Promise<TriageResult> {
		return this.request<TriageResult>("/api/operator/triage");
	}

	/** GET /api/operator/actions — `open` is the backend's pending-inbox state. */
	operatorActions(
		status: OperatorActionStatus = "open",
		limit = 1000,
	): Promise<OperatorActionsResult> {
		return this.request<OperatorActionsResult>(
			`/api/operator/actions?status=${encodeURIComponent(status)}&limit=${limit}`,
		);
	}

	// ---- paper book ---------------------------------------------------------------

	/** GET /api/book/nav — nav_history rows ascending; last row = latest NAV. */
	bookNav(days = 30): Promise<NavRow[]> {
		return this.request<NavRow[]>(`/api/book/nav?days=${days}`);
	}

	/** GET /api/book/positions?status=open — open paper positions. */
	bookPositions(status = "open", limit = 200): Promise<PaperPositionRow[]> {
		return this.request<PaperPositionRow[]>(
			`/api/book/positions?status=${encodeURIComponent(status)}&limit=${limit}`,
		);
	}

	// ---- forecasts ----------------------------------------------------------------

	/** GET /api/forecasts — newest first. */
	forecasts(limit = 5): Promise<ForecastRow[]> {
		return this.request<ForecastRow[]>(`/api/forecasts?limit=${limit}`);
	}

	/** GET /api/forecasts/{id} — includes the settlement row. */
	forecast(forecastId: string): Promise<ForecastRow> {
		return this.request<ForecastRow>(
			`/api/forecasts/${encodeURIComponent(forecastId)}`,
		);
	}

	// ---- research trees -----------------------------------------------------------

	researchTrees(status?: ResearchTreeStatus, limit = 200): Promise<ResearchTreeRow[]> {
		const q = status
			? `?status=${encodeURIComponent(status)}&limit=${limit}`
			: `?limit=${limit}`;
		return this.request<ResearchTreeRow[]>(`/api/research/trees${q}`);
	}

	researchTree(treeId: string): Promise<ResearchTreeDetail> {
		return this.request<ResearchTreeDetail>(
			`/api/research/tree/${encodeURIComponent(treeId)}`,
		);
	}

	// ---- digests (/api/institute/*.md — text/markdown) ---------------------------

	digestRecentReports(days = 7): Promise<string> {
		return this.requestText(`/api/institute/recent-reports.md?days=${days}`);
	}

	digestAnalystMemory(analystId: string): Promise<string> {
		return this.requestText(
			`/api/institute/analyst-memory/${encodeURIComponent(analystId)}.md`,
		);
	}

	digestAnalystDisputes(analystId: string): Promise<string> {
		return this.requestText(
			`/api/institute/analyst-disputes/${encodeURIComponent(analystId)}.md`,
		);
	}
}
