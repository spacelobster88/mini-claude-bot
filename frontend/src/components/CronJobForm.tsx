import { useState } from "react";
import type { CronJobInput } from "../api/client";

const fieldStyle = {
  display: "flex",
  flexDirection: "column" as const,
  gap: "0.25rem",
  marginBottom: "0.75rem",
};
const inputStyle = {
  padding: "0.5rem 0.75rem",
  background: "#1e1e1e",
  border: "1px solid #444",
  borderRadius: 6,
  color: "#e0e0e0",
  fontSize: "0.9rem",
};
const btnStyle = {
  padding: "0.5rem 1rem",
  background: "#2563eb",
  color: "#fff",
  border: "none",
  borderRadius: 6,
  cursor: "pointer",
};

interface Props {
  onSubmit: (job: CronJobInput) => void;
  initial?: Partial<CronJobInput>;
}

export default function CronJobForm({ onSubmit, initial }: Props) {
  const [name, setName] = useState(initial?.name ?? "");
  const [cron, setCron] = useState(initial?.cron_expression ?? "");
  const [command, setCommand] = useState(initial?.command ?? "");
  const [jobType, setJobType] = useState(initial?.job_type ?? "shell");

  const handleSubmit = () => {
    if (!name || !cron || !command) return;
    onSubmit({ name, cron_expression: cron, command, job_type: jobType, enabled: true });
    setName(""); setCron(""); setCommand("");
  };

  return (
    <div style={{ background: "#161616", padding: "1rem", borderRadius: 8, marginBottom: "1rem" }}>
      <div style={fieldStyle}>
        <label>Name</label>
        <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="daily-report" />
      </div>
      <div style={fieldStyle}>
        <label>Cron Expression</label>
        <input style={inputStyle} value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 9 * * *" />
      </div>
      <div style={fieldStyle}>
        <label>Command</label>
        <input style={inputStyle} value={command} onChange={(e) => setCommand(e.target.value)} placeholder="echo hello" />
      </div>
      <div style={fieldStyle}>
        <label>Type</label>
        <select style={inputStyle} value={jobType} onChange={(e) => setJobType(e.target.value)}>
          <option value="shell">Shell</option>
          <option value="claude">Claude Prompt</option>
        </select>
      </div>
      <button style={btnStyle} onClick={handleSubmit}>Save Job</button>
    </div>
  );
}
