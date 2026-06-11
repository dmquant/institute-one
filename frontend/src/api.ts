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

// vault
export const getVaultStatus = () => req<VaultStatus>("/api/vault/status");
export const vaultDoctor = () => post<Record<string, unknown>>("/api/vault/doctor");
