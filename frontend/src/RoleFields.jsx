import { useEffect, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE || "";

export const ROLE_LABELS = {
  manager: "Manager",
  senior_executive: "Senior Executive",
  executive: "Executive / Employee",
};

// Shared by the registration form and the post-Microsoft-SSO
// "complete your profile" screen - lets someone declare their own role and
// pick who they report to, so the reporting hierarchy behind role-based
// access control (see ACCESS-CONTROL.md) can be built without an admin UI.
export default function RoleFields({ role, setRole, reportsToId, setReportsToId }) {
  const [managers, setManagers] = useState([]);
  const [seniorExecutives, setSeniorExecutives] = useState([]);

  useEffect(() => {
    fetch(`${API_BASE}/auth/managers`)
      .then((res) => res.json())
      .then(setManagers)
      .catch(() => {});
    fetch(`${API_BASE}/auth/senior-executives`)
      .then((res) => res.json())
      .then(setSeniorExecutives)
      .catch(() => {});
  }, []);

  const managerNameById = Object.fromEntries(managers.map((m) => [m.id, m.name]));

  function handleRoleChange(e) {
    setRole(e.target.value);
    setReportsToId("");
  }

  return (
    <>
      <select className="login-input" value={role} onChange={handleRoleChange} required>
        <option value="" disabled>
          Select your role
        </option>
        <option value="manager">Manager</option>
        <option value="senior_executive">Senior Executive</option>
        <option value="executive">Executive / Employee</option>
      </select>

      {role === "senior_executive" && (
        <select
          className="login-input"
          value={reportsToId}
          onChange={(e) => setReportsToId(e.target.value)}
          required
        >
          <option value="" disabled>
            Reports to (Manager)
          </option>
          {managers.map((m) => (
            <option key={m.id} value={m.id}>
              {m.name}
            </option>
          ))}
        </select>
      )}

      {role === "executive" && (
        <select
          className="login-input"
          value={reportsToId}
          onChange={(e) => setReportsToId(e.target.value)}
          required
        >
          <option value="" disabled>
            Reports to (Senior Executive)
          </option>
          {seniorExecutives.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
              {managerNameById[s.reports_to_id] ? ` (under ${managerNameById[s.reports_to_id]})` : ""}
            </option>
          ))}
        </select>
      )}
    </>
  );
}
