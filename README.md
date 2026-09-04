# Sales Insight Agent (RAG)

Ask natural-language questions about the FMO monthly sales reports in
`data/` and get answers grounded in real pandas-computed numbers.

Runs **entirely locally and free** — no API keys, no cloud calls, no data
leaving this machine. It uses a local embedding model (sentence-transformers)
for retrieval and a local LLM via [Ollama](https://ollama.com) for the actual
answers.

## How it works (RAG pipeline)

1. **Ingest** (`src/ingest.py`) — loads every `data/*.xlsx` file (Cereals and
   Dairy sheets), cleans and normalizes them into one tidy pandas
   DataFrame (~55K transaction rows), cached to `cache/master_sales.parquet`.
2. **Summarize** (`src/summarize.py`) — instead of embedding raw rows (too
   many, too noisy), pandas `groupby` aggregations turn the raw data into a
   few hundred natural-language summaries: totals by month/brand, by
   month/channel, by month/category, brand trends over time, and top
   customers. Every number in a summary is a real pandas calculation.
3. **Embed** (`src/embeddings.py`) — each summary is turned into a vector
   using a local `sentence-transformers` model (`all-MiniLM-L6-v2`, no API
   key needed). Cached to `cache/chunks.parquet`.
4. **Retrieve** (`src/index.py`) — when you ask a question, it's embedded
   with the same model and compared (cosine similarity) against every
   summary to find the most relevant ones.
5. **Generate** (`src/agent.py`) — the top matches are sent to a local LLM
   running via Ollama (`llama3.1:8b`) as context, with instructions to answer
   only from that context and cite the figures used.

There are three ways to use the pipeline:
- **`backend/main.py`** — a FastAPI server exposing the pipeline over HTTP,
  used by the React UI.
- **`frontend/`** — a React (Vite) chat interface that talks to the backend.
- **`src/chat.py`** — a plain command-line chat loop, no server needed.

## Setup

1. **Install [Ollama](https://ollama.com/download)** (already done on this
   machine) and pull the model used by the agent:
   ```
   ollama pull llama3.1:8b
   ```
   Ollama runs as a background service once installed — no need to start
   anything manually.

2. **Python side** — activate the virtual environment (already created in
   `venv/`) and install dependencies:
   ```
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Frontend side** — install the React app's dependencies (already done
   once, only needed again if `frontend/node_modules` is deleted):
   ```
   cd frontend
   npm install
   ```

## Run it

**Web UI** — needs two terminals running at the same time:

Terminal 1 (backend API, from the project root):
```
venv\Scripts\activate
python -m uvicorn backend.main:app --port 8001
```

Terminal 2 (React dev server):
```
cd frontend
npm run dev
```

Then open **http://localhost:5173** in your browser.

**Command line**, if you'd rather not run a UI at all:
```
venv\Scripts\activate
python -m src.chat
```

The first run of any of these builds the index (parses the Excel files,
computes summaries, generates embeddings) — this takes a bit. Every run
after that is instant because the index is cached in `cache/`. Answers
themselves take a few seconds to a couple of minutes since the model runs
on CPU.

Example questions:
- "What were total net sales for Cereals in April 2026?"
- "Which Dairy brand performed best in May?"
- "How did the Coated brand trend from January to June?"
- "Who were the top customers for Cereals in March?"

## Adding new monthly reports

Easiest way: attach the file in the chat UI (drag-and-drop or the `+`
button) - it validates and merges automatically, with an auto-generated
summary. See `DEPLOYMENT.md`'s "After deployment" section for the
production equivalent.

You can also drop the `.xlsx` file into `data/` directly (same format:
sheets named `Cereals` and `Dairy` - the header row is detected
automatically, so it doesn't have to be a fixed row). The next time you run
the agent, it will notice the new file is newer than the cache and rebuild
automatically. If you ever want to force a full rebuild, just delete the
`cache/` folder.

## Checking accuracy

`eval_accuracy.py` runs a diverse sample of questions against the live
agent and grades each one against ground truth computed independently via
pandas straight from the current dataset (not from the agent's own
output). Useful after any change to `src/summarize.py`, `src/agent.py`, or
`src/index.py` to confirm nothing regressed:
```
python eval_accuracy.py
```
Takes a while - each question is a real Ollama call (roughly 1-3 minutes
apiece on CPU), so a full run is easily 30-45 minutes. Prints a pass/fail
per question and a final accuracy percentage.

## Swapping in Claude instead (optional, better answer quality)

If you later get an Anthropic API key, you can get noticeably better
answers by swapping `src/agent.py` to call the Claude API
(`anthropic.Anthropic().messages.create(model="claude-opus-5", ...)`)
instead of `ollama.chat(...)` — everything else (ingest, summarize, embed,
retrieve, backend, frontend) stays exactly the same, since only the final
generation step changes.

## Project structure

```
data/                  monthly sales report Excel files (input, not modified)
cache/                 generated: cleaned data + embeddings (safe to delete)
deploy/                systemd unit + nginx config templates for the backend server
DEPLOYMENT.md          full deployment runbook (Vercel frontend + self-hosted backend)
eval_accuracy.py       accuracy evaluation - see "Checking accuracy" above
backend/
  main.py              FastAPI server: /api/stats, /api/chat, /api/upload(/resolve)
  uploads.py           upload staging, filename disambiguation, summary text
frontend/              React (Vite) chat UI
  src/App.jsx           chat interface, file upload, dataset sidebar
  src/App.css            layout + component styles (glassmorphism theme)
  src/index.css          base theme tokens, font stack
src/
  ingest.py            load + clean Excel files -> one DataFrame; upload validation
  summarize.py         pandas aggregations -> text summary chunks
  embeddings.py         local embedding model wrapper
  index.py             build/cache the embedded index; hybrid retrieval
  agent.py             retrieval + local Ollama call
  chat.py              command-line chat loop
```
