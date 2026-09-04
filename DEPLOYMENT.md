# Deploying the Sales Insight Agent

Architecture: the React frontend deploys to **Vercel** (a real public URL,
free, in minutes). The FastAPI + Ollama backend needs an always-on server
with enough RAM to hold `llama3.1:8b` in memory - Vercel's serverless
functions can't run Ollama at all (no persistent process, no loaded model
between requests, and hard execution timeouts far shorter than the 1-3
minute response times this model takes on CPU) - so the backend runs on a
separate VPS.

This is two independent deploys connected by one URL. You can do Part A
first and it'll just show connection errors until Part B is done, or do B
first.

## Part A - Frontend on Vercel

1. Push this repo to GitHub (or GitLab/Bitbucket) if it isn't already.
2. On [vercel.com](https://vercel.com), **Add New Project** -> import the repo.
3. In the import settings, set **Root Directory** to `frontend`.
   Vercel auto-detects the Vite framework preset - build command
   `npm run build`, output directory `dist` should already be filled in.
4. Under **Environment Variables**, add:
   - `VITE_API_BASE` = `https://api.yourdomain.com` (the backend URL you'll
     set up in Part B - use a placeholder now, update it once the backend
     is live, then redeploy).
5. Deploy. Vercel gives you a URL like `https://your-app.vercel.app`
   (and lets you add a custom domain later under Project -> Settings ->
   Domains).

Until Part B is live, the deployed frontend will load but show a
"Could not reach the backend" error when it tries to fetch stats/chat -
that's expected.

## Part B - Backend on a VPS

Any Ubuntu 22.04+ VPS with **8GB+ RAM** works (Hetzner, DigitalOcean,
Contabo, Linode, etc. - a plain VPS with enough RAM for an 8B model is
usually much cheaper than an equivalent managed "app platform" tier, since
those platforms aren't priced for standing memory-heavy workloads like a
loaded LLM). These steps are provider-agnostic once you have SSH access.

### 1. Base setup

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip nginx git
sudo useradd -r -m -d /opt/sales-agent -s /bin/bash sales-agent
```

### 2. Get the code onto the server

From your local machine (adjust the source path):
```bash
rsync -avz --exclude venv --exclude node_modules --exclude cache \
  "c:\Users\itintern\Desktop\Manahil\sales agent/" \
  your-user@your-server:/opt/sales-agent/
```
(Or `git clone` the repo directly on the server if it's pushed there -
either way, `data/`'s existing `.xlsx` files need to end up on the server
too, since that's the working dataset.)

### 3. Python environment

```bash
sudo -u sales-agent bash -c '
  cd /opt/sales-agent
  python3 -m venv venv
  ./venv/bin/pip install -r requirements.txt
'
```

### 4. Install Ollama and pull the model

```bash
curl -fsSL https://ollama.com/install.sh | sh   # sets up its own systemd service
ollama pull llama3.1:8b
```

### 5. Pre-build the index

Avoids a slow first request - builds `cache/master_sales.parquet` and the
embedded `cache/chunks.parquet` once, up front:
```bash
sudo -u sales-agent bash -c '
  cd /opt/sales-agent
  ./venv/bin/python -m src.index
'
```

### 6. Configure and start the backend service

```bash
sudo mkdir -p /etc/sales-agent
sudo cp /opt/sales-agent/deploy/backend.env.example /etc/sales-agent/backend.env
sudo nano /etc/sales-agent/backend.env   # fill in ALLOWED_ORIGINS and the MS_* / SESSION_SECRET_KEY values - see SETUP.md

sudo cp /opt/sales-agent/deploy/sales-agent-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sales-agent-backend
sudo systemctl status sales-agent-backend   # should show "active (running)"
```

### 7. HTTPS via nginx + certbot

Point your domain's DNS (an A record for `api.yourdomain.com`) at the
server's IP first, then:
```bash
sudo cp /opt/sales-agent/deploy/nginx-backend.conf /etc/nginx/sites-available/sales-agent-backend
sudo ln -s /etc/nginx/sites-available/sales-agent-backend /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d api.yourdomain.com   # rewrites the nginx config to add HTTPS
```

## Part C - Connect them

Back in the Vercel project settings, update `VITE_API_BASE` to the real
`https://api.yourdomain.com`, then redeploy (Vercel -> Deployments -> ...
-> Redeploy, or just push a commit).

## Part D - Smoke test

```bash
curl https://api.yourdomain.com/api/me
```
should return `{"detail":"Not authenticated"}` (401) - that's correct,
`/api/stats` and friends are all behind login now, so this just confirms
the server is up and the auth check is active. Then load the Vercel URL in
a browser, sign in with Microsoft, and ask a real question end-to-end.

## After deployment: adding new monthly reports

The upload feature (attach button / drag-and-drop in the chat UI) works
the same in production as it does locally - files land in `data/` on the
server and the running backend process refreshes itself, no restart
needed. If you'd rather add a file manually, drop it into
`/opt/sales-agent/data/` on the server and restart the service:
```bash
sudo systemctl restart sales-agent-backend
```

## Things worth knowing about this setup

- **The backend must stay a single process.** `backend/main.py` holds the
  search index and dataset stats in memory (`_state`); running multiple
  uvicorn workers would give each one its own out-of-sync copy. Scale by
  using a bigger server, not more workers, unless that's refactored first.
- **Authentication is required, not optional, before going live.** The app
  now has Microsoft SSO restricted to your organization's tenant (see
  `SETUP.md`) - the Azure app registration side needs to be set up before
  the login flow will work at all, both locally and in production.
- **Response times stay CPU-bound.** Keeping Ollama means the 1-3
  minute-per-question latency seen during local development carries over
  to production, and concurrent users queue behind the same model
  instance. If that becomes a problem, the fix is a GPU server or
  switching the backend to a cloud LLM API - not something to solve by
  adding more uvicorn workers (see above).
- **Back up `data/`.** It's now the durable source of truth for the real
  dataset on a server you're responsible for keeping alive - `cache/` can
  always be regenerated from it, but `data/` can't.
