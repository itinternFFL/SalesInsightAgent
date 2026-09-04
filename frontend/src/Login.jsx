import { useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

const ERROR_MESSAGES = {
  denied: "Sign-in was cancelled or denied.",
  wrong_tenant: "Access restricted to company employees.",
  expired: "Your sign-in attempt expired. Please try again.",
};

// Microsoft's four-color logo mark, per their sign-in button guidelines.
function MicrosoftLogo() {
  return (
    <svg width="20" height="20" viewBox="0 0 21 21" aria-hidden="true">
      <rect x="1" y="1" width="9" height="9" fill="#F25022" />
      <rect x="11" y="1" width="9" height="9" fill="#7FBA00" />
      <rect x="1" y="11" width="9" height="9" fill="#00A4EF" />
      <rect x="11" y="11" width="9" height="9" fill="#FFB900" />
    </svg>
  );
}

export default function Login() {
  const params = new URLSearchParams(window.location.search);
  const errorCode = params.get("login_error");
  const ssoErrorMessage = errorCode
    ? ERROR_MESSAGES[errorCode] || "Sign-in failed. Please try again."
    : null;

  const [mode, setMode] = useState("login"); // "login" | "register"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState(null);

  function handleSignIn() {
    window.location.href = `${API_BASE}/auth/login`;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setFormError(null);
    setSubmitting(true);
    try {
      const path = mode === "register" ? "/auth/register" : "/auth/login-password";
      const body = mode === "register" ? { name, email, password } : { email, password };
      const res = await fetch(`${API_BASE}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Something went wrong. Please try again.");
      }
      window.location.href = "/";
    } catch (err) {
      setFormError(err.message);
      setSubmitting(false);
    }
  }

  function toggleMode() {
    setMode((m) => (m === "login" ? "register" : "login"));
    setFormError(null);
  }

  return (
    <div className="page login-page">
      <div className="backdrop">
        <div className="backdrop-blob one" />
        <div className="backdrop-blob two" />
        <div className="backdrop-blob three" />
      </div>

      <div className="login-card">
        <h1 className="title login-title">Sales Insight Agent</h1>
        <p className="login-subtitle">Sign in with your company account to continue.</p>

        <button className="ms-signin-btn" onClick={handleSignIn}>
          <MicrosoftLogo />
          Sign in with Microsoft
        </button>

        {ssoErrorMessage && <div className="login-error">{ssoErrorMessage}</div>}

        <div className="login-divider">
          <span>or</span>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          {mode === "register" && (
            <input
              type="text"
              placeholder="Full name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="login-input"
            />
          )}
          <input
            type="email"
            placeholder="Work email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            className="login-input"
          />
          <input
            type="password"
            placeholder="Password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
            className="login-input"
          />
          <button type="submit" className="login-submit-btn" disabled={submitting}>
            {submitting
              ? mode === "register"
                ? "Creating account…"
                : "Signing in…"
              : mode === "register"
              ? "Create account"
              : "Sign in"}
          </button>
        </form>

        {formError && <div className="login-error">{formError}</div>}

        <button type="button" className="login-toggle-mode" onClick={toggleMode}>
          {mode === "register"
            ? "Already have an account? Sign in"
            : "New here? Create an account"}
        </button>
      </div>
    </div>
  );
}
