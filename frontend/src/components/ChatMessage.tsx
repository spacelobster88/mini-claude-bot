const msgStyle = (role: string) => ({
  padding: "0.75rem 1rem",
  marginBottom: "0.5rem",
  borderRadius: 8,
  background: role === "user" ? "#1a2733" : "#1e1e1e",
  borderLeft: `3px solid ${role === "user" ? "#2563eb" : "#10b981"}`,
  whiteSpace: "pre-wrap" as const,
  fontSize: "0.9rem",
  lineHeight: 1.5,
});

const metaStyle = { fontSize: "0.75rem", color: "#888", marginBottom: "0.25rem" };

interface Props {
  role: string;
  content: string;
  created_at: string;
  distance?: number;
}

export default function ChatMessage({ role, content, created_at, distance }: Props) {
  return (
    <div style={msgStyle(role)}>
      <div style={metaStyle}>
        <strong>{role}</strong> &middot; {new Date(created_at).toLocaleString()}
        {distance !== undefined && <span> &middot; similarity: {(1 - distance).toFixed(3)}</span>}
      </div>
      {content}
    </div>
  );
}
