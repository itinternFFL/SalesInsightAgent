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
  const errorMessage = errorCode
    ? ERROR_MESSAGES[errorCode] || "Sign-in failed. Please try again."
    : null;

  function handleSignIn() {
    window.location.href = `${API_BASE}/auth/login`;
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

        {errorMessage && <div className="login-error">{errorMessage}</div>}
      </div>
    </div>
  );
}
