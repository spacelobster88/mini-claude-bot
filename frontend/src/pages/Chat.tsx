import { useEffect, useState } from "react";
import { listSessions, getSession, searchMessages, type Session, type Message, type SearchResult } from "../api/client";
import SearchBar from "../components/SearchBar";
import ChatMessage from "../components/ChatMessage";

export default function Chat() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null);
  const [activeSession, setActiveSession] = useState<string | null>(null);

  useEffect(() => { listSessions().then(setSessions).catch(() => {}); }, []);

  const openSession = async (id: string) => {
    setActiveSession(id);
    setSearchResults(null);
    const msgs = await getSession(id);
    setMessages(msgs);
  };

  const handleSearch = async (q: string) => {
    setActiveSession(null);
    const results = await searchMessages(q);
    setSearchResults(results);
  };

  return (
    <>
      <h1 style={{ marginBottom: "1rem" }}>Chat History</h1>
      <SearchBar onSearch={handleSearch} placeholder="Semantic search across all chats..." />

      {searchResults && (
        <div>
          <h3 style={{ marginBottom: "0.5rem" }}>Search Results ({searchResults.length})</h3>
          {searchResults.map((r) => (
            <ChatMessage key={r.id} role={r.role} content={r.content} created_at={r.created_at} distance={r.distance} />
          ))}
        </div>
      )}

      {!searchResults && (
        <div style={{ display: "flex", gap: "1.5rem" }}>
          <div style={{ minWidth: 220 }}>
            <h3 style={{ marginBottom: "0.5rem" }}>Sessions</h3>
            {sessions.map((s) => (
              <div
                key={s.session_id}
                onClick={() => openSession(s.session_id)}
                style={{
                  padding: "0.5rem 0.75rem",
                  marginBottom: "0.25rem",
                  borderRadius: 6,
                  cursor: "pointer",
                  background: activeSession === s.session_id ? "#2563eb22" : "transparent",
                  border: activeSession === s.session_id ? "1px solid #2563eb" : "1px solid transparent",
                }}
              >
                <div style={{ fontSize: "0.85rem", fontWeight: 600 }}>{s.session_id.slice(0, 12)}...</div>
                <div style={{ fontSize: "0.75rem", color: "#888" }}>{s.message_count} msgs</div>
              </div>
            ))}
            {sessions.length === 0 && <div style={{ color: "#666" }}>No sessions yet</div>}
          </div>

          <div style={{ flex: 1 }}>
            {activeSession && messages.map((m) => (
              <ChatMessage key={m.id} role={m.role} content={m.content} created_at={m.created_at} />
            ))}
            {!activeSession && <div style={{ color: "#666" }}>Select a session or search</div>}
          </div>
        </div>
      )}
    </>
  );
}
