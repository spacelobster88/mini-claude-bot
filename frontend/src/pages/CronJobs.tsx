import { useEffect, useState } from "react";
import { listCronJobs, createCronJob, deleteCronJob, runCronJob, updateCronJob, type CronJob, type CronJobInput } from "../api/client";
import CronJobForm from "../components/CronJobForm";

const cardStyle = {
  background: "#161616",
  padding: "1rem",
  borderRadius: 8,
  marginBottom: "0.75rem",
  border: "1px solid #333",
};
const btnSmall = (color: string) => ({
  padding: "0.3rem 0.6rem",
  background: color,
  color: "#fff",
  border: "none",
  borderRadius: 4,
  cursor: "pointer",
  fontSize: "0.8rem",
  marginRight: "0.5rem",
});

export default function CronJobs() {
  const [jobs, setJobs] = useState<CronJob[]>([]);
  const [showForm, setShowForm] = useState(false);
  const [runResult, setRunResult] = useState<{ id: number; result: string } | null>(null);

  const refresh = () => listCronJobs().then(setJobs).catch(() => {});
  useEffect(() => { refresh(); }, []);

  const handleCreate = async (job: CronJobInput) => {
    await createCronJob(job);
    setShowForm(false);
    refresh();
  };

  const handleDelete = async (id: number) => {
    await deleteCronJob(id);
    refresh();
  };

  const handleRun = async (id: number) => {
    const res = await runCronJob(id);
    setRunResult({ id, result: res.result });
  };

  const handleToggle = async (job: CronJob) => {
    await updateCronJob(job.id, { enabled: !job.enabled });
    refresh();
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <h1>CRON Jobs</h1>
        <button style={btnSmall("#2563eb")} onClick={() => setShowForm(!showForm)}>
          {showForm ? "Cancel" : "+ New Job"}
        </button>
      </div>

      {showForm && <CronJobForm onSubmit={handleCreate} />}

      {jobs.map((j) => (
        <div key={j.id} style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <div>
              <strong>{j.name}</strong>
              <span style={{ color: "#888", marginLeft: "0.75rem", fontSize: "0.85rem" }}>
                {j.cron_expression} &middot; {j.job_type}
              </span>
            </div>
            <span style={{ color: j.enabled ? "#10b981" : "#ef4444", fontSize: "0.85rem" }}>
              {j.enabled ? "enabled" : "disabled"}
            </span>
          </div>
          <div style={{ fontSize: "0.85rem", color: "#aaa", margin: "0.25rem 0" }}>
            <code>{j.command}</code>
          </div>
          {j.last_run_at && (
            <div style={{ fontSize: "0.75rem", color: "#666" }}>
              Last run: {new Date(j.last_run_at).toLocaleString()}
            </div>
          )}
          <div style={{ marginTop: "0.5rem" }}>
            <button style={btnSmall("#10b981")} onClick={() => handleRun(j.id)}>Run Now</button>
            <button style={btnSmall("#6b7280")} onClick={() => handleToggle(j)}>
              {j.enabled ? "Disable" : "Enable"}
            </button>
            <button style={btnSmall("#ef4444")} onClick={() => handleDelete(j.id)}>Delete</button>
          </div>
          {runResult?.id === j.id && (
            <pre style={{ marginTop: "0.5rem", padding: "0.5rem", background: "#0f0f0f", borderRadius: 4, fontSize: "0.8rem", maxHeight: 200, overflow: "auto" }}>
              {runResult.result}
            </pre>
          )}
        </div>
      ))}

      {jobs.length === 0 && !showForm && <div style={{ color: "#666" }}>No jobs yet. Create one to get started.</div>}
    </>
  );
}
