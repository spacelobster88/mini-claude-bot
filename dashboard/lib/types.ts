export interface DashboardMetrics {
  timestamp: string;
  cron_jobs: CronJob[];
  memory: MemorySummary;
  chat: ChatSummary;
  claude_usage: ClaudeUsage;
  system: SystemMetrics;
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

export interface MemorySummary {
  count: number;
  categories: Record<string, number>;
  oldest: string | null;
  newest: string | null;
}

export interface ChatSummary {
  session_count: number;
  message_count: number;
  oldest_message: string | null;
  newest_message: string | null;
}

export interface ClaudeUsage {
  last_computed_date: string | null;
  total_sessions: number;
  total_messages: number;
  first_session_date: string | null;
  model_usage: Record<string, ModelUsage>;
  daily_activity: DailyActivity[];
  daily_model_tokens: DailyModelTokens[];
  hour_counts: Record<string, number>;
}

export interface ModelUsage {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  web_search_requests: number;
  cost_usd: number;
}

export interface DailyActivity {
  date: string;
  messages: number;
  sessions: number;
  tool_calls: number;
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
  cpu_usage_percent: number;
  cpu_idle_percent: number;
  load_avg: number[];
  disk_total_gb: number;
  disk_used_gb: number;
  disk_free_gb: number;
  disk_used_percent: number;
  uptime: string;
}
