import logging

from fastapi import APIRouter, HTTPException

from ...appconfig import appconfig
from ...container import container
from ...services.processing import ActionType
from ...utils.exceptions import ProcessMachineOccupiedError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/actions", tags=["actions"])


def _check_payment_required_before_capture(action_type: ActionType, index: int):
    """Guard: if payment is enabled in Variant A (before capture), verify payment was completed."""
    if not appconfig.payment.enabled:
        return
    if not appconfig.payment.payment_before_capture:
        return

    # Get the action config to check the price
    try:
        action_list = getattr(appconfig.actions, action_type)
        action_config = action_list[index]
    except (AttributeError, IndexError):
        return  # let the actual trigger_action handle invalid action types

    if action_config.price <= 0:
        return  # free action, no payment needed

    # Payment is required – check if it was confirmed
    if not container.payment_service.consume_payment_confirmation():
        raise HTTPException(
            status_code=402,
            detail="Payment required before capture. Please complete payment first.",
        )

    logger.info(f"Payment confirmed for action {action_type}[{index}], proceeding with capture.")


@router.get("/{action_type}/{index}")
def api_trigger_model(action_type: ActionType, index: int = 0):
    try:
        # Payment guard for Variant A (payment before capture)
        _check_payment_required_before_capture(action_type, index)

        container.processing_service.trigger_action(action_type, index)

    except ProcessMachineOccupiedError as exc:
        # raised if processingservice not idle
        raise HTTPException(status_code=400, detail=f"only one capture at a time allowed: {exc}") from exc
    except HTTPException:
        raise  # re-raise payment 402 without wrapping
    except Exception as exc:
        # other errors
        logger.exception(exc)
        logger.critical(exc)
        raise HTTPException(status_code=500, detail=f"something went wrong, Exception: {exc}") from exc
        