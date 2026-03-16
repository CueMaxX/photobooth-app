"""
REST API endpoints for the SumUp payment system.

Provides endpoints to initiate, cancel, and retry payments,
as well as a price calculation endpoint for the extra copies overlay.
Payment status updates are pushed to the frontend via SSE (PaymentState event).
"""

import logging
from threading import Thread
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...appconfig import appconfig
from ...container import container
from ...services.payment import PaymentResult, calculate_extra_copies_price
from ...services.sse import sse_service
from ...services.sse.sse_ import SseEventPaymentState

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payment", tags=["payment"])


# --- Request / Response models ---


class PaymentInitiateRequest(BaseModel):
    action_type: str  # "image", "collage", "animation", "video", "multicamera"
    action_index: int = 0
    payment_type: Literal["initial", "extra_copies"] = "initial"
    num_copies: int = 1


class PaymentInitiateResponse(BaseModel):
    started: bool
    message: str = ""
    amount: float = 0.0
    description: str = ""


class ExtraCopiesPriceRequest(BaseModel):
    base_price: float
    num_copies: int


class ExtraCopiesPriceResponse(BaseModel):
    total_price: float
    num_copies: int
    base_price: float


# --- Helper: run payment in background thread and dispatch SSE events ---


def _run_payment_thread(amount: float, description: str):
    """Execute payment synchronously in a background thread, dispatching SSE events."""

    # Dispatch "pending" event
    sse_service.dispatch_event(
        SseEventPaymentState(
            state="pending",
            amount=amount,
            currency=appconfig.payment.currency,
            description=description,
        )
    )

    # This blocks until payment completes, times out, or fails
    result: PaymentResult = container.payment_service.request_payment(amount, description)

    # Dispatch result event
    if result.success:
        sse_service.dispatch_event(
            SseEventPaymentState(
                state="success",
                amount=result.amount,
                currency=appconfig.payment.currency,
                description=description,
                checkout_id=result.checkout_id,
                transaction_id=result.transaction_id,
            )
        )
    else:
        # Map status to frontend state
        if result.status == "TIMEOUT":
            state = "timeout"
        elif result.status == "CANCELLED":
            state = "cancelled"
        else:
            state = "error"

        sse_service.dispatch_event(
            SseEventPaymentState(
                state=state,
                amount=result.amount,
                currency=appconfig.payment.currency,
                description=description,
                error_code=result.error_code,
                error_message=result.error_message,
                checkout_id=result.checkout_id,
            )
        )


# --- Endpoints ---


@router.post("/initiate", response_model=PaymentInitiateResponse)
def api_payment_initiate(request: PaymentInitiateRequest):
    """Start a payment. Runs asynchronously – status updates are sent via SSE (PaymentState)."""

    if not appconfig.payment.enabled:
        raise HTTPException(status_code=400, detail="Payment system is not enabled.")

    if container.payment_service.has_active_checkout:
        raise HTTPException(status_code=409, detail="A payment is already in progress.")

    # Determine amount and description
    try:
        action_list = getattr(appconfig.actions, request.action_type)
        action_config = action_list[request.action_index]
    except (AttributeError, IndexError) as exc:
        raise HTTPException(status_code=404, detail=f"Action not found: {request.action_type}[{request.action_index}]") from exc

    base_price = action_config.price
    description = action_config.product_name or action_config.name

    if request.payment_type == "initial":
        amount = base_price
    elif request.payment_type == "extra_copies":
        if not appconfig.payment.extra_copies.enabled:
            raise HTTPException(status_code=400, detail="Extra copies are not enabled.")
        amount = calculate_extra_copies_price(base_price, request.num_copies)
        description = f"{description} - {request.num_copies}x extra"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown payment_type: {request.payment_type}")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0. Is the price configured for this action?")

    # Start payment in background thread
    thread = Thread(
        name="_payment_thread",
        target=_run_payment_thread,
        args=(amount, description),
        daemon=True,
    )
    thread.start()

    logger.info(f"Payment initiated: {amount} {appconfig.payment.currency} for '{description}'")

    return PaymentInitiateResponse(
        started=True,
        message="Payment started. Listen to SSE 'PaymentState' events for status updates.",
        amount=amount,
        description=description,
    )


@router.post("/cancel")
def api_payment_cancel():
    """Cancel the currently active payment."""

    if not container.payment_service.has_active_checkout:
        raise HTTPException(status_code=404, detail="No active payment to cancel.")

    container.payment_service.cancel_current()

    sse_service.dispatch_event(
        SseEventPaymentState(
            state="cancelled",
            error_code="USER_CANCELLED",
            error_message="Payment cancelled by user.",
        )
    )

    return {"status": "cancelled"}


@router.post("/extra-copies-price", response_model=ExtraCopiesPriceResponse)
def api_extra_copies_price(request: ExtraCopiesPriceRequest):
    """Calculate the price for extra copies (used by frontend for live price display)."""

    if request.num_copies < 1:
        raise HTTPException(status_code=400, detail="num_copies must be >= 1")

    total = calculate_extra_copies_price(request.base_price, request.num_copies)

    return ExtraCopiesPriceResponse(
        total_price=total,
        num_copies=request.num_copies,
        base_price=request.base_price,
    )
    