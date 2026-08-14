from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import Settings
from .domain import (
    AgentError,
    ConflictError,
    IncomingMessage,
    InvalidTransitionError,
    NotFoundError,
)
from .runtime import AgentRuntime

SyntheticEvidenceId = Annotated[
    str,
    StringConstraints(pattern=r"^SYN-EVIDENCE-[0-9]{4}$"),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageRequest(StrictModel):
    tenant_id: str = Field(default="demo", min_length=1, max_length=64)
    channel: str = Field(default="sandbox", min_length=1, max_length=64)
    store_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    message_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    attachments: list[SyntheticEvidenceId] = Field(default_factory=list, max_length=5)


class ApprovalRequest(StrictModel):
    approver: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Audit label supplied by an authenticated operator",
    )


class HealthResponse(StrictModel):
    status: str
    demo_mode: bool
    planner_mode: str


class ToolCallResponse(StrictModel):
    name: str
    status: str
    latency_ms: int
    args: dict[str, Any]


class PolicyResponse(StrictModel):
    outcome: str
    reason_code: str
    explanation: str


class PendingActionResponse(StrictModel):
    action_id: str
    type: str
    state: str
    amount_cents: int
    sandbox: bool
    deduplicated: bool


class MessageResponse(StrictModel):
    trace_id: str
    session_key: str
    intent: str
    status: str
    reply: str
    tool_calls: list[ToolCallResponse]
    policy: PolicyResponse | None
    pending_action: PendingActionResponse | None
    facts_used: list[str]
    deduplicated: bool
    demo_mode: bool


class TraceStepResponse(StrictModel):
    kind: str
    name: str
    status: str
    detail: dict[str, Any]
    latency_ms: int


class TraceResponse(StrictModel):
    trace_id: str
    session_key: str
    intent: str
    status: str
    steps: list[TraceStepResponse]
    facts_used: list[str]
    demo_mode: bool


class ActionResponse(StrictModel):
    action_id: str
    business_key: str
    action_type: str
    state: str
    payload: dict[str, Any]
    policy: dict[str, Any]
    approved_by: str | None
    created_at: str
    updated_at: str


class ExecutionResponse(StrictModel):
    action_id: str
    status: str
    sandbox: bool
    refund_id: str
    deduplicated: bool


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    service = runtime or AgentRuntime.from_settings(Settings.from_env())
    application = FastAPI(
        title="DXL Commerce Agent",
        version="0.2.0",
        description=(
            "Runnable commerce customer-service Agent runtime with typed planning, "
            "scoped tools, policy gates, durable channel workers, and human handoff."
        ),
    )
    application.state.runtime = service

    async def require_operator(
        operator_key: Annotated[
            str | None,
            Header(alias="X-DXL-Operator-Key", min_length=8, max_length=256),
        ] = None,
    ) -> None:
        if service.operator_key is None:
            raise HTTPException(
                status_code=503,
                detail="Operator endpoints are disabled until DXL_OPERATOR_KEY is configured",
            )
        if operator_key is None or not secrets.compare_digest(operator_key, service.operator_key):
            raise HTTPException(status_code=401, detail="Invalid operator key")

    @application.get("/health", response_model=HealthResponse)
    async def health() -> dict[str, Any]:
        return service.health()

    @application.post("/v1/messages", response_model=MessageResponse)
    async def decide(request: MessageRequest) -> dict[str, Any]:
        message = IncomingMessage(
            tenant_id=request.tenant_id,
            channel=request.channel,
            store_id=request.store_id,
            customer_id=request.customer_id,
            message_id=request.message_id,
            text=request.text,
            attachments=tuple(request.attachments),
        )
        try:
            return await service.handle_message(message)
        except ConflictError as error:
            raise HTTPException(
                status_code=409,
                detail=str(error),
                headers={"Retry-After": "1"},
            ) from error

    @application.get(
        "/v1/traces/{trace_id}",
        response_model=TraceResponse,
        dependencies=[Depends(require_operator)],
    )
    async def trace(trace_id: str) -> dict[str, Any]:
        try:
            return service.get_trace(trace_id)
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @application.post(
        "/v1/actions/{action_id}/approve",
        response_model=ActionResponse,
        dependencies=[Depends(require_operator)],
    )
    async def approve(action_id: str, request: ApprovalRequest) -> dict[str, Any]:
        try:
            return service.approve_action(action_id, request.approver)
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except InvalidTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @application.post(
        "/v1/actions/{action_id}/execute",
        response_model=ExecutionResponse,
        dependencies=[Depends(require_operator)],
    )
    async def execute(
        action_id: str,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
                description="Caller-generated stable key for this exact side effect",
            ),
        ],
    ) -> dict[str, Any]:
        try:
            return service.execute_action(action_id, idempotency_key)
        except NotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ConflictError, InvalidTransitionError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except AgentError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return application


app = create_app()
