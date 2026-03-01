"use client";

import { useEffect, useState } from "react";
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

function ProgressBar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  const barColor = pct > 85 ? "bg-red-500" : pct > 60 ? "bg-yellow-500" : color;
  return (
    <div className="w-full bg-gray-800 rounded-full h-2.5">
      <div className={`${barColor} h-2.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
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

  const { system: sys, claude_usage: claude, cron_jobs: jobs, memory: mem, chat } = data;
  const lastPush = data._last_push;
  const isOnline = lastPush ? Date.now() - new Date(lastPush).getTime() < 600000 : false;

  const totalTokens = Object.values(claude.model_usage).reduce(
    (sum, m) => sum + m.input_tokens + m.output_tokens + m.cache_read_tokens + m.cache_creation_tokens,
    0
  );

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
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span>CPU</span>
                <span className="text-gray-400">{sys.cpu_usage_percent}%</span>
              </div>
              <ProgressBar value={sys.cpu_usage_percent} max={100} color="bg-blue-500" />
            </div>
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span>Memory</span>
                <span className="text-gray-400">{sys.memory_used_gb}G / {sys.memory_total_gb}G</span>
              </div>
              <ProgressBar value={sys.memory_used_gb} max={sys.memory_total_gb} color="bg-purple-500" />
            </div>
            <div>
              <div className="flex justify-between text-sm mb-1">
                <span>Disk</span>
                <span className="text-gray-400">{sys.disk_used_gb}G / {sys.disk_total_gb}G</span>
              </div>
              <ProgressBar value={sys.disk_used_gb} max={sys.disk_total_gb} color="bg-emerald-500" />
            </div>
            <div className="flex justify-between text-xs text-gray-500 pt-1">
              <span>Load: {sys.load_avg?.join(" / ")}</span>
              <span>Up: {sys.uptime}</span>
            </div>
          </div>
        </Card>

        {/* Claude Usage */}
        <Card title="Claude Usage">
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="bg-gray-800 rounded-lg p-3 text-center">
                <div className="text-xl font-bold text-blue-400">{fmt(totalTokens)}</div>
                <div className="text-xs text-gray-500">Total Tokens</div>
              </div>
              <div className="bg-gray-800 rounded-lg p-3 text-center">
                <div className="text-xl font-bold text-emerald-400">{claude.total_sessions}</div>
                <div className="text-xs text-gray-500">Sessions</div>
              </div>
            </div>
            {Object.entries(claude.model_usage).map(([model, u]) => (
              <div key={model} className="text-xs space-y-1 bg-gray-800 rounded-lg p-3">
                <div className="font-semibold text-gray-300">{model}</div>
                <div className="grid grid-cols-2 gap-1 text-gray-500">
                  <span>In: {fmt(u.input_tokens)}</span>
                  <span>Out: {fmt(u.output_tokens)}</span>
                  <span>Cache Read: {fmt(u.cache_read_tokens)}</span>
                  <span>Cache Write: {fmt(u.cache_creation_tokens)}</span>
                </div>
              </div>
            ))}
            <div className="text-xs text-gray-600">Stats from: {claude.last_computed_date || "n/a"}</div>
          </div>
        </Card>

        {/* Scheduled Jobs */}
        <Card title="Scheduled Jobs">
          <div className="space-y-2">
            {jobs.map((job) => (
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
          </div>
        </Card>

        {/* Memory */}
        <Card title="Memory Store">
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
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(mem.categories).map(([cat, count]) => (
              <span key={cat} className="text-xs bg-gray-800 text-gray-400 px-2 py-1 rounded-full">
                {cat}: {count}
              </span>
            ))}
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

        {/* Daily Activity */}
        <Card title="Daily Activity">
          {claude.daily_activity.length > 0 ? (
            <div className="space-y-1.5">
              {claude.daily_activity.slice(-7).map((day) => {
                const maxMsg = Math.max(...claude.daily_activity.map((d) => d.messages), 1);
                return (
                  <div key={day.date} className="flex items-center gap-2 text-xs">
                    <span className="text-gray-500 w-20 shrink-0">{day.date}</span>
                    <div className="flex-1 bg-gray-800 rounded-full h-3">
                      <div className="bg-blue-600 h-3 rounded-full" style={{ width: `${(day.messages / maxMsg) * 100}%` }} />
                    </div>
                    <span className="text-gray-500 w-12 text-right">{day.messages}</span>
                  </div>
                );
              })}
              <div className="text-xs text-gray-600 pt-1">Messages per day (last 7)</div>
            </div>
          ) : (
            <div className="text-sm text-gray-600">No activity data</div>
          )}
        </Card>
      </div>

      <div className="text-center text-xs text-gray-700 mt-8">
        mini-claude-bot &middot; Refreshes every 30s &middot; Heartbeat every 5min
      </div>
    </main>
  );
}
