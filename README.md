# Design Studio

A Cursor-style AI chat workspace for designing and building Databricks applications. Chat with foundation models through your Databricks AI Gateway, then go from idea to deployed app in one click.

## What it does

**Design Studio** has four modes:

| Mode | What it does |
|---|---|
| **Chat** | Conversational AI assistant specialised in Databricks app design — architecture, data models, API contracts, Lakebase, Unity Catalog |
| **Build & Deploy** | Describe an app in plain language → generates full source code → uploads to your workspace → deploys as a live Databricks App automatically |
| **Promote** | Turns a chat conversation into structured docs: architecture spec, security review, Jira stories, test cases, or a self-contained build prompt |
| **Ideate** | Generates 5 app ideas from a problem description, then produces a ready-to-use build prompt for whichever idea you pick |

## Architecture

```
Browser  ──►  FastAPI (app.py)  ──►  Databricks Serving Endpoints (LLM)
                   │
                   ├── GET  /api/models          # list available chat models
                   ├── POST /api/chat            # chat completion (detects build intent)
                   ├── POST /api/chat/stream     # streaming chat via SSE
                   ├── POST /api/build-and-deploy   # start async build+deploy job
                   ├── GET  /api/build-and-deploy/{id}  # poll job status
                   ├── POST /api/promote         # generate docs from conversation
                   ├── POST /api/ideate          # generate 5 app ideas
                   └── POST /api/ideate/prompt   # build prompt for a selected idea
```

The frontend is vanilla JS + CSS served from `static/`. No framework, no bundler.

## Authentication

The app uses Databricks Apps user authorization. Each request carries the signed-in user's token via the `x-forwarded-access-token` header — models are listed and called as that user. When the header is absent (local dev), the app falls back to its own service-principal credentials via `databricks.sdk.core.Config`.

## Model policy

Only models matching specific patterns (`claude-opus-4-8`, `claude-sonnet-4-6`, `gpt-5`, `gpt-5-5`) are exposed for chat and code generation. The app discovers available endpoints live from the workspace and filters to this allowlist, with a static fallback list if the API is unreachable.

## Build & Deploy pipeline

When a user asks to build an app, the pipeline runs asynchronously:

1. **Generate** — prompts the LLM to return a JSON blob with `project_name`, `summary`, and `files` (a map of path → content)
2. **Validate** — enforces file path allowlist, per-file size cap (180 KB), total cap (512 KB), and required files (`app.py`, `app.yaml`, `requirements.txt`)
3. **Repair** — multi-stage JSON salvage logic handles truncated/malformed LLM output; falls back across model candidates automatically
4. **Upload** — writes files to a temp directory, then syncs to `/Workspace/Users/<sp>/generated/<project>` via Databricks CLI
5. **Deploy** — creates the Databricks App (if it doesn't exist), waits for it to reach `RUNNING`, deploys the source, and returns the live URL

Poll `GET /api/build-and-deploy/{job_id}` for progress steps and the final result.

## Local development

**Prerequisites:** Python 3.12+, `pip`, Databricks CLI configured

```bash
# Install dependencies
pip install -r requirements.txt

# Set workspace credentials
export DATABRICKS_HOST="https://<your-workspace>.azuredatabricks.net"
export DATABRICKS_TOKEN="dapi..."

# Run
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`.

> Without `x-forwarded-access-token`, the app uses the service-principal credentials from your environment. Model listing and chat calls will use the SP's token.

## Deploy to Databricks Apps

```bash
# From the project root
databricks apps deploy <app-name> --source-code-path /Workspace/Users/<you>/apps/design-studio
```

Or let the app deploy itself — describe what you want to build in the chat and it will generate and deploy a new app automatically.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABRICKS_HOST` | SDK config | Workspace URL |
| `DATABRICKS_TOKEN` | SDK config | Personal access token or SP secret |
| `DBX_WORKSPACE_PATH_MODE` | `app_owned` | `app_owned` or `user` — controls where generated apps are uploaded |
| `DBX_DEFAULT_USER` | — | Fallback username when identity cannot be resolved |
| `GENERATION_REQUEST_TIMEOUT_SECONDS` | `240` | Timeout per LLM generation request |
| `GENERATION_TIMEOUT_MAX_RETRIES` | `2` | Retries on timeout before moving to next model |
| `GENERATION_REMOTE_REPAIR_ENABLED` | `false` | Enable remote JSON repair pass for malformed generation output |

## Files

```
app.py              FastAPI backend — all endpoints and build/deploy logic
app.yaml            Databricks Apps deployment config
requirements.txt    Python dependencies
static/
  index.html        Single-page frontend shell
  app.js            Frontend logic (chat, build, promote, ideate)
  styles.css        UI styles
```

## Dependencies

- `fastapi` + `uvicorn` — web server
- `databricks-sdk` — workspace auth and config
- `requests` — HTTP calls to serving endpoints
- `pydantic` — request/response models
- `pyyaml` — parsing and repairing generated `app.yaml`
