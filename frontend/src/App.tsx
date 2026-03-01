import { Routes, Route, NavLink } from "react-router-dom";
import Chat from "./pages/Chat";
import CronJobs from "./pages/CronJobs";
import MemoryPage from "./pages/Memory";

const navStyle = {
  display: "flex",
  gap: "1.5rem",
  padding: "1rem 2rem",
  borderBottom: "1px solid #333",
  background: "#161616",
};

const activeStyle = { color: "#fff", fontWeight: 700 as const };

export default function App() {
  return (
    <>
      <nav style={navStyle}>
        <NavLink to="/" style={({ isActive }) => (isActive ? activeStyle : {})}>
          Chat History
        </NavLink>
        <NavLink to="/cron" style={({ isActive }) => (isActive ? activeStyle : {})}>
          CRON Jobs
        </NavLink>
        <NavLink to="/memory" style={({ isActive }) => (isActive ? activeStyle : {})}>
          Memory
        </NavLink>
      </nav>
      <main style={{ padding: "1.5rem 2rem", maxWidth: 960, margin: "0 auto" }}>
        <Routes>
          <Route path="/" element={<Chat />} />
          <Route path="/cron" element={<CronJobs />} />
          <Route path="/memory" element={<MemoryPage />} />
        </Routes>
      </main>
    </>
  );
}
