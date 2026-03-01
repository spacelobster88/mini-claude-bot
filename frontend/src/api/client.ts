const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body}`);
  }
  return res.json();
}

// Chat
export const listSessions = () => request<Session[]>("/chat/sessions");
export const getSession = (id: string) => request<Message[]>(`/chat/sessions/${id}`);
export const searchMessages = (q: string) => request<SearchResult[]>(`/chat/search?q=${encodeURIComponent(q)}`);

// CRON
export const listCronJobs = () => request<CronJob[]>("/cron");
export const createCronJob = (job: CronJobInput) => request<{ id: number }>("/cron", { method: "POST", body: JSON.stringify(job) });
export const updateCronJob = (id: number, job: Partial<CronJobInput>) => request<CronJob>(`/cron/${id}`, { method: "PUT", body: JSON.stringify(job) });
export const deleteCronJob = (id: number) => request<{ deleted: boolean }>(`/cron/${id}`, { method: "DELETE" });
export const runCronJob = (id: number) => request<{ result: string }>(`/cron/${id}/run`, { method: "POST" });

// Memory
export const listMemories = () => request<Memory[]>("/memory");
export const createMemory = (mem: MemoryInput) => request<{ id: number }>("/memory", { method: "POST", body: JSON.stringify(mem) });
export const deleteMemory = (id: number) => request<{ deleted: boolean }>(`/memory/${id}`, { method: "DELETE" });
export const searchMemories = (q: string) => request<MemorySearchResult[]>(`/memory/search?q=${encodeURIComponent(q)}`);

// Types
export interface Session { session_id: string; started_at: string; last_message_at: string; message_count: number; }
export interface Message { id: number; session_id: string; role: string; content: string; source: string; created_at: string; }
export interface SearchResult extends Message { distance: number; }
export interface CronJob { id: number; name: string; cron_expression: string; command: string; job_type: string; enabled: number; last_run_at: string | null; last_result: string | null; created_at: string; }
export interface CronJobInput { name: string; cron_expression: string; command: string; job_type: string; enabled: boolean; }
export interface Memory { id: number; key: string; content: string; category: string; created_at: string; }
export interface MemoryInput { key: string; content: string; category: string; }
export interface MemorySearchResult extends Memory { distance: number; }
