import { useState } from "react";

const style = {
  wrapper: { display: "flex", gap: "0.5rem", marginBottom: "1rem" } as const,
  input: {
    flex: 1,
    padding: "0.5rem 0.75rem",
    background: "#1e1e1e",
    border: "1px solid #444",
    borderRadius: 6,
    color: "#e0e0e0",
    fontSize: "0.95rem",
  } as const,
  button: {
    padding: "0.5rem 1rem",
    background: "#2563eb",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: "0.95rem",
  } as const,
};

export default function SearchBar({ onSearch, placeholder }: { onSearch: (q: string) => void; placeholder?: string }) {
  const [query, setQuery] = useState("");
  return (
    <div style={style.wrapper}>
      <input
        style={style.input}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && query.trim() && onSearch(query.trim())}
        placeholder={placeholder ?? "Semantic search..."}
      />
      <button style={style.button} onClick={() => query.trim() && onSearch(query.trim())}>
        Search
      </button>
    </div>
  );
}
