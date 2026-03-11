"""
Smart Workflow Engine — FastAPI application.

A lightweight, production-ready workflow automation engine that supports
webhook, scheduled, and manual triggers with configurable action steps.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from engine import WorkflowEngine
from models import TriggerType, WorkflowDefinition, WorkflowStep, WorkflowTrigger

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "logs" / "engine.log"),
    ],
)
logger = logging.getLogger("workflow.app")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Smart Workflow Engine",
    description="A configurable workflow automation engine with webhook, schedule, and manual triggers.",
    version="1.0.0",
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
engine = WorkflowEngine()
scheduler = AsyncIOScheduler()

# Workflow storage (in-memory, loaded from YAML on startup)
WORKFLOWS: dict[str, WorkflowDefinition] = {}

# ---------------------------------------------------------------------------
# Workflow loading
# ---------------------------------------------------------------------------

WORKFLOWS_DIR = Path(__file__).parent / "workflows"


def load_workflow_from_yaml(filepath: Path) -> WorkflowDefinition:
    """Parse a YAML workflow definition file."""
    with open(filepath) as f:
        data = yaml.safe_load(f)

    steps = [
        WorkflowStep(
            name=s["name"],
            action=s["action"],
            config=s.get("config", {}),
            retry=s.get("retry", 0),
            retry_delay=s.get("retry_delay", 1.0),
            condition=s.get("condition"),
            on_error=s.get("on_error", "stop"),
        )
        for s in data.get("steps", [])
    ]

    trigger_data = data.get("trigger", {"type": "manual"})
    trigger = WorkflowTrigger(
        type=TriggerType(trigger_data["type"]),
        config=trigger_data.get("config", {}),
    )

    return WorkflowDefinition(
        id=data["id"],
        name=data["name"],
        description=data.get("description", ""),
        enabled=data.get("enabled", True),
        trigger=trigger,
        steps=steps,
        metadata=data.get("metadata", {}),
    )


def load_all_workflows():
    """Load all YAML workflow definitions from the workflows directory."""
    WORKFLOWS.clear()
    if not WORKFLOWS_DIR.exists():
        logger.warning("Workflows directory not found: %s", WORKFLOWS_DIR)
        return

    for filepath in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        try:
            wf = load_workflow_from_yaml(filepath)
            WORKFLOWS[wf.id] = wf
            logger.info("Loaded workflow: %s (%s)", wf.name, wf.id)
        except Exception as e:
            logger.error("Failed to load %s: %s", filepath.name, e)

    logger.info("Loaded %d workflows", len(WORKFLOWS))


def schedule_cron_workflows():
    """Register APScheduler jobs for cron-triggered workflows."""
    for wf in WORKFLOWS.values():
        if wf.trigger.type == TriggerType.SCHEDULE and wf.enabled:
            cron_expr = wf.trigger.config.get("cron", "0 9 * * *")
            parts = cron_expr.split()
            trigger = CronTrigger(
                minute=parts[0] if len(parts) > 0 else "0",
                hour=parts[1] if len(parts) > 1 else "*",
                day=parts[2] if len(parts) > 2 else "*",
                month=parts[3] if len(parts) > 3 else "*",
                day_of_week=parts[4] if len(parts) > 4 else "*",
            )
            scheduler.add_job(
                _run_scheduled,
                trigger=trigger,
                args=[wf.id],
                id=f"cron_{wf.id}",
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info("Scheduled workflow '%s' with cron: %s", wf.name, cron_expr)


async def _run_scheduled(workflow_id: str):
    """Callback for APScheduler to execute a scheduled workflow."""
    wf = WORKFLOWS.get(workflow_id)
    if not wf or not wf.enabled:
        return
    await engine.execute(wf, trigger_type="schedule")


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    os.makedirs(Path(__file__).parent / "logs", exist_ok=True)
    load_all_workflows()
    schedule_cron_workflows()
    scheduler.start()
    logger.info("Smart Workflow Engine started")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)
    logger.info("Smart Workflow Engine stopped")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the main dashboard."""
    workflows_list = []
    for wf in WORKFLOWS.values():
        # Count executions for this workflow
        execs = [e for e in engine.executions if e.workflow_id == wf.id]
        last_exec = execs[-1] if execs else None
        workflows_list.append({
            "id": wf.id,
            "name": wf.name,
            "description": wf.description,
            "trigger": wf.trigger.type.value,
            "enabled": wf.enabled,
            "steps": len(wf.steps),
            "total_runs": len(execs),
            "last_status": last_exec.status.value if last_exec else "never",
            "last_run": last_exec.started_at.strftime("%Y-%m-%d %H:%M:%S") if last_exec else "-",
        })

    # Get recent executions
    recent = sorted(engine.executions, key=lambda e: e.started_at, reverse=True)[:50]
    executions_list = []
    for ex in recent:
        executions_list.append({
            "id": ex.id,
            "workflow_name": ex.workflow_name,
            "trigger": ex.trigger if isinstance(ex.trigger, str) else ex.trigger.value,
            "status": ex.status.value,
            "started_at": ex.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_ms": f"{ex.duration_ms:.0f}" if ex.finished_at else "...",
            "steps_total": len(ex.steps),
            "steps_ok": sum(1 for s in ex.steps if s.status.value == "success"),
            "error": ex.steps[-1].error if ex.steps and ex.steps[-1].error else None,
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "workflows": workflows_list,
        "executions": executions_list,
        "total_workflows": len(WORKFLOWS),
        "total_executions": len(engine.executions),
        "success_rate": _success_rate(),
    })


def _success_rate() -> str:
    total = len(engine.executions)
    if total == 0:
        return "N/A"
    ok = sum(1 for e in engine.executions if e.status.value == "success")
    return f"{ok / total * 100:.0f}%"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.get("/api/workflows")
async def list_workflows():
    """List all registered workflows."""
    return [
        {
            "id": wf.id,
            "name": wf.name,
            "description": wf.description,
            "trigger": wf.trigger.type.value,
            "enabled": wf.enabled,
            "steps": len(wf.steps),
        }
        for wf in WORKFLOWS.values()
    ]


@app.get("/api/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    """Get a single workflow definition."""
    wf = WORKFLOWS.get(workflow_id)
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found")
    return wf.model_dump()


@app.post("/api/workflows/{workflow_id}/execute")
async def execute_workflow(workflow_id: str, request: Request):
    """Manually trigger a workflow execution."""
    wf = WORKFLOWS.get(workflow_id)
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found")
    if not wf.enabled:
        raise HTTPException(400, f"Workflow '{workflow_id}' is disabled")

    try:
        body = await request.json()
    except Exception:
        body = {}

    execution = await engine.execute(wf, input_data=body, trigger_type="manual")
    return {
        "execution_id": execution.id,
        "status": execution.status.value,
        "duration_ms": execution.duration_ms,
        "steps": [
            {
                "name": s.step_name,
                "action": s.action,
                "status": s.status.value,
                "duration_ms": s.duration_ms,
                "attempts": s.attempts,
                "error": s.error,
            }
            for s in execution.steps
        ],
    }


@app.post("/api/workflows/{workflow_id}/toggle")
async def toggle_workflow(workflow_id: str):
    """Enable or disable a workflow."""
    wf = WORKFLOWS.get(workflow_id)
    if not wf:
        raise HTTPException(404, f"Workflow '{workflow_id}' not found")
    wf.enabled = not wf.enabled
    return {"id": wf.id, "enabled": wf.enabled}


# ---------------------------------------------------------------------------
# Webhook receiver
# ---------------------------------------------------------------------------

@app.post("/webhook/{webhook_id}")
async def webhook_trigger(webhook_id: str, request: Request):
    """
    Receive webhook events and trigger matching workflows.
    The webhook_id matches against the workflow trigger config's webhook_id.
    """
    # Find workflows with matching webhook_id
    matching = [
        wf for wf in WORKFLOWS.values()
        if wf.trigger.type == TriggerType.WEBHOOK
        and wf.trigger.config.get("webhook_id") == webhook_id
        and wf.enabled
    ]

    if not matching:
        raise HTTPException(404, f"No workflow registered for webhook '{webhook_id}'")

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw_body": (await request.body()).decode(errors="replace")}

    results = []
    for wf in matching:
        execution = await engine.execute(wf, input_data=payload, trigger_type="webhook")
        results.append({
            "workflow": wf.name,
            "execution_id": execution.id,
            "status": execution.status.value,
        })

    return {"triggered": len(results), "results": results}


# ---------------------------------------------------------------------------
# Execution history
# ---------------------------------------------------------------------------

@app.get("/api/executions")
async def list_executions(limit: int = 50, workflow_id: str | None = None):
    """List recent workflow executions."""
    execs = engine.executions
    if workflow_id:
        execs = [e for e in execs if e.workflow_id == workflow_id]
    execs = sorted(execs, key=lambda e: e.started_at, reverse=True)[:limit]
    return [
        {
            "id": ex.id,
            "workflow_id": ex.workflow_id,
            "workflow_name": ex.workflow_name,
            "trigger": ex.trigger if isinstance(ex.trigger, str) else ex.trigger.value,
            "status": ex.status.value,
            "started_at": ex.started_at.isoformat(),
            "duration_ms": ex.duration_ms,
            "steps": len(ex.steps),
        }
        for ex in execs
    ]


@app.get("/api/executions/{execution_id}")
async def get_execution(execution_id: str):
    """Get detailed execution info including step-by-step results."""
    ex = engine.get_execution(execution_id)
    if not ex:
        raise HTTPException(404, f"Execution '{execution_id}' not found")
    return {
        "id": ex.id,
        "workflow_id": ex.workflow_id,
        "workflow_name": ex.workflow_name,
        "trigger": ex.trigger if isinstance(ex.trigger, str) else ex.trigger.value,
        "status": ex.status.value,
        "started_at": ex.started_at.isoformat(),
        "finished_at": ex.finished_at.isoformat() if ex.finished_at else None,
        "duration_ms": ex.duration_ms,
        "input_data": ex.input_data,
        "steps": [
            {
                "name": s.step_name,
                "action": s.action,
                "status": s.status.value,
                "output": s.output,
                "error": s.error,
                "duration_ms": s.duration_ms,
                "attempts": s.attempts,
            }
            for s in ex.steps
        ],
    }


@app.post("/api/reload")
async def reload_workflows():
    """Reload workflow definitions from disk."""
    load_all_workflows()
    schedule_cron_workflows()
    return {"reloaded": len(WORKFLOWS), "workflow_ids": list(WORKFLOWS.keys())}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "workflows": len(WORKFLOWS),
        "executions": len(engine.executions),
        "uptime": datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True)
