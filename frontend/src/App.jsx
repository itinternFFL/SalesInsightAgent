import { useEffect, useRef, useState } from "react";
import "./App.css";

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8001";

const EXAMPLE_QUESTIONS = [
  "What were total net sales for Cereals in April 2026?",
  "Which Dairy brand performed best in May?",
  "How did the Coated brand trend from January to June?",
  "Who were the top customers for Cereals in March?",
];

function Backdrop() {
  return (
    <div className="backdrop">
      <div className="backdrop-blob one" />
      <div className="backdrop-blob two" />
      <div className="backdrop-blob three" />
    </div>
  );
}

function Sidebar({ open, onClose, stats }) {
  const range =
    stats && stats.months.length > 0
      ? `${stats.months[0]} to ${stats.months[stats.months.length - 1]}`
      : "";

  return (
    <>
      <div
        className={`sidebar-scrim ${open ? "open" : ""}`}
        onClick={onClose}
      />
      <aside className={`sidebar ${open ? "open" : ""}`}>
        <div className="sidebar-header">
          <span className="sidebar-title">Dataset</span>
          <button className="sidebar-close" onClick={onClose}>
            Close
          </button>
        </div>
        {stats ? (
          <div className="sidebar-stats">
            <div className="sidebar-stat">
              <span className="sidebar-stat-label">Transactions</span>
              <span className="sidebar-stat-value">
                {stats.rows.toLocaleString()}
              </span>
            </div>
            <div className="sidebar-stat">
              <span className="sidebar-stat-label">Date range</span>
              <span className="sidebar-stat-value">{range}</span>
            </div>
            <div className="sidebar-stat">
              <span className="sidebar-stat-label">Categories</span>
              <span className="sidebar-stat-value">
                {stats.categories.join(", ")}
              </span>
            </div>
            <div className="sidebar-stat">
              <span className="sidebar-stat-label">Model</span>
              <span className="sidebar-stat-value">{stats.model}</span>
            </div>
          </div>
        ) : (
          <p className="sidebar-empty">Loading dataset info...</p>
        )}
      </aside>
    </>
  );
}

// Only **bold** needs handling (the model marks key figures this way) - a
// full markdown library would be overkill for one inline pattern.
function renderWithBold(text) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    return <span key={i}>{part}</span>;
  });
}

function Message({ role, content, actions, resolved, onAction }) {
  return (
    <div className={`message ${role}`}>
      <div className="message-label">{role === "user" ? "You" : "Agent"}</div>
      <div className="message-bubble">{renderWithBold(content)}</div>
      {actions && !resolved && (
        <div className="message-actions">
          {actions.map((a) => (
            <button
              key={a.value}
              className="message-action-btn"
              onClick={() => onAction(a.value)}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function TypingBubble() {
  return (
    <div className="message assistant">
      <div className="message-label">Agent</div>
      <div className="message-bubble">
        <span className="typing">
          <span />
          <span />
          <span />
        </span>
      </div>
    </div>
  );
}

export default function App() {
  const [stats, setStats] = useState(null);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [suggestionIndex, setSuggestionIndex] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const bottomRef = useRef(null);
  const fileInputRef = useRef(null);
  const dragCounter = useRef(0);

  function refreshStats() {
    fetch(`${API_BASE}/api/stats`)
      .then((res) => res.json())
      .then(setStats)
      .catch(() => {});
  }

  useEffect(() => {
    fetch(`${API_BASE}/api/stats`)
      .then((res) => res.json())
      .then(setStats)
      .catch(() => setError("Could not reach the backend at localhost:8000."));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading, uploading]);

  async function sendQuestion(question) {
    const trimmed = question.trim();
    if (!trimmed || loading || uploading) return;

    setError(null);
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: trimmed }]);
    setLoading(true);

    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: trimmed }),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.answer },
      ]);
    } catch (err) {
      setError(err.message || "Something went wrong.");
    } finally {
      setLoading(false);
    }
  }

  async function uploadFile(file) {
    if (uploading) return;

    setError(null);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: `Uploaded: ${file.name}` },
    ]);
    setUploading(true);

    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await fetch(`${API_BASE}/api/upload`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = await res.json();

      if (data.status === "saved") {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: data.summary },
        ]);
        refreshStats();
      } else if (data.status === "needs_confirmation") {
        setMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content: data.message,
            actions: data.actions,
            uploadId: data.upload_id,
          },
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: data.message },
        ]);
      }
    } catch (err) {
      setError(err.message || "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function resolveAction(messageIndex, uploadId, action) {
    setMessages((prev) =>
      prev.map((m, i) => (i === messageIndex ? { ...m, resolved: true } : m))
    );
    setUploading(true);

    try {
      const res = await fetch(`${API_BASE}/api/upload/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_id: uploadId, action }),
      });
      if (!res.ok) throw new Error(`Request failed (${res.status})`);
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.summary || data.message },
      ]);
      if (data.status === "saved") refreshStats();
    } catch (err) {
      setError(err.message || "Could not resolve the upload.");
    } finally {
      setUploading(false);
    }
  }

  function handleFileInputChange(e) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (file) uploadFile(file);
  }

  function handleDragEnter(e) {
    e.preventDefault();
    dragCounter.current += 1;
    setDragActive(true);
  }

  function handleDragLeave(e) {
    e.preventDefault();
    dragCounter.current -= 1;
    if (dragCounter.current <= 0) setDragActive(false);
  }

  function handleDragOver(e) {
    e.preventDefault();
  }

  function handleDrop(e) {
    e.preventDefault();
    dragCounter.current = 0;
    setDragActive(false);
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  }

  function handleSubmit(e) {
    e.preventDefault();
    sendQuestion(input);
  }

  function advanceSuggestions() {
    setSuggestionIndex((i) => (i + 1) % EXAMPLE_QUESTIONS.length);
  }

  return (
    <div
      className="page"
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <Backdrop />

      {dragActive && (
        <div className="drop-overlay">
          <div className="drop-overlay-card">Drop a sales report to add it</div>
        </div>
      )}

      <div className="topbar">
        <h1 className="title">Sales Insight Agent</h1>
        <button
          className="sidebar-toggle"
          onClick={() => setSidebarOpen(true)}
        >
          Dataset details
        </button>
      </div>

      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        stats={stats}
      />

      <div className="main">
        <div className="messages">
          {messages.length === 0 && (
            <div className="empty-state">
              Ask a question about the sales data to get started.
            </div>
          )}
          {messages.map((m, i) => (
            <Message
              key={i}
              role={m.role}
              content={m.content}
              actions={m.actions}
              resolved={m.resolved}
              onAction={(value) => resolveAction(i, m.uploadId, value)}
            />
          ))}
          {(loading || uploading) && <TypingBubble />}
          <div ref={bottomRef} />
        </div>

        <div className="suggestions-row">
          <div className="suggestions-viewport">
            <button
              key={suggestionIndex}
              className="suggestion-chip"
              onClick={() => sendQuestion(EXAMPLE_QUESTIONS[suggestionIndex])}
              disabled={loading || uploading}
            >
              {EXAMPLE_QUESTIONS[suggestionIndex]}
            </button>
          </div>
          <button
            type="button"
            className="suggestions-next"
            onClick={advanceSuggestions}
            aria-label="Show next suggested question"
          >
            &#8594;
          </button>
        </div>

        <div className="suggestions-dots">
          {EXAMPLE_QUESTIONS.map((_, i) => (
            <button
              key={i}
              type="button"
              className={`suggestions-dot ${i === suggestionIndex ? "active" : ""}`}
              onClick={() => setSuggestionIndex(i)}
              aria-label={`Show suggested question ${i + 1}`}
            />
          ))}
        </div>

        <form className="composer" onSubmit={handleSubmit}>
          <div className="composer-inner">
            <input
              type="file"
              ref={fileInputRef}
              accept=".xlsx"
              onChange={handleFileInputChange}
              hidden
            />
            <button
              type="button"
              className="composer-attach"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              aria-label="Attach a sales report file"
            >
              +
            </button>
            <input
              className="composer-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about the sales data, or attach a report..."
            />
            <button
              type="submit"
              className="composer-send"
              disabled={loading || uploading || !input.trim()}
            >
              Send
            </button>
          </div>
          {error && <div className="error-banner">{error}</div>}
        </form>
      </div>
    </div>
  );
}
