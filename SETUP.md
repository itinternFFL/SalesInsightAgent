# Microsoft SSO Setup

The app code is ready; this covers the part only your organization's
Microsoft admin side can do - registering the app in Entra ID (Azure AD)
and getting the values the backend needs. You'll likely need Application
Administrator (or Global Administrator) rights in your organization's
tenant to do this - if you don't have that, your IT admin does.

## 1. Register the app

1. Go to **entra.microsoft.com** (or **portal.azure.com** -> Microsoft
   Entra ID) and sign in with your organization account.
2. **App registrations** (left sidebar, under "Manage") -> **New registration**.
3. Name it something like `Sales Insight Agent`.
4. Under **Supported account types**, choose **Accounts in this
   organizational directory only (Single tenant)** - this is what
   restricts sign-in to your organization; personal Microsoft accounts and
   other organizations' accounts are rejected by Microsoft's login page
   itself before the app code even runs.
5. Leave **Redirect URI** blank here - it's added in step 2.
6. Click **Register**.

## 2. Add the redirect URI

1. On the app's page, go to **Authentication** (left sidebar).
2. **Add a platform** -> **Web**.
3. Redirect URI: for local development, enter
   `http://localhost:8001/auth/callback` exactly. Add your production
   backend's callback URL too once you have it (e.g.
   `https://api.yourdomain.com/auth/callback`) - you can list both at once,
   local dev and production don't need separate app registrations.
4. Save.

This must match `MS_REDIRECT_URI` in your `.env` / server env file
**exactly**, including the scheme (`http`/`https`) and no trailing slash -
Microsoft rejects the callback otherwise.

## 3. Create a client secret

1. **Certificates & secrets** (left sidebar) -> **Client secrets** tab ->
   **New client secret**.
2. Give it a description and an expiry (Microsoft caps this - 6/12/24
   months; you'll need to rotate it before it expires).
3. Click **Add**, then **immediately copy the "Value" column** - this is
   `MS_CLIENT_SECRET`. It's only shown once; if you navigate away before
   copying it, you'll need to create a new secret.

## 4. Add the API permission

1. **API permissions** (left sidebar) -> **Add a permission** ->
   **Microsoft Graph** -> **Delegated permissions**.
2. Search for and check **User.Read** (this is usually already added by
   default when you register an app - if so, nothing to do here).
3. `openid`, `profile`, and `email` don't need to be added explicitly -
   MSAL requests them automatically as part of every sign-in.

No admin consent button-click is needed for `User.Read` alone - it's a
permission users can consent to for themselves on first sign-in.

## 5. Find your IDs

Both are on the app's **Overview** page:
- **Application (client) ID** -> this is `MS_CLIENT_ID`.
- **Directory (tenant) ID** -> this is `MS_TENANT_ID`.

## 6. Set the environment variables

**Local development**: copy `.env.example` to `.env` in the project root
and fill in the five values above, plus generate a `SESSION_SECRET_KEY`:
```
python -c "import secrets; print(secrets.token_hex(32))"
```
`backend/main.py` loads `.env` automatically (via `python-dotenv`) - no
need to export anything manually. Leave `APP_ENV=development`.

**Production**: follow `DEPLOYMENT.md` Part B as normal, but before
starting the systemd service, fill in `/etc/sales-agent/backend.env` using
`deploy/backend.env.example` as the template - same values, but:
- `MS_REDIRECT_URI` and `FRONTEND_URL` point at your real domains, not
  localhost.
- `APP_ENV=production` - this switches the session cookie to
  `SameSite=None; Secure`, which is required once the frontend (Vercel)
  and backend are on different domains, but only works over HTTPS (already
  covered by the nginx/certbot steps in `DEPLOYMENT.md`).
- Use a **different** `SESSION_SECRET_KEY` than local dev, generated the
  same way.

## Restart after any env change

The backend only reads these at startup: restart the service
(`sudo systemctl restart sales-agent-backend` in production, or just
re-run uvicorn locally) after editing the env file.

## Email/password sign-in (alternative to Microsoft SSO)

The login page also offers a plain email + password option, for anyone who
doesn't have (or doesn't want to use) a Microsoft account tied to the
tenant. It's a second door into the same app, not a fallback within the
Microsoft flow.

- Self-registration is restricted by email domain: set
  `ALLOWED_EMAIL_DOMAIN` (comma-separated for multiple domains, e.g.
  `faujifoods.com,fauji.com.pk`) to the domain(s) your employees' email
  addresses use. Anyone signing up with an address outside that list is
  refused. Leave it unset to disable self-registration entirely (existing
  accounts can still log in, but no new ones can be created).
- Accounts are stored in a local SQLite database at `db/users.db`
  (created automatically on first backend start) - passwords are hashed
  with bcrypt, never stored in plain text, and the file is gitignored.
- This does **not** get Microsoft's tenant-membership guarantee - anyone
  with an inbox at an allowed domain can self-register, whether or not
  they're actually an employee with a company Microsoft account. If that
  distinction matters for your use case, prefer Microsoft SSO and consider
  unsetting `ALLOWED_EMAIL_DOMAIN` to turn this path off.

## Optional: restrict to a specific group, not just the tenant

The app currently allows anyone in your organization's tenant to sign in.
To narrow that to specific approved people/groups:

1. In Entra ID, go to **Groups** -> **New group**, create a security
   group (e.g. "Sales Insight Agent Users"), and add the approved members.
2. Go to **Enterprise applications** (not "App registrations" - it's a
   different blade) -> find this app -> **Properties** -> set
   **Assignment required?** to **Yes** -> save.
3. Still on the Enterprise application, go to **Users and groups** ->
   **Add user/group** -> assign the security group you created. Only
   assigned users/groups can now sign in at all - Microsoft rejects
   everyone else before your app's code runs, same mechanism as the tenant
   restriction.

This is entirely configured on the Microsoft side - no code change is
needed for this option, since Microsoft itself blocks the sign-in attempt.
If you want the app to *also* show different content per group (not just
gate all-or-nothing access), that needs actual code changes (reading group
claims from the token) - ask if you want that built.
