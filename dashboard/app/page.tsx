"use client";

import { useEffect, useState, useCallback } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import type { DashboardMetrics } from "@/lib/types";

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function fmt(n: number): string {
  return n.toLocaleString();
}

function thresholdColor(pct: number, normalBar: string, normalText: string): { bar: string; text: string } {
  if (pct > 85) return { bar: "bg-red-500", text: "text-red-400" };
  if (pct > 60) return { bar: "bg-yellow-500", text: "text-yellow-400" };
  return { bar: normalBar, text: normalText };
}

function ProgressBar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const barColor = pct > 85 ? "bg-red-500" : pct > 60 ? "bg-yellow-500" : color;
  return (
    <div className="w-full bg-gray-800 rounded-full h-2.5">
      <div className={`${barColor} h-2.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function MemoryBar({ sys }: { sys: DashboardMetrics["system"] }) {
  const total = sys.memory_total_gb;
  if (!total) return null;
  const wired = sys.memory_wired_gb ?? 0;
  const active = sys.memory_active_gb ?? 0;
  const compressed = sys.memory_compressed_gb ?? 0;
  const inactive = sys.memory_inactive_gb ?? 0;
  const pct = (v: number) => Math.min((v / total) * 100, 100);
  const pressure = wired + active;

  const pressurePct = total > 0 ? (pressure / total) * 100 : 0;
  const pressureTextColor = pressurePct > 85 ? "text-red-400" : pressurePct > 60 ? "text-yellow-400" : "text-purple-400";

  return (
    <div>
      <div className="flex justify-between text-sm mb-1">
        <span>Memory</span>
        <span className={pressureTextColor}>{pressure.toFixed(1)}G pressure / {total}G</span>
      </div>
      <div className="w-full bg-gray-800 rounded-full h-3 flex overflow-hidden">
        <div className="bg-red-500 h-3 transition-all" style={{ width: `${pct(wired)}%` }} title={`Wired: ${wired.toFixed(1)}G`} />
        <div className="bg-orange-500 h-3 transition-all" style={{ width: `${pct(active)}%` }} title={`Active: ${active.toFixed(1)}G`} />
        <div className="bg-yellow-500/60 h-3 transition-all" style={{ width: `${pct(compressed)}%` }} title={`Compressed: ${compressed.toFixed(1)}G`} />
        <div className="bg-gray-600/40 h-3 transition-all" style={{ width: `${pct(inactive)}%` }} title={`Inactive: ${inactive.toFixed(1)}G`} />
      </div>
      <div className="flex gap-3 text-[10px] text-gray-500 mt-1.5 flex-wrap">
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-red-500 inline-block" />Wired {wired.toFixed(1)}G</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-orange-500 inline-block" />Active {active.toFixed(1)}G</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-yellow-500/60 inline-block" />Compressed {compressed.toFixed(1)}G</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-gray-600/40 inline-block" />Inactive {inactive.toFixed(1)}G</span>
      </div>
    </div>
  );
}

function HarnessTabList({ harness }: { harness: DashboardMetrics["harness"] }) {
  const [activeTab, setActiveTab] = useState<"completed" | "archived" | null>(null);
  if (!harness) return null;

  const completed = harness.completed_jobs || [];
  const archived = harness.archived_projects || [];

  const toggle = (tab: "completed" | "archived") => {
    setActiveTab(activeTab === tab ? null : tab);
  };

  return (
    <div className="pt-2 border-t border-gray-800">
      <div className="grid grid-cols-2 gap-2">
        <button onClick={() => toggle("completed")} className={`rounded-lg p-2.5 text-center transition-colors ${activeTab === "completed" ? "bg-emerald-900/30 ring-1 ring-emerald-700" : "bg-gray-800 hover:bg-gray-750"}`}>
          <div className="text-lg font-bold text-emerald-400">{completed.length}</div>
          <div className="text-[10px] text-gray-500">Completed</div>
        </button>
        <button onClick={() => toggle("archived")} className={`rounded-lg p-2.5 text-center transition-colors ${activeTab === "archived" ? "bg-gray-700/50 ring-1 ring-gray-600" : "bg-gray-800 hover:bg-gray-750"}`}>
          <div className="text-lg font-bold text-gray-400">{harness.archived_count}</div>
          <div className="text-[10px] text-gray-500">Archived</div>
        </button>
      </div>

      {activeTab === "completed" && completed.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {completed.map((job) => (
            <div key={job.project_id} className="bg-gray-800/60 rounded-lg px-3 py-2 flex items-center justify-between">
              <div className="min-w-0">
                <div className="text-xs text-gray-200 truncate">{job.project_name}</div>
                <div className="text-[10px] text-gray-500">{job.done}/{job.total} tasks</div>
              </div>
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-900/50 text-emerald-400 shrink-0 ml-2">complete</span>
            </div>
          ))}
        </div>
      )}
      {activeTab === "completed" && completed.length === 0 && (
        <div className="text-xs text-gray-600 mt-2">No completed projects</div>
      )}

      {activeTab === "archived" && archived.length > 0 && (
        <div className="mt-2 space-y-1.5">
          {[...archived].reverse().map((p, i) => (
            <div key={i} className="bg-gray-800/60 rounded-lg px-3 py-2 flex items-center justify-between">
              <div className="min-w-0">
                <div className="text-xs text-gray-200 truncate">{p.project_name}</div>
                <div className="text-[10px] text-gray-500">
                  {p.tasks_done}/{p.tasks_total} tasks
                  {p.archived_at && <span> &middot; {timeAgo(p.archived_at)}</span>}
                </div>
              </div>
              {p.archived_at && (
                <div className="text-[10px] text-gray-500 shrink-0 ml-2">{new Date(p.archived_at).toLocaleDateString()}</div>
              )}
            </div>
          ))}
        </div>
      )}
      {activeTab === "archived" && archived.length === 0 && (
        <div className="text-xs text-gray-600 mt-2">No archived projects</div>
      )}
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">{title}</h2>
      {children}
    </div>
  );
}

export default function Dashboard() {
  const [data, setData] = useState<(DashboardMetrics & { _last_push?: string }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedCat, setExpandedCat] = useState<string | null>(null);
  const [jobPage, setJobPage] = useState(0);
  const jobsPerPage = 5;

  const fetchMetrics = async () => {
    try {
      const res = await fetch("/api/metrics");
      if (res.status === 404) {
        setError("Waiting for first heartbeat from Mac mini...");
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMetrics();
    const interval = setInterval(fetchMetrics, 30000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-gray-500 text-lg">Loading...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="text-gray-500 text-lg mb-2">{error || "No data"}</div>
          <div className="text-gray-600 text-sm">The Mac mini pushes metrics every 5 minutes.</div>
        </div>
      </div>
    );
  }

  const { system: sys, claude_usage: claude, cron_jobs: jobs, memory: mem, chat, db_health: dbh, harness } = data;
  const lastPush = data._last_push;
  const isOnline = lastPush ? Date.now() - new Date(lastPush).getTime() < 600000 : false;

  const totalMessages = claude.total_messages || 0;

  return (
    <main className="min-h-screen p-4 md:p-8 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold">Mac Mini Dashboard</h1>
          <p className="text-gray-500 text-sm">{sys.hostname} &middot; {sys.cpu}</p>
        </div>
        <div className="flex items-center gap-3">
          <div className={`w-2.5 h-2.5 rounded-full ${isOnline ? "bg-green-500 animate-pulse" : "bg-red-500"}`} />
          <span className="text-sm text-gray-400">
            {isOnline ? "Online" : "Offline"} &middot; {timeAgo(lastPush ?? null)}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {/* System */}
        <Card title="System">
          <div className="space-y-3">
            {(() => { const c = thresholdColor(sys.cpu_usage_percent, "bg-blue-500", "text-blue-400"); return (
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span>CPU</span>
                <span className={c.text}>{sys.cpu_usage_percent}%</span>
              </div>
              <ProgressBar value={sys.cpu_usage_percent} max={100} color={c.bar} />
            </div>
            ); })()}
            {sys.memory_wired_gb != null ? (
              <MemoryBar sys={sys} />
            ) : (() => { const c = thresholdColor(sys.memory_total_gb > 0 ? (sys.memory_used_gb / sys.memory_total_gb) * 100 : 0, "bg-purple-500", "text-purple-400"); return (
              <div>
                <div className="flex justify-between text-sm mb-1">
                  <span>Memory</span>
                  <span className={c.text}>{sys.memory_used_gb}G / {sys.memory_total_gb}G</span>
                </div>
                <ProgressBar value={sys.memory_used_gb} max={sys.memory_total_gb} color={c.bar} />
              </div>
            ); })()}
            {(() => { const c = thresholdColor(sys.disk_total_gb > 0 ? (sys.disk_used_gb / sys.disk_total_gb) * 100 : 0, "bg-emerald-500", "text-emerald-400"); return (
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span>Disk</span>
                <span className={c.text}>{sys.disk_used_gb}G / {sys.disk_total_gb}G</span>
              </div>
              <ProgressBar value={sys.disk_used_gb} max={sys.disk_total_gb} color={c.bar} />
            </div>
            ); })()}
            <div className="flex justify-between text-xs text-gray-500 pt-1">
              <span>Load: {sys.load_avg?.join(" / ")}</span>
              <span>Up: {sys.uptime}</span>
            </div>
          </div>
        </Card>

        {/* Claude Usage */}
        <Card title="Claude Usage">
          <div className="space-y-2">
            <div className="grid grid-cols-3 gap-2">
              <div className="bg-gray-800 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-blue-400">{fmt(totalMessages)}</div>
                <div className="text-xs text-gray-500">Messages</div>
              </div>
              <div className="bg-gray-800 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-emerald-400">{claude.total_sessions}</div>
                <div className="text-xs text-gray-500">Sessions</div>
              </div>
              <div className="bg-gray-800 rounded-lg p-3 text-center">
                <div className="text-lg font-bold text-amber-400">{fmt(claude.total_requests || 0)}</div>
                <div className="text-xs text-gray-500">API Calls</div>
              </div>
            </div>
            <div className="bg-gray-800 rounded-lg p-3">
              <div className="flex justify-between text-xs mb-1">
                <span className="text-gray-400">Context Window</span>
                <span className="text-gray-500">max 200k</span>
              </div>
              <ProgressBar value={claude.context_max || 0} max={200000} color="bg-blue-500" />
              <div className="flex justify-between text-xs text-gray-500 mt-1">
                <span>Avg: {fmt(claude.context_avg || 0)}</span>
                <span>Peak: {fmt(claude.context_max || 0)}</span>
              </div>
            </div>
            {claude.daily_activity.length > 0 && (() => {
              const days = claude.daily_activity.slice(-7).map((d) => ({
                date: d.date.slice(5).replace("-", "/"),
                Messages: d.messages,
                Sessions: d.sessions,
              }));
              return (
                <div className="bg-gray-800 rounded-lg p-3">
                  <div className="text-xs text-gray-400 mb-2">Last 7 Days</div>
                  <ResponsiveContainer width="100%" height={140}>
                    <LineChart data={days}>
                      <XAxis dataKey="date" tick={{ fill: "#6b7280", fontSize: 11 }} axisLine={false} tickLine={false} />
                      <YAxis tick={{ fill: "#6b7280", fontSize: 11 }} axisLine={false} tickLine={false} width={35} />
                      <Tooltip
                        contentStyle={{ background: "#1f2937", border: "1px solid #374151", borderRadius: 8, fontSize: 12 }}
                        itemStyle={{ color: "#d1d5db" }}
                        labelStyle={{ color: "#9ca3af", fontWeight: 600 }}
                      />
                      <Line type="monotone" dataKey="Messages" stroke="#60a5fa" strokeWidth={2} dot={{ r: 3, fill: "#60a5fa" }} />
                      <Line type="monotone" dataKey="Sessions" stroke="#34d399" strokeWidth={2} dot={{ r: 3, fill: "#34d399" }} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              );
            })()}
            {Object.entries(claude.model_usage || {}).map(([model, u]: [string, any]) => (
              <div key={model} className="text-xs space-y-1 bg-gray-800 rounded-lg p-3">
                <div className="flex justify-between">
                  <span className="font-semibold text-gray-300">{model}</span>
                  <span className="text-gray-500">{fmt(u.requests)} reqs</span>
                </div>
                <div className="grid grid-cols-2 gap-1 text-gray-500">
                  <span>In: {fmt(u.input_tokens)}</span>
                  <span>Out: {fmt(u.output_tokens)}</span>
                  <span>Cache Read: {fmt(u.cache_read_tokens)}</span>
                  <span>Cache Write: {fmt(u.cache_creation_tokens)}</span>
                </div>
              </div>
            ))}
            <div className="text-xs text-gray-600">
              {claude.first_session_date && `Since: ${claude.first_session_date}`}
            </div>
          </div>
        </Card>

        {/* Scheduled Jobs */}
        {(() => {
          const totalPages = Math.ceil(jobs.length / jobsPerPage);
          const pagedJobs = jobs.slice(jobPage * jobsPerPage, (jobPage + 1) * jobsPerPage);
          return (
            <Card title={`Scheduled Jobs (${jobs.length})`}>
              <div className="space-y-2">
                {pagedJobs.map((job) => (
                  <div key={job.id} className="bg-gray-800 rounded-lg p-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium truncate mr-2">{job.name}</span>
                      <span className={`text-xs px-2 py-0.5 rounded-full ${job.enabled ? "bg-green-900 text-green-300" : "bg-gray-700 text-gray-400"}`}>
                        {job.enabled ? "on" : "off"}
                      </span>
                    </div>
                    <div className="flex justify-between text-xs text-gray-500">
                      <span className="font-mono">{job.cron_expression}</span>
                      <span>{job.timezone || "system"}</span>
                    </div>
                    {job.last_run_at && (
                      <div className="text-xs text-gray-600 mt-1">Last: {timeAgo(job.last_run_at)}</div>
                    )}
                  </div>
                ))}
                {totalPages > 1 && (
                  <div className="flex items-center justify-between pt-1">
                    <button
                      onClick={() => setJobPage(Math.max(0, jobPage - 1))}
                      disabled={jobPage === 0}
                      className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-400 hover:text-gray-200 disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      Prev
                    </button>
                    <span className="text-xs text-gray-500">{jobPage + 1} / {totalPages}</span>
                    <button
                      onClick={() => setJobPage(Math.min(totalPages - 1, jobPage + 1))}
                      disabled={jobPage >= totalPages - 1}
                      className="text-xs px-2 py-1 rounded bg-gray-800 text-gray-400 hover:text-gray-200 disabled:opacity-30 disabled:cursor-not-allowed"
                    >
                      Next
                    </button>
                  </div>
                )}
              </div>
            </Card>
          );
        })()}

        {/* Memory */}
        <Card title="Memory Store">
          <div className="h-[320px] flex flex-col">
            <div className="flex items-center gap-4 mb-3">
              <div className="bg-gray-800 rounded-lg p-3 text-center flex-1">
                <div className="text-2xl font-bold text-amber-400">{mem.count}</div>
                <div className="text-xs text-gray-500">Memories</div>
              </div>
              <div className="text-xs text-gray-500 flex-1">
                {mem.oldest && <div>From: {new Date(mem.oldest).toLocaleDateString()}</div>}
                {mem.newest && <div>To: {new Date(mem.newest).toLocaleDateString()}</div>}
              </div>
            </div>
            {dbh && (
              <div className="grid grid-cols-3 gap-2 mb-3">
                <div className="bg-gray-800 rounded-lg px-2 py-1.5 text-center">
                  <div className="text-sm font-bold text-amber-400">{dbh.db_size_mb} MB</div>
                  <div className="text-[10px] text-gray-500">SQLite</div>
                </div>
                <div className="bg-gray-800 rounded-lg px-2 py-1.5 text-center">
                  <div className="text-sm font-bold text-purple-400">{dbh.chat_embeddings}</div>
                  <div className="text-[10px] text-gray-500">Chat Vectors</div>
                </div>
                <div className="bg-gray-800 rounded-lg px-2 py-1.5 text-center">
                  <div className="text-sm font-bold text-purple-400">{dbh.memory_embeddings}</div>
                  <div className="text-[10px] text-gray-500">Mem Vectors</div>
                </div>
              </div>
            )}
            <div className="flex flex-wrap gap-1.5 shrink-0">
              {Object.entries(mem.categories).map(([cat, count]) => (
                <button
                  key={cat}
                  onClick={() => setExpandedCat(expandedCat === cat ? null : cat)}
                  className={`text-xs px-2 py-1 rounded-full transition-colors cursor-pointer ${
                    expandedCat === cat
                      ? "bg-amber-600 text-white"
                      : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-300"
                  }`}
                >
                  {cat}: {count}
                </button>
              ))}
            </div>
            {expandedCat && mem.items && (
              <div className="mt-3 space-y-2 overflow-y-auto min-h-0">
                {mem.items
                  .filter((item) => item.category === expandedCat)
                  .map((item) => (
                    <div key={item.key} className="bg-gray-800 rounded-lg p-3">
                      <div className="text-xs font-semibold text-amber-400 mb-1">{item.key}</div>
                      <div className="text-xs text-gray-400 whitespace-pre-wrap">{item.content}</div>
                    </div>
                  ))}
              </div>
            )}
          </div>
        </Card>

        {/* Chat */}
        <Card title="Chat History">
          <div className="grid grid-cols-2 gap-2">
            <div className="bg-gray-800 rounded-lg p-3 text-center">
              <div className="text-2xl font-bold text-cyan-400">{chat.session_count}</div>
              <div className="text-xs text-gray-500">Sessions</div>
            </div>
            <div className="bg-gray-800 rounded-lg p-3 text-center">
              <div className="text-2xl font-bold text-cyan-400">{fmt(chat.message_count)}</div>
              <div className="text-xs text-gray-500">Messages</div>
            </div>
          </div>
          {chat.oldest_message && (
            <div className="text-xs text-gray-600 mt-2">
              Range: {new Date(chat.oldest_message).toLocaleDateString()} &mdash; {chat.newest_message ? new Date(chat.newest_message).toLocaleDateString() : "now"}
            </div>
          )}
        </Card>

        {/* Harness Loops */}
        <Card title="Harness Loops">
          <div className="space-y-3">
            {harness && harness.running_jobs.length > 0 && (
              <div>
                <div className="text-xs text-gray-500 mb-1.5">Active</div>
                <div className="space-y-2">
                  {harness.running_jobs.map((job) => (
                    <div key={job.project_id} className="bg-gray-800 rounded-lg p-3">
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-sm font-medium text-gray-200">{job.project_name}</span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                          job.bg_status === "running" ? "bg-green-900/50 text-green-400" :
                          job.in_progress > 0 ? "bg-yellow-900/50 text-yellow-400" :
                          job.blocked > 0 ? "bg-red-900/50 text-red-400" :
                          "bg-blue-900/50 text-blue-400"
                        }`}>{
                          job.bg_status === "running" ? "running" :
                          job.in_progress > 0 ? "in progress" :
                          job.blocked > 0 ? "blocked" :
                          "idle"
                        }</span>
                      </div>
                      <div className="text-xs text-gray-500 mb-1">
                        Phase: {job.current_phase} &middot; {job.done}/{job.total} tasks
                        {job.chain_depth > 0 && <span> &middot; chain #{job.chain_depth}</span>}
                      </div>
                      <ProgressBar value={job.done} max={job.total} color="bg-emerald-500" />
                      {job.blocked > 0 && (
                        <div className="text-[10px] text-red-400 mt-1">{job.blocked} blocked</div>
                      )}
                      {job.in_progress > 0 && (
                        <div className="text-[10px] text-yellow-400 mt-0.5">{job.in_progress} in progress</div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
            {harness && harness.running_jobs.length === 0 && (
              <div className="text-xs text-gray-600">No active harness loops</div>
            )}
            {harness && <HarnessTabList harness={harness} />}
          </div>
        </Card>
      </div>

      <div className="text-center text-xs text-gray-700 mt-8">
        mini-claude-bot &middot; Refreshes every 30s &middot; Heartbeat every 5min
      </div>
    </main>
  );
}
