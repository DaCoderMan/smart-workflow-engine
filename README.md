# Smart Workflow Engine

A lightweight, production-ready workflow automation engine built with Python and FastAPI. Define workflows as simple YAML configs, trigger them via webhooks, cron schedules, or manual API calls, and monitor everything through a real-time dashboard.

Think of it as a self-hosted, developer-friendly alternative to n8n or Zapier — designed for teams that want full control over their automation infrastructure.

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

- **YAML-based workflow definitions** — No code needed for most automations. Define triggers, steps, conditions, and error handling in clean YAML files.
- **Multiple trigger types** — Webhook (receive HTTP events), Cron schedule (APScheduler), or Manual (API call).
- **Built-in actions** — HTTP requests, email (SMTP), Slack notifications, data transforms, conditional routing, validation, and logging.
- **Retry logic with backoff** — Per-step retry counts and delays. Configurable error strategies: stop, continue, or skip.
- **Template variables** — Reference input data and previous step outputs using `{{ variable }}` syntax across all configs.
- **Async execution** — Built on asyncio for non-blocking, concurrent workflow processing with a configurable concurrency limit.
- **Execution history** — Full step-by-step logs with status, duration, attempt count, and error details.
- **Dark-themed dashboard** — Real-time web UI showing workflows, execution history, success rates, and one-click manual triggers.
- **Hot reload** — Reload workflow definitions from disk without restarting the server.

## Architecture

```
app.py              FastAPI application, routes, dashboard, scheduler
engine.py           Async execution engine with retry and error handling
actions.py          Pluggable action registry (http, email, slack, etc.)
models.py           Pydantic data models
workflows/*.yaml    Workflow definitions
templates/          Jinja2 dashboard template
```

## Quick Start

```bash
# Clone and install
cd smart-workflow-engine
pip install -r requirements.txt

# Run
python app.py
# or
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

Open [http://localhost:8080](http://localhost:8080) for the dashboard, or [http://localhost:8080/docs](http://localhost:8080/docs) for the interactive API documentation.

## Example Workflows

### 1. Lead Capture Pipeline (`lead-capture.yaml`)
Webhook &rarr; Validate &rarr; Transform &rarr; Append to Google Sheet &rarr; Slack Alert

```bash
curl -X POST http://localhost:8080/webhook/new-lead \
  -H "Content-Type: application/json" \
  -d '{"name":"Jane Doe","email":"jane@company.com","source":"website","company":"Acme Inc"}'
```

### 2. Daily Metrics Report (`daily-report.yaml`)
Cron (9 AM weekdays) &rarr; Fetch API &rarr; Format Report &rarr; Send Email

Runs automatically on schedule. Can also be triggered manually:
```bash
curl -X POST http://localhost:8080/api/workflows/daily-report/execute
```

### 3. File Upload Monitor (`file-monitor.yaml`)
Webhook &rarr; Validate &rarr; Extract Metadata &rarr; Conditional Routing &rarr; Process &rarr; Notify

```bash
curl -X POST http://localhost:8080/webhook/file-upload \
  -H "Content-Type: application/json" \
  -d '{"filename":"report.pdf","file_url":"https://...","file_type":"pdf","uploaded_by":"user@co.com"}'
```

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/` | Dashboard |
| `GET`  | `/health` | Health check |
| `GET`  | `/api/workflows` | List all workflows |
| `GET`  | `/api/workflows/{id}` | Get workflow definition |
| `POST` | `/api/workflows/{id}/execute` | Manually execute a workflow |
| `POST` | `/api/workflows/{id}/toggle` | Enable/disable a workflow |
| `POST` | `/webhook/{webhook_id}` | Trigger webhook workflows |
| `GET`  | `/api/executions` | List execution history |
| `GET`  | `/api/executions/{id}` | Get execution details |
| `POST` | `/api/reload` | Reload workflows from disk |

## Writing Custom Workflows

Create a YAML file in the `workflows/` directory:

```yaml
id: my-workflow
name: My Custom Workflow
description: Does something useful

trigger:
  type: webhook          # webhook | schedule | manual
  config:
    webhook_id: my-hook  # for webhook triggers
    # cron: "0 9 * * *"  # for schedule triggers

steps:
  - name: step_one
    action: http_request  # http_request | send_email | slack_notify | transform | condition | validate | log
    config:
      method: GET
      url: "https://api.example.com/data"
    retry: 2              # retry up to 2 times on failure
    retry_delay: 3.0      # seconds between retries
    on_error: continue     # stop | continue | skip
```

### Template Variables

Reference data from input and previous steps:

- `{{ input.field }}` — Webhook/trigger input data
- `{{ steps.step_name.output.field }}` — Output from a previous step
- `{{ workflow.id }}` — Current workflow metadata
- `{{ env.VAR_NAME }}` — Environment variables

## Adding Custom Actions

Register new actions in `actions.py`:

```python
@register("my_action")
async def my_action(config: dict, context: dict) -> dict:
    # config = resolved step config
    # context = full execution context (input, steps, workflow)
    result = await do_something(config["param"])
    return {"output_key": result}
```

## License

MIT
