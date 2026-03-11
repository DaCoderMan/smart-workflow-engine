"""Data models for the workflow engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TriggerType(str, Enum):
    WEBHOOK = "webhook"
    SCHEDULE = "schedule"
    MANUAL = "manual"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExecutionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class StepResult(BaseModel):
    step_name: str
    action: str
    status: StepStatus = StepStatus.PENDING
    output: Any = None
    error: str | None = None
    duration_ms: float = 0
    attempts: int = 0


class WorkflowExecution(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    workflow_id: str
    workflow_name: str
    trigger: TriggerType
    status: ExecutionStatus = ExecutionStatus.QUEUED
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None
    input_data: dict[str, Any] = {}
    steps: list[StepResult] = []
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.finished_at and self.started_at:
            return (self.finished_at - self.started_at).total_seconds() * 1000
        return 0


class WorkflowStep(BaseModel):
    name: str
    action: str
    config: dict[str, Any] = {}
    retry: int = 0
    retry_delay: float = 1.0
    condition: str | None = None
    on_error: str = "stop"  # stop | continue | skip


class WorkflowTrigger(BaseModel):
    type: TriggerType
    config: dict[str, Any] = {}


class WorkflowDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    trigger: WorkflowTrigger
    steps: list[WorkflowStep]
    metadata: dict[str, Any] = {}
