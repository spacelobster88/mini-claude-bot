import { useEffect, useState } from "react";
import { listMemories, createMemory, deleteMemory, searchMemories, type Memory, type MemorySearchResult } from "../api/client";
import SearchBar from "../components/SearchBar";

const cardStyle = {
  background: "#161616",
  padding: "1rem",
  borderRadius: 8,
  marginBottom: "0.5rem",
  border: "1px solid #333",
};
const inputStyle = {
  padding: "0.5rem 0.75rem",
  background: "#1e1e1e",
  border: "1px solid #444",
  borderRadius: 6,
  color: "#e0e0e0",
  fontSize: "0.9rem",
  width: "100%",
  marginBottom: "0.5rem",
};
const btnStyle = {
  padding: "0.4rem 0.8rem",
  background: "#2563eb",
  color: "#fff",
  border: "none",
  borderRadius: 4,
  cursor: "pointer",
  fontSize: "0.85rem",
};

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([]);
  const [searchResults, setSearchResults] = useState<MemorySearchResult[] | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [key, setKey] = useState("");
  const [content, setContent] = useState("");
  const [category, setCategory] = useState("general");

  const refresh = () => { listMemories().then(setMemories).catch(() => {}); setSearchResults(null); };
  useEffect(() => { refresh(); }, []);

  const handleCreate = async () => {
    if (!key || !content) return;
    await createMemory({ key, content, category });
    setKey(""); setContent(""); setCategory("general"); setShowForm(false);
    refresh();
  };

  const handleSearch = async (q: string) => {
    const results = await searchMemories(q);
    setSearchResults(results);
  };

  const handleDelete = async (id: number) => {
    await deleteMemory(id);
    refresh();
  };

  const display = searchResults ?? memories;

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "1rem" }}>
        <h1>Memory</h1>
        <button style={btnStyle} onClick={() => setShowForm(!showForm)}>
          {showForm ? "Cancel" : "+ New Memory"}
        </button>
      </div>

      <SearchBar onSearch={handleSearch} placeholder="Semantic search memories..." />
      {searchResults && (
        <button style={{ ...btnStyle, background: "#6b7280", marginBottom: "1rem" }} onClick={refresh}>
          Clear search
        </button>
      )}

      {showForm && (
        <div style={{ ...cardStyle, marginBottom: "1rem" }}>
          <input style={inputStyle} value={key} onChange={(e) => setKey(e.target.value)} placeholder="Key (unique identifier)" />
          <textarea
            style={{ ...inputStyle, minHeight: 80, resize: "vertical" }}
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="Content"
          />
          <input style={inputStyle} value={category} onChange={(e) => setCategory(e.target.value)} placeholder="Category" />
          <button style={btnStyle} onClick={handleCreate}>Save Memory</button>
        </div>
      )}

      {display.map((m) => (
        <div key={m.id} style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between" }}>
            <div>
              <strong>{m.key}</strong>
              <span style={{ color: "#888", marginLeft: "0.75rem", fontSize: "0.8rem" }}>{m.category}</span>
              {"distance" in m && (
                <span style={{ color: "#6cb4ee", marginLeft: "0.75rem", fontSize: "0.8rem" }}>
                  similarity: {(1 - (m as MemorySearchResult).distance).toFixed(3)}
                </span>
              )}
            </div>
            <button style={{ ...btnStyle, background: "#ef4444", fontSize: "0.75rem", padding: "0.2rem 0.5rem" }} onClick={() => handleDelete(m.id)}>
              Delete
            </button>
          </div>
          <div style={{ marginTop: "0.25rem", fontSize: "0.9rem", whiteSpace: "pre-wrap" }}>{m.content}</div>
        </div>
      ))}

      {display.length === 0 && <div style={{ color: "#666" }}>No memories yet.</div>}
    </>
  );
}
