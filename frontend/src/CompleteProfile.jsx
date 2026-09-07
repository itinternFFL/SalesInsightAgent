import { useState } from "react";
import RoleFields from "./RoleFields.jsx";

const API_BASE = import.meta.env.VITE_API_BASE || "";

// Shown once, right after a user's FIRST successful Microsoft SSO sign-in.
// Microsoft's login has no form to collect role/manager, unlike email/
// password registration, so this screen fills that gap before the user can
// reach any sales data - see backend/access_control.py's
// require_role_assigned and ACCESS-CONTROL.md.
export default function CompleteProfile({ user, onComplete }) {
  const [role, setRole] = useState("");
  const [reportsToId, setReportsToId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await fetch(`${API_BASE}/auth/complete-profile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          role,
          reports_to_id: reportsToId ? Number(reportsToId) : null,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Something went wrong. Please try again.");
      }
      const updatedUser = await res.json();
      onComplete(updatedUser);
    } catch (err) {
      setError(err.message);
      setSubmitting(false);
    }
  }

  return (
    <div className="page login-page">
      <div className="backdrop">
        <div className="backdrop-blob one" />
        <div className="backdrop-blob two" />
        <div className="backdrop-blob three" />
      </div>

      <div className="login-card">
        <h1 className="title login-title">Almost there</h1>
        <p className="login-subtitle">
          Signed in as {user.name} ({user.email}). Set your role so the app
          knows which sales data you should see.
        </p>

        <form className="login-form" onSubmit={handleSubmit}>
          <RoleFields
            role={role}
            setRole={setRole}
            reportsToId={reportsToId}
            setReportsToId={setReportsToId}
          />
          <button type="submit" className="login-submit-btn" disabled={submitting || !role}>
            {submitting ? "Saving…" : "Continue"}
          </button>
        </form>

        {error && <div className="login-error">{error}</div>}
      </div>
    </div>
  );
}
