export interface DashboardMetrics {
  timestamp: string;
  cron_jobs: CronJob[];
  memory: MemorySummary;
  chat: ChatSummary;
  claude_usage: ClaudeUsage;
  system: SystemMetrics;
  db_health?: DbHealth;
  harness?: HarnessSummary;
}

export interface HarnessJob {
  bg_status: string;
  elapsed_seconds: number;
  chain_depth: number;
  project_id: string;
  project_name: string;
  current_phase: string;
  done: number;
  total: number;
  in_progress: number;
  blocked: number;
}

export interface ArchivedProject {
  project_name: string;
  archived_at: string | null;
  tasks_done: number;
  tasks_total: number;
  status: string;
}

export interface HarnessSummary {
  running_jobs: HarnessJob[];
  completed_jobs: HarnessJob[];
  archived_count: number;
  archived_projects?: ArchivedProject[];
}

export interface DbHealth {
  db_size_mb: number;
  chat_embeddings: number;
  memory_embeddings: number;
}

export interface OllamaModel {
  name: string;
  size: string;
}

export interface ServiceStatus {
  name: string;
  type: string;
  running: boolean;
}

export interface CronJob {
  id: number;
  name: string;
  cron_expression: string;
  job_type: string;
  enabled: boolean;
  last_run_at: string | null;
  last_result_preview: string | null;
  timezone: string | null;
}

export interface MemoryItem {
  key: string;
  content: string;
  category: string;
}

export interface MemorySummary {
  count: number;
  categories: Record<string, number>;
  oldest: string | null;
  newest: string | null;
  items?: MemoryItem[];
}

export interface ChatSummary {
  session_count: number;
  message_count: number;
  oldest_message: string | null;
  newest_message: string | null;
}

export interface ClaudeUsage {
  last_computed_date?: string | null;
  total_sessions: number;
  total_messages: number;
  total_requests?: number;
  first_session_date: string | null;
  model_usage: Record<string, ModelUsage>;
  daily_activity: DailyActivity[];
  daily_model_tokens?: DailyModelTokens[];
  hour_counts?: Record<string, number>;
  context_avg?: number;
  context_max?: number;
}

export interface ModelUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  requests?: number;
  web_search_requests?: number;
  cost_usd?: number;
}

export interface DailyActivity {
  date: string;
  messages: number;
  sessions: number;
  tool_calls?: number;
}

export interface DailyModelTokens {
  date: string;
  tokens_by_model: Record<string, number>;
}

export interface SystemMetrics {
  hostname: string;
  cpu: string;
  memory_total_gb: number;
  memory_used_gb: number;
  memory_free_gb: number;
  memory_wired_gb?: number;
  memory_active_gb?: number;
  memory_inactive_gb?: number;
  memory_compressed_gb?: number;
  memory_purgeable_gb?: number;
  cpu_usage_percent: number;
  cpu_idle_percent: number;
  load_avg: number[];
  disk_total_gb: number;
  disk_used_gb: number;
  disk_free_gb: number;
  disk_used_percent: number;
  uptime: string;
}
