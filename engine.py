"""
Workflow execution engine.

Handles step-by-step async execution with retry logic, condition evaluation,
error handling strategies, and full execution logging.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from actions import get_action
from models import (
    ExecutionStatus,
    StepResult,
    StepStatus,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowStep,
)

logger = logging.getLogger("workflow.engine")


class WorkflowEngine:
    """Async workflow execution engine with retry and error handling."""

    def __init__(self, max_concurrent: int = 10):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._executions: list[WorkflowExecution] = []
        self._max_history = 500

    @property
    def executions(self) -> list[WorkflowExecution]:
        return list(self._executions)

    def get_execution(self, execution_id: str) -> WorkflowExecution | None:
        for ex in self._executions:
            if ex.id == execution_id:
                return ex
        return None

    async def execute(
        self,
        workflow: WorkflowDefinition,
        input_data: dict[str, Any] | None = None,
        trigger_type: str = "manual",
    ) -> WorkflowExecution:
        """Execute a workflow end-to-end."""
        async with self._semaphore:
            execution = WorkflowExecution(
                workflow_id=workflow.id,
                workflow_name=workflow.name,
                trigger=trigger_type,
                input_data=input_data or {},
            )
            self._executions.append(execution)

            # Trim history
            if len(self._executions) > self._max_history:
                self._executions = self._executions[-self._max_history:]

            execution.status = ExecutionStatus.RUNNING
            logger.info(
                "Executing workflow '%s' (id=%s, execution=%s)",
                workflow.name, workflow.id, execution.id,
            )

            # Build initial context
            context: dict[str, Any] = {
                "input": input_data or {},
                "workflow": {"id": workflow.id, "name": workflow.name},
                "steps": {},
                "env": dict(os.environ),
            }

            has_failure = False
            skip_remaining = False

            for step_def in workflow.steps:
                if skip_remaining:
                    step_result = StepResult(
                        step_name=step_def.name,
                        action=step_def.action,
                        status=StepStatus.SKIPPED,
                    )
                    execution.steps.append(step_result)
                    continue

                step_result = await self._execute_step(step_def, context)
                execution.steps.append(step_result)

                # Store step output in context for downstream steps
                context["steps"][step_def.name] = {
                    "status": step_result.status.value,
                    "output": step_result.output,
                    "error": step_result.error,
                }
                # Also available as previous_step
                context["previous_step"] = context["steps"][step_def.name]

                if step_result.status == StepStatus.FAILED:
                    has_failure = True
                    if step_def.on_error == "stop":
                        logger.error(
                            "Step '%s' failed, stopping workflow (on_error=stop)",
                            step_def.name,
                        )
                        skip_remaining = True
                    elif step_def.on_error == "skip":
                        logger.warning(
                            "Step '%s' failed, skipping remaining (on_error=skip)",
                            step_def.name,
                        )
                        skip_remaining = True
                    else:
                        logger.warning(
                            "Step '%s' failed, continuing (on_error=continue)",
                            step_def.name,
                        )

                # Handle condition action results
                if (
                    step_result.status == StepStatus.SUCCESS
                    and step_def.action == "condition"
                    and isinstance(step_result.output, dict)
                ):
                    if not step_result.output.get("result", True):
                        on_false = step_result.output.get("on_false", "skip_rest")
                        if on_false == "skip_rest":
                            logger.info("Condition false, skipping remaining steps")
                            skip_remaining = True

            # Determine final status
            execution.finished_at = datetime.now(timezone.utc)
            if has_failure and skip_remaining:
                execution.status = ExecutionStatus.FAILED
            elif has_failure:
                execution.status = ExecutionStatus.PARTIAL
            else:
                execution.status = ExecutionStatus.SUCCESS

            logger.info(
                "Workflow '%s' execution %s finished: %s (%.0fms)",
                workflow.name, execution.id, execution.status.value,
                execution.duration_ms,
            )
            return execution

    async def _execute_step(
        self,
        step: WorkflowStep,
        context: dict[str, Any],
    ) -> StepResult:
        """Execute a single step with retry logic."""
        result = StepResult(step_name=step.name, action=step.action)
        max_attempts = step.retry + 1

        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            result.status = StepStatus.RUNNING
            start = time.monotonic()

            try:
                action_fn = get_action(step.action)
                output = await action_fn(step.config, context)
                elapsed = (time.monotonic() - start) * 1000

                result.status = StepStatus.SUCCESS
                result.output = output
                result.duration_ms = elapsed
                result.error = None

                logger.info(
                    "Step '%s' (%s) succeeded in %.0fms (attempt %d/%d)",
                    step.name, step.action, elapsed, attempt, max_attempts,
                )
                return result

            except Exception as e:
                elapsed = (time.monotonic() - start) * 1000
                result.duration_ms = elapsed
                result.error = str(e)

                logger.warning(
                    "Step '%s' (%s) failed attempt %d/%d: %s",
                    step.name, step.action, attempt, max_attempts, e,
                )

                if attempt < max_attempts:
                    delay = step.retry_delay * attempt  # Linear backoff
                    logger.info("Retrying step '%s' in %.1fs...", step.name, delay)
                    await asyncio.sleep(delay)

        result.status = StepStatus.FAILED
        logger.error(
            "Step '%s' (%s) failed after %d attempts: %s",
            step.name, step.action, max_attempts, result.error,
        )
        return result
