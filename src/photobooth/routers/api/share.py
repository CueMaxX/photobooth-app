import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from ...appconfig import appconfig
from ...container import container
from ...database.models import Mediaitem
from ...plugins import pm as pluggy_pm
from ...utils.exceptions import WrongMediaTypeError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/share", tags=["share"])


def _check_payment_required_before_print():
    """Guard: if payment is enabled in Variant B (before print), verify payment was completed."""
    if not appconfig.payment.enabled:
        return
    if appconfig.payment.payment_before_capture:
        return  # Variant A: payment was already done before capture, not before print

    # In Variant B we need to check if there's a price on the current action.
    # Since the share endpoint doesn't know which action was used, we check the
    # payment confirmation flag which was set after a successful payment.
    # If no payment was initiated (e.g. price is 0), the flag won't be checked
    # because the frontend only initiates payment for priced actions.
    # As defense-in-depth, we require confirmation if payment is enabled in Variant B.
    if not container.payment_service.consume_payment_confirmation():
        raise HTTPException(
            status_code=402,
            detail="Payment required before printing. Please complete payment first.",
        )

    logger.info("Payment confirmed for print, proceeding with share.")


def _share(mediaitem: Mediaitem, index: int, parameters: dict[str, str] | None):
    try:
        container.share_service.share(mediaitem, index, parameters)
    except (BlockingIOError, ConnectionRefusedError, WrongMediaTypeError):
        pass  # informed by sepearate sse event
    except Exception as exc:
        logger.exception(exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Something went wrong, Exception: {exc}") from exc


@router.post("/actions/{index}")
@router.post("/actions/latest/{index}")
def api_share_latest(index: int = 0, parameters: dict[str, str] | None = None):
    # Payment guard for Variant B (payment before print)
    _check_payment_required_before_print()

    try:
        latest_mediaitem = container.mediacollection_service.get_item_latest()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {exc}") from exc

    _share(latest_mediaitem, index, parameters)


@router.post("/actions/{id}/{index}")
def api_share_item_id(id: UUID, index: int = 0, parameters: dict[str, str] | None = None):
    # Payment guard for Variant B (payment before print)
    _check_payment_required_before_print()

    try:
        requested_mediaitem = container.mediacollection_service.get_item(id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {exc}") from exc
    _share(requested_mediaitem, index, parameters)


@router.get("/download/{id}")
def api_download_item_id_get_sharelinks(id: UUID):  # -> list[str]:
    try:
        requested_mediaitem = container.mediacollection_service.get_item(id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"File not found: {exc}") from exc

    pluggy_links = pluggy_pm.hook.get_share_links(filepath_local=requested_mediaitem.processed, identifier=requested_mediaitem.id)
    pluggy_links_flatten = [x for xs in pluggy_links for x in xs]

    return pluggy_links_flatten
