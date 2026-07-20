// Typed fetch client for the institute-one FastAPI backend.
// Shapes are derived directly from app/api/*.py, app/router/executor.py,
// app/institute/*.py and migrations/0001_init.sql.

const BASE = "";

// ---------------------------------------------------------------- types ----

export type TaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "rate_limited"
  | "cancelled"
  | "expired";

/** Full Task dataclass (GET /api/tasks/{id}, POST /api/ask). */
export interface Task {
  id: string;
  status: TaskStatus;
  hand: string | null;
  requested_hand: string;
  model: string | null;
  prompt: string;
  source: string;
  session_id: string | null;
  parent_run_id: string | null;
  workspace_dir: string;
  exit_code: number | null;
  output: string;
  error: string | null;
  artifacts: string[] | null;
  tried: string[] | null;
}

/** Row shape from executor.list_tasks (GET /api/tasks). */
export interface TaskRow {
  id: string;
  session_id: string | null;
  hand: string | null;
  requested_hand: string;
  model: string | null;
  status: TaskStatus;
  source: string;
  exit_code: number | null;
  error: string | null;
  parent_run_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface QueueStats {
  by_status: Record<string, number>;
  running_now: number;
}

export interface HandStatus {
  name: string;
  type: string;
  installed: boolean;
  available: boolean;
  degraded: boolean;
  cooldown_until: number | null; // unix seconds
  cooldown_reason: string | null;
  consecutive_failures: number;
  fallback_chain: string[];
}

export interface Meta {
  version: string;
  timezone: string;
  work_date: string;
  hands: HandStatus[];
  vault_configured: boolean;
  queue: QueueStats;
  limits: {
    max_concurrent: number;
    default_timeout_s: number;
    output_cap_bytes: number;
  };
}

export interface BusEvent {
  id: number;
  type: string;
  ref_kind: string;
  ref_id: string;
  payload: Record<string, unknown>;
  created_at: string;
}

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

/** GET /api/analysts/roles. */
export interface AnalystDailyStatus {
  date: string;
  analysts: Record<string, string>; // analyst_id -> pending|completed|failed
  session_id: string | null;
}

export interface AnalystRoles {
  roles: string[];
  in_use: string[];
}

/** Body for POST/PUT /api/analysts (empty hand/model -> null). */
export interface AnalystInput {
  id: string;
  name: string;
  name_en: string;
  category: string;
  emoji: string;
  focus: string;
  persona: string;
  hand?: string | null;
  model?: string | null;
}

export interface WorkflowStep {
  id?: string;
  title?: string;
  analyst?: string;
  analyst_id?: string;
  prompt?: string;
  output_file?: string;
  timeout_s?: number;
}

export interface Workflow {
  id: string;
  name: string;
  description: string;
  variables: string[];
  steps: WorkflowStep[];
  updated_at: string;
}

export interface StepResult {
  step_id: string;
  title: string;
  task_id: string;
  status: TaskStatus;
  summary: string;
  output_file: string | null;
}

export type RunStatus = "running" | "completed" | "failed" | "cancelled";

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  session_id: string | null;
  status: RunStatus;
  variables: Record<string, string>;
  current_step: number;
  results: StepResult[];
  error: string | null;
  source: string;
  started_at: string;
  finished_at: string | null;
}

export type BoardStatus = "active" | "completed" | "stopped" | "failed";
export type CardStatus = "pending" | "running" | "completed" | "failed";

export interface Board {
  id: string;
  topic: string;
  question: string;
  status: BoardStatus;
  max_cards: number;
  session_id: string | null;
  work_date: string;
  created_at: string;
  updated_at: string;
  n_cards?: number; // list endpoint only
}

export interface Card {
  id: string;
  board_id: string;
  idx: number;
  analyst_id: string;
  status: CardStatus;
  question: string;
  summary: string | null;
  output_file: string | null;
  task_id: string | null;
  created_at: string;
  finished_at: string | null;
}

export interface BoardDetail extends Board {
  cards: Card[];
}

export interface Topic {
  id: number;
  topic: string;
  question: string;
  source: string;
  score: number;
  status: "pending" | "used" | "expired";
  content_hash: string | null;
  created_at: string;
}

export interface MailThread {
  id: string;
  subject: string;
  analyst_id: string;
  status: "open" | "closed";
  created_at: string;
  updated_at: string;
  n_messages?: number; // list endpoint only
}

export interface MailMessage {
  id: number;
  thread_id: string;
  author: string; // 'operator' or analyst_id
  kind: "note" | "dispatch" | "reply";
  body: string;
  task_id: string | null;
  status: string; // dispatch: pending|done|failed
  created_at: string;
}

export interface MailThreadDetail extends MailThread {
  messages: MailMessage[];
}

export type ResearchStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

export interface ResearchItem {
  id: string;
  topic: string;
  priority: number;
  status: ResearchStatus;
  source: string;
  run_id: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ResearchItemDetail extends ResearchItem {
  run: WorkflowRun | null;
}

/** enqueue may dedupe or refuse on cooldown. */
export type EnqueueResult =
  | (ResearchItem & { deduped?: boolean })
  | { refused: string; topic: string; last_completed_at: string };

export interface ResearchLogRow {
  id: number;
  topic: string;
  run_id: string | null;
  summary: string | null;
  completed_at: string;
}

export interface Session {
  id: string;
  title: string;
  kind: string;
  analyst_id: string | null;
  workspace_dir: string;
  created_at: string;
  updated_at: string;
}

export interface WorkspaceFile {
  path: string;
  size: number;
  mtime: string;
}

export interface VaultStatus {
  configured: boolean;
  vault_dir: string | null;
  counts: Record<string, number>;
  total: number;
}

export interface Health {
  ok: boolean;
  version: string;
  time_sgt: string;
}

/** GET /api/cron/health (app/api/meta.py): scheduler registry LEFT JOIN
 * cron_metrics — jobs that never fired are present with zeroed metric fields;
 * gated is null only for metrics-only names no longer in the registry. */
export interface CronJobHealth {
  registered: boolean;
  gated: boolean | null;
  schedule: string | null;
  next_run_time: string | null;
  last_fired_at: string | null;
  last_status: "ok" | "failed" | "skipped" | null;
  fires: number;
  ok: number;
  failed: number;
  skipped: number;
  ok_rate: number | null;
  avg_duration_ms: number | null;
  last_error: { fired_at: string; error: string | null } | null;
}

export interface CronHealth {
  window_days: number;
  jobs: Record<string, CronJobHealth>;
}

/** Row from GET /api/hands/weights (migrations/0009). */
export interface HandWeightRow {
  scope: string;
  hand: string;
  weight: number;
  updated_at: string;
}

export const WEIGHT_SCOPES = ["default", "whiteboard", "research", "daily", "mailbox"] as const;
export type WeightScope = (typeof WEIGHT_SCOPES)[number];

export interface WeightEntry {
  scope: WeightScope;
  hand: string;
  weight: number;
}

export type ScorecardVerdict = "ok" | "stub" | "false_complete";

export interface ScorecardEntry {
  hand: string;
  work_date: string;
  task_id: string;
  verdict: ScorecardVerdict;
  reason: string | null;
  created_at: string;
}

/** GET /api/hands/scorecard?date= */
export interface Scorecard {
  date: string;
  counts: Record<ScorecardVerdict, number>;
  by_hand: Record<string, Record<ScorecardVerdict, number>>;
  entries: ScorecardEntry[];
}

export interface HandStatsAgg {
  tasks_total: number;
  tasks_ok: number;
  tasks_failed: number;
  tasks_rate_limited: number;
  avg_duration_ms: number | null;
}

/** GET /api/hands/stats?hours= */
export interface HandStats {
  hours: number;
  since: string;
  by_hand: Record<string, HandStatsAgg>;
  windows: Record<string, unknown>[];
}

/** Forecast row (app/institute/forecasts.py — SELECT * FROM forecasts). */
export interface Forecast {
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
  settlement?: ForecastSettlement | null; // GET /api/forecasts/{id} only
}

export interface ForecastSettlement {
  id: string;
  forecast_id: string;
  verdict: "hit" | "miss" | "partial" | "invalid";
  settled_at: string;
  benchmark_return: number | null;
  actual_return: number | null;
  note: string | null;
  created_at: string;
}

// Paper book (C3, landing in parallel — shape read from app/institute/
// paper_book.py: SELECT * FROM paper_positions / nav_history). Fields stay
// optional and render through fallbacks so a contract drift degrades softly;
// a 404/501 from the API renders as "账本未启用".
export interface BookPosition {
  id?: string;
  forecast_id?: string;
  security_id?: string | null;
  direction?: "long" | "short" | string;
  entry_date?: string;
  entry_price?: number;
  size?: number;
  stop_pct?: number;
  target_pct?: number;
  status?: "open" | "closed" | string;
  opened_at?: string;
  closed_at?: string | null;
  close_reason?: "stop" | "target" | "horizon" | "manual" | string | null;
  close_price?: number | null;
  realized_pnl?: number | null;
  [k: string]: unknown;
}

export interface BookNavPoint {
  work_date?: string;
  nav?: number;
  benchmark_nav?: number | null;
  gross_exposure?: number;
  n_open?: number;
  realized_pnl_cum?: number;
  [k: string]: unknown;
}

/** NDJSON frames from POST /api/ask/stream (app/api/ask_stream.py). */
export interface AskStreamChunk {
  type: "stdout" | "stderr" | "status";
  text: string;
}

export interface AskDoneTask {
  id: string | null;
  status: TaskStatus | "failed";
  hand: string | null;
  exit_code: number | null;
  error: string | null;
  output: string;
}

export type AskStreamFrame = AskStreamChunk | { type: "done"; task: AskDoneTask };

export interface AskBody {
  prompt: string;
  analyst_id?: string | null;
  hand?: string | null;
  model?: string | null;
  timeout_s?: number | null;
}

// Operator triage & actions kanban (E4 — app/api/operator.py; row shapes from
// migrations/0018). GET callers catch ApiError 404/501 → 运维面未启用.
export type OperatorActionStatus = "open" | "in_progress" | "done" | "dismissed";
export type OperatorActionKind =
  | "vault_conflict"
  | "disputed_fact"
  | "scorecard_anomaly"
  | "failed_run"
  | "cron_failure"
  | "other";

/** action_dispositions row — shadow=1 rows are logged suggestions only,
 * consumed exclusively via the human approve endpoint. */
export interface ActionDisposition {
  id: number;
  action_id: number;
  proposed_by: "fast_loop" | "deep_loop" | "human" | string;
  disposition: string; // router vocabulary or 'unparsed'
  confidence: number | null;
  shadow: number; // 0 | 1
  flags: string; // comma-joined: low_confidence, human_pinned, approved
  created_at: string;
}

/** operator_actions row with dispositions inlined (GET /api/operator/actions). */
export interface OperatorAction {
  id: number;
  kind: OperatorActionKind;
  ref: string;
  title: string;
  detail: string;
  status: OperatorActionStatus;
  priority: number; // higher = more urgent
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  resolution: string | null;
  dispositions: ActionDisposition[];
}

/** GET /api/operator/triage aggregate. */
export interface OperatorTriage {
  maintenance: { paused: boolean; drain_depth: number; queue: QueueStats };
  feature_switches: Record<string, boolean>;
  /** CAS token for putFeatureSwitches (0 until the first versioned save). */
  feature_switches_version: number;
  hand_weights: { configured: number; by_scope: Record<string, Record<string, number>> };
  cron: { window_days: number; jobs: number; failing: string[] };
  vault: { ledger_total: number; conflicts: number };
  actions: {
    by_status: Record<string, number>;
    open_by_kind: Record<string, number>;
    open: number;
  };
}

// Multi-agent compare (C5, in flight) — defensive: 404 renders as 未启用.
export interface MultiAgentResult {
  agent?: string;
  hand?: string;
  analyst_id?: string;
  status?: string;
  output?: string;
  error?: string | null;
  task_id?: string | null;
  [k: string]: unknown;
}

export interface MultiAgentRun {
  id?: string;
  mode?: string;
  status?: string;
  results?: MultiAgentResult[];
  [k: string]: unknown;
}

// BFS research trees (Phase 7 Explore mode — app/api/research_tree.py).
// GET /tree/{id} returns the tree row plus a FLAT `nodes` list in BFS order
// carrying parent_id references; the viewer rebuilds nesting client-side.
export type TreeStatus = "pending" | "exploring" | "completed" | "stopped" | "failed";
export type TreeNodeStatus = "pending" | "running" | "completed" | "failed" | "pruned";

export interface TreeNode {
  id: string;
  tree_id: string;
  parent_id: string | null; // null = root
  depth: number;
  topic: string;
  question: string;
  status: TreeNodeStatus;
  task_id: string | null;
  summary: string | null;
  score: number | null; // canonical SCORE line, 0-100; malformed/missing -> null
  created_at: string;
  finished_at: string | null;
}

export interface ResearchTree {
  id: string;
  root_topic: string;
  status: TreeStatus;
  max_depth: number;
  max_nodes: number;
  created_at: string;
  finished_at: string | null;
  announced_at: string | null;
  nodes_total?: number; // list endpoint only
  nodes_completed?: number; // list endpoint only
}

export interface ResearchTreeDetail extends ResearchTree {
  nodes: TreeNode[];
}

/** create_tree may refuse on the daily cap (research-queue cooldown shape). */
export type CreateTreeResult =
  | ResearchTreeDetail
  | { refused: "daily_cap"; cap: number; booked_today: number; root_topic: string };

// Research projects (Phase 7 containers — app/api/projects.py).
export type ProjectStatus = "active" | "archived";
export type ProjectLinkKind = "research" | "board" | "thread" | "tree";

export interface Project {
  id: string;
  name: string;
  description: string;
  status: ProjectStatus;
  created_at: string;
  n_links?: number; // list endpoint only
}

// Per-kind attachment rows from projects.get() — LEFT JOINs, so the enriched
// fields are null when the referenced row is gone (render ref_id fallbacks).
export interface ProjectResearchLink {
  ref_id: string;
  topic?: string | null;
  status?: string | null;
  run_id?: string | null;
  created_at: string;
}

export interface ProjectBoardLink {
  ref_id: string;
  topic?: string | null;
  status?: string | null;
  work_date?: string | null;
  created_at: string;
}

export interface ProjectThreadLink {
  ref_id: string;
  subject?: string | null;
  analyst_id?: string | null;
  status?: string | null;
  created_at: string;
}

export interface ProjectTreeLink {
  ref_id: string;
  root_topic?: string | null;
  status?: string | null;
  created_at: string;
}

export interface ProjectDetail extends Project {
  links: {
    research: ProjectResearchLink[];
    board: ProjectBoardLink[];
    thread: ProjectThreadLink[];
    tree: ProjectTreeLink[];
  };
}

/** bilingual.twin_ready event payload (BY REFERENCE — the full translation
 * lives in the tasks row; dereference via GET /api/tasks/{task_id}). */
export interface TwinReadyPayload {
  run_id?: string;
  workflow_id?: string;
  locale?: string;
  work_date?: string;
  task_id?: string;
  summary?: string;
  text_bytes?: number;
}

// ------------------------------------------------------------- plumbing ----

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  // The SPA GET catch-all serves index.html for ANY unmatched path, /api/*
  // included — an unmounted API router answers 200 text/html, not 404. Map
  // that to a 404 so "接口未启用" fallbacks fire instead of a JSON parse error.
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("text/html")) {
    throw new ApiError(404, "接口未部署（请求被 SPA fallback 接住，返回了 HTML）");
  }
  return (await res.json()) as T;
}

async function reqText(path: string): Promise<string> {
  const res = await fetch(BASE + path);
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  return await res.text();
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return req<T>(path, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

function put<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return req<T>(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

// ------------------------------------------------------------ endpoints ----

// meta
export const getHealth = () => req<Health>("/health");
export const getMeta = () => req<Meta>("/api/meta");
export const getAdminState = () => req<Record<string, string>>("/api/admin/state");
export const setMaintenance = (paused: boolean) =>
  post<{ paused: boolean }>("/api/admin/maintenance", { paused });
export const getCronHealth = () => req<CronHealth>("/api/cron/health");

/** admin_state.maintenance holds JSON '{"paused": bool}' (app/institute/scheduler.py). */
export function isMaintenancePaused(state: Record<string, string> | null): boolean {
  const raw = state?.["maintenance"];
  if (!raw) return false;
  try {
    return Boolean((JSON.parse(raw) as { paused?: boolean }).paused);
  } catch {
    return false; // corrupt state means not paused — mirrors the backend
  }
}

/** admin_state 'bilingual:enabled' holds JSON true/false (app/institute/
 * bilingual.py). Missing/corrupt row = OFF — mirrors bilingual.is_enabled. */
export function isBilingualEnabled(state: Record<string, string> | null): boolean {
  const raw = state?.["bilingual:enabled"];
  if (!raw) return false;
  try {
    return JSON.parse(raw) === true;
  } catch {
    return false;
  }
}

// events
export const listEvents = (since = 0, types?: string, limit = 200) =>
  req<BusEvent[]>(`/api/events${qs({ since, types, limit })}`);

// tasks
export const listTasks = (filters: {
  status?: string;
  hand?: string;
  source?: string;
  session_id?: string;
  run_id?: string;
  limit?: number;
}) => req<TaskRow[]>(`/api/tasks${qs(filters)}`);
export const getTask = (id: string) => req<Task>(`/api/tasks/${id}`);
export const cancelTask = (id: string) => post<{ cancelled: boolean }>(`/api/tasks/${id}/cancel`);
export const getQueueStats = () => req<QueueStats>("/api/tasks/queue");

// analysts
export const listAnalysts = () => req<Analyst[]>("/api/analysts");
export const getAnalystRoles = () => req<AnalystRoles>("/api/analysts/roles");
export const createAnalyst = (body: AnalystInput) => post<Analyst>("/api/analysts", body);
export const updateAnalyst = (id: string, body: AnalystInput) =>
  put<Analyst>(`/api/analysts/${encodeURIComponent(id)}`, body);
export const deleteAnalyst = (id: string) =>
  req<{ deleted: string }>(`/api/analysts/${encodeURIComponent(id)}`, { method: "DELETE" });
export const runAnalystDaily = (id: string) =>
  post<{ started: string }>(`/api/analysts/${encodeURIComponent(id)}/daily/run`);
export const runAllAnalystDailies = () => post<{ started: string }>("/api/analysts/daily/run-now");
export const getAnalystDailyStatus = (date?: string) =>
  req<AnalystDailyStatus>(`/api/analysts/daily/status${qs({ date })}`);

// workflows
export const listWorkflows = () => req<Workflow[]>("/api/workflows");
export const getWorkflow = (id: string) => req<Workflow>(`/api/workflows/${id}`);
export const runWorkflow = (id: string, variables?: Record<string, string>) =>
  post<{ run_id: string }>(`/api/workflows/${id}/run`, { variables: variables ?? null });
export const listRuns = (filters: { workflow_id?: string; status?: string; limit?: number } = {}) =>
  req<WorkflowRun[]>(`/api/workflows/runs/recent${qs(filters)}`);
export const getRun = (runId: string) => req<WorkflowRun>(`/api/workflows/runs/${runId}`);
export const cancelRun = (runId: string) =>
  post<{ cancelled: boolean }>(`/api/workflows/runs/${runId}/cancel`);
export const runBriefingNow = () =>
  post<{ run_id: string | null; skipped: boolean }>("/api/workflows/daily/briefing/run-now");
export const runDailyNow = () =>
  post<{ run_id: string | null; skipped: boolean }>("/api/workflows/daily/daily/run-now");

// whiteboard
export const listBoards = (status?: string, limit = 50) =>
  req<Board[]>(`/api/whiteboard/boards${qs({ status, limit })}`);
export const getBoard = (id: string) => req<BoardDetail>(`/api/whiteboard/boards/${id}`);
export const createBoard = (topic: string, question: string, max_cards: number) =>
  post<BoardDetail>("/api/whiteboard/boards", { topic, question, max_cards });
export const stopBoard = (id: string) =>
  post<{ stopped: boolean; board: BoardDetail }>(`/api/whiteboard/boards/${id}/stop`);
export const whiteboardTick = () => post<{ ok: boolean }>("/api/whiteboard/tick");
export const whiteboardKickoff = () => post<{ board_id: string | null }>("/api/whiteboard/kickoff");
export const listTopics = (status: string | "" = "pending") =>
  req<Topic[]>(`/api/whiteboard/topics${qs({ status })}`);
export const addTopic = (topic: string, question: string, score = 1.0) =>
  post<Topic>("/api/whiteboard/topics", { topic, question, score });
export const expireTopic = (id: number) =>
  req<{ expired: boolean }>(`/api/whiteboard/topics/${id}`, { method: "DELETE" });

// mailbox
export const listThreads = (status?: string, limit = 50) =>
  req<MailThread[]>(`/api/mailbox/threads${qs({ status, limit })}`);
export const getThread = (id: string) => req<MailThreadDetail>(`/api/mailbox/threads/${id}`);
export const createThread = (subject: string, analyst_id: string, body: string) =>
  post<MailThreadDetail>("/api/mailbox/threads", { subject, analyst_id, body });
export const replyThread = (id: string, body: string) =>
  post<MailThreadDetail>(`/api/mailbox/threads/${id}/reply`, { body });
export const closeThread = (id: string) =>
  post<MailThreadDetail>(`/api/mailbox/threads/${id}/close`);
export const mailboxSweep = () => post<{ ok: boolean }>("/api/mailbox/sweep");

// research
export const listResearchQueue = (status?: string, limit = 100) =>
  req<ResearchItem[]>(`/api/research/queue${qs({ status, limit })}`);
export const getResearchItem = (id: string) => req<ResearchItemDetail>(`/api/research/queue/${id}`);
export const enqueueResearch = (topic: string, priority = 0) =>
  post<EnqueueResult>("/api/research/queue", { topic, priority });
export const cancelResearchItem = (id: string) =>
  post<ResearchItem>(`/api/research/queue/${id}/cancel`);
export const researchTick = () => post<{ processed: string | null }>("/api/research/tick");
export const getResearchLog = (limit = 50) =>
  req<ResearchLogRow[]>(`/api/research/log${qs({ limit })}`);

// research trees (Phase 7 explore mode — same /api/research prefix, disjoint paths)
export const listTrees = (status?: string, limit = 50) =>
  req<ResearchTree[]>(`/api/research/trees${qs({ status, limit })}`);
export const getTree = (id: string) => req<ResearchTreeDetail>(`/api/research/tree/${id}`);
export const createTree = (root_topic: string, max_depth = 2, max_nodes = 12) =>
  post<CreateTreeResult>("/api/research/tree", { root_topic, max_depth, max_nodes });
export const stopTree = (id: string) => post<ResearchTreeDetail>(`/api/research/tree/${id}/stop`);
export const retryTreeNode = (treeId: string, nodeId: string) =>
  post<ResearchTreeDetail>(`/api/research/tree/${treeId}/node/${nodeId}/retry`);

// projects (Phase 7 containers)
export const listProjects = (status?: string, limit = 100) =>
  req<Project[]>(`/api/projects${qs({ status, limit })}`);
export const getProject = (id: string) => req<ProjectDetail>(`/api/projects/${id}`);
export const createProject = (name: string, description = "") =>
  post<Project>("/api/projects", { name, description });

/** GET /api/projects/{id}/digest.md — text/markdown. Own fetch (not reqText):
 * the SPA GET catch-all answers 200 text/html when the router is unmounted,
 * which must surface as 接口未启用, not render as a markdown page. */
export async function getProjectDigest(id: string): Promise<string> {
  const res = await fetch(`${BASE}/api/projects/${id}/digest.md`);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("text/html")) {
    throw new ApiError(404, "接口未部署（请求被 SPA fallback 接住，返回了 HTML）");
  }
  return await res.text();
}

/** POST /api/projects/{id}/archive — a PATCH-NOTES-D5 follow-up endpoint;
 * callers must catch ApiError 404/405/501 (未部署) and 409 (已归档). */
export const archiveProject = (id: string) => post<Project>(`/api/projects/${id}/archive`);

// sessions
export const listSessions = (kind?: string, limit = 100) =>
  req<Session[]>(`/api/sessions${qs({ kind, limit })}`);
export const getSession = (id: string) => req<Session>(`/api/sessions/${id}`);
export const listWorkspaceFiles = (sessionId: string) =>
  req<WorkspaceFile[]>(`/api/sessions/${sessionId}/workspace`);
export const readWorkspaceFile = (sessionId: string, path: string) =>
  reqText(`/api/sessions/${sessionId}/workspace/file${qs({ path })}`);

// hands
export const listHands = () => req<HandStatus[]>("/api/hands");
export const clearHandCooldown = (name: string) =>
  post<{ ok: boolean }>(`/api/hands/${encodeURIComponent(name)}/cooldown/clear`);
export const getHandWeights = () => req<HandWeightRow[]>("/api/hands/weights");
export const putHandWeights = (entries: WeightEntry[], replace = false) =>
  put<{ ok: boolean; upserted: number; weights: Record<string, Record<string, number>> }>(
    "/api/hands/weights",
    { entries, replace },
  );
export const getScorecard = (date?: string) =>
  req<Scorecard>(`/api/hands/scorecard${qs({ date })}`);
export const getHandStats = (hours = 24) => req<HandStats>(`/api/hands/stats${qs({ hours })}`);

// vault
export const getVaultStatus = () => req<VaultStatus>("/api/vault/status");
export const vaultDoctor = () => post<Record<string, unknown>>("/api/vault/doctor");

// forecasts
export const listForecasts = (status?: string, thesis_id?: string, limit = 100) =>
  req<Forecast[]>(`/api/forecasts${qs({ status, thesis_id, limit })}`);
export const getForecast = (id: string) => req<Forecast>(`/api/forecasts/${id}`);
export const settleForecast = (id: string) =>
  post<Forecast>(`/api/forecasts/${encodeURIComponent(id)}/settle`);

// paper book (C3, in flight — callers must catch ApiError 404/501: 账本未启用)
export const listBookPositions = (status?: string) =>
  req<BookPosition[]>(`/api/book/positions${qs({ status })}`);
export const getBookNav = (days = 90) => req<BookNavPoint[]>(`/api/book/nav${qs({ days })}`);

// ask
export const askSync = (body: AskBody) => post<Task>("/api/ask", body);

/** Coerce a parsed done.task into AskDoneTask, defaulting missing/wrong-typed
 * fields instead of letting a malformed frame crash or masquerade downstream. */
function coerceDoneTask(raw: unknown): AskDoneTask {
  const t = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  return {
    id: typeof t.id === "string" ? t.id : null,
    status: typeof t.status === "string" ? (t.status as AskDoneTask["status"]) : "failed",
    hand: typeof t.hand === "string" ? t.hand : null,
    exit_code: typeof t.exit_code === "number" ? t.exit_code : null,
    error: typeof t.error === "string" ? t.error : null,
    output: typeof t.output === "string" ? t.output : "",
  };
}

/**
 * POST /api/ask/stream — NDJSON consumer. Calls onFrame per parsed line and
 * resolves with the done-frame task; a stream that ends without a done frame
 * rejects (the backend always sends one, even on failure — an EOF without it
 * means the response is incomplete). The signal aborts the *reading*; the
 * backend keeps running the task.
 */
export async function askStream(
  body: AskBody,
  onFrame: (f: AskStreamFrame) => void,
  signal?: AbortSignal,
): Promise<AskDoneTask> {
  const res = await fetch("/api/ask/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    let detail = res.statusText;
    try {
      const b = await res.json();
      if (b && typeof b.detail === "string") detail = b.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let done: AskDoneTask | null = null;
  // Parses one NDJSON line; returns the done-task if this was the done frame.
  const feed = (line: string): AskDoneTask | null => {
    const trimmed = line.trim();
    if (!trimmed) return null;
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return null; // malformed line — skip
    }
    // runtime guard: JSON.parse gives unknown shapes (null, arrays, {}), a
    // TS cast alone would pass them through as fake frames
    if (parsed === null || typeof parsed !== "object") return null;
    const type = (parsed as { type?: unknown }).type;
    if (type === "done") {
      const task = coerceDoneTask((parsed as { task?: unknown }).task);
      onFrame({ type: "done", task });
      return task;
    }
    const text = (parsed as { text?: unknown }).text;
    if (type === "stdout" || type === "stderr" || type === "status") {
      onFrame({ type, text: typeof text === "string" ? text : "" });
    } else {
      // unknown frame type: surface it as a visible status line, don't drop
      onFrame({ type: "status", text: `[未知帧 ${String(type)}] ${trimmed.slice(0, 200)}` });
    }
    return null;
  };
  try {
    for (;;) {
      const { done: eof, value } = await reader.read();
      if (eof) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        done = feed(buf.slice(0, nl)) ?? done;
        buf = buf.slice(nl + 1);
      }
    }
    done = feed(buf) ?? done; // trailing line without newline
  } finally {
    // release the stream on every exit path (onFrame throw, abort, EOF)
    try {
      await reader.cancel();
    } catch {
      /* already closed/errored */
    }
  }
  if (done === null) {
    throw new Error("流在收到 done 帧前结束——响应不完整，任务结果请到任务页查看");
  }
  return done;
}

// multi-agent compare (C5, in flight — callers must catch ApiError 404/501)
export const runMultiAgent = (agents: string[], prompt: string, mode: string) =>
  post<MultiAgentRun>("/api/multi-agent/run", { agents, prompt, mode });

// operator (Phase 6 — app/api/operator.py; GET callers catch 404/501 → 运维面未启用)
export const listOperatorActions = (filters: { status?: string; kind?: string; limit?: number } = {}) =>
  req<{ actions: OperatorAction[]; count: number }>(`/api/operator/actions${qs(filters)}`);
/** Conditional-claim transition; a lost claim answers 409 (never a double disposition). */
export const patchOperatorAction = (id: number, status: OperatorActionStatus, resolution?: string) =>
  patch<OperatorAction>(`/api/operator/actions/${id}`, { status, resolution: resolution ?? null });
export const getOperatorTriage = () => req<OperatorTriage>("/api/operator/triage");
/** PUT replaces the FULL switch set, compare-and-swap: expectedVersion must
 * match the server's current version (triage feature_switches_version) or the
 * write answers 409 — reload and re-apply. */
export const putFeatureSwitches = (switches: Record<string, boolean>, expectedVersion: number) =>
  put<{ feature_switches: Record<string, boolean>; version: number }>(
    "/api/operator/feature-switches",
    { switches, expected_version: expectedVersion },
  );
/** THE human gate: 409 either when the action is already disposed or when the
 * confidence is below the LIVE floor (backend message carries both values). */
export const approveDisposition = (id: number, note = "") =>
  post<{ approved: number; action: OperatorAction }>(
    `/api/operator/dispositions/${id}/approve`,
    { note },
  );
