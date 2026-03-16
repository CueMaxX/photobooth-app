"""
SumUp Cloud API payment service for Solo reader terminal payments.

Uses the Cloud API endpoints:
- POST /v0.1/merchants/{mc}/readers/{rid}/checkout  (start transaction on reader)
- POST /v0.1/merchants/{mc}/readers/{rid}/terminate  (cancel transaction)
- GET  /v0.1/merchants/{mc}/readers/{rid}/status      (poll reader status)

SumUp Cloud API docs: https://developer.sumup.com/terminal-payments/cloud-api
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import requests

from ..appconfig import appconfig

logger = logging.getLogger(__name__)

SUMUP_API_BASE = "https://api.sumup.com/v0.1"

# Polling interval in seconds when waiting for terminal payment result
POLL_INTERVAL_SECONDS = 2.0

# HTTP timeout for individual API requests in seconds
REQUEST_TIMEOUT_SECONDS = 15.0


@dataclass
class PaymentResult:
    """Result of a payment attempt."""

    success: bool
    status: str  # "PAID", "FAILED", "EXPIRED", "CANCELLED", "TIMEOUT", "ERROR"
    transaction_id: str = ""
    error_code: str = ""
    error_message: str = ""
    amount: float = 0.0
    checkout_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class SumUpPaymentService:
    """Encapsulates the SumUp Cloud API for Solo reader terminal payments."""

    def __init__(self):
        self._active: bool = False
        self._cancelled: bool = False
        self._client_transaction_id: str = ""

    def _get_api_key(self) -> str:
        return appconfig.payment.sumup_api_key.get_secret_value()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
        }

    def _get_merchant_code(self) -> str:
        return appconfig.payment.sumup_merchant_code

    def _get_reader_id(self) -> str:
        return appconfig.payment.sumup_reader_id

    def _get_affiliate_key(self) -> str:
        return appconfig.payment.sumup_affiliate_key

    def _reader_base_url(self) -> str:
        mc = self._get_merchant_code()
        rid = self._get_reader_id()
        return f"{SUMUP_API_BASE}/merchants/{mc}/readers/{rid}"

    def _amount_to_minor_units(self, amount: float) -> dict:
        """Convert a float amount (e.g. 3.00) to SumUp minor units format."""
        value = round(amount * 100)
        return {
            "currency": appconfig.payment.currency,
            "minor_unit": 2,
            "value": value,
        }

    def create_reader_checkout(self, amount: float, description: str) -> dict:
        """
        Start a checkout on the paired Solo reader.

        POST /v0.1/merchants/{mc}/readers/{rid}/checkout
        Returns response dict with data.client_transaction_id.
        """
        url = f"{self._reader_base_url()}/checkout"
        payload: dict = {
            "total_amount": self._amount_to_minor_units(amount),
        }

        if description:
            payload["description"] = description

        affiliate_key = self._get_affiliate_key()
        if affiliate_key:
            payload["affiliate"] = {"key": affiliate_key}

        logger.info(f"Creating reader checkout: amount={amount}, description='{description}'")
        logger.debug(f"Checkout payload: {payload}")

        try:
            response = requests.post(url, headers=self._get_headers(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Reader checkout created: {data}")
            return data
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else "no response body"
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"SumUp API error creating reader checkout: {status_code} - {error_body}")
            raise RuntimeError(f"SumUp reader checkout failed: {status_code} - {error_body}") from exc
        except requests.RequestException as exc:
            logger.error(f"Network error creating reader checkout: {exc}")
            raise RuntimeError(f"Network error contacting SumUp API: {exc}") from exc

    def get_reader_status(self) -> dict:
        """
        Get current reader status.

        GET /v0.1/merchants/{mc}/readers/{rid}/status
        """
        url = f"{self._reader_base_url()}/status"

        try:
            response = requests.get(url, headers=self._get_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else "no response body"
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"SumUp API error getting reader status: {status_code} - {error_body}")
            raise RuntimeError(f"SumUp reader status failed: {status_code} - {error_body}") from exc
        except requests.RequestException as exc:
            logger.warning(f"Network error getting reader status (will retry): {exc}")
            raise

    def terminate_reader_checkout(self) -> None:
        """
        Terminate/cancel the current checkout on the reader.

        POST /v0.1/merchants/{mc}/readers/{rid}/terminate
        Best-effort – logs errors but does not raise.
        """
        url = f"{self._reader_base_url()}/terminate"

        logger.info("Terminating reader checkout")

        try:
            response = requests.post(url, headers=self._get_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code < 300:
                logger.info("Reader checkout terminated successfully")
            else:
                logger.warning(f"Terminate response: {response.status_code} - {response.text}")
        except Exception as exc:
            logger.warning(f"Failed to terminate reader checkout (best-effort): {exc}")

    def poll_until_done(self, timeout: int) -> PaymentResult:
        """
        Poll the reader status until the transaction completes or timeout.

        The reader status endpoint returns the current screen/event on the reader.
        We poll until we detect a terminal state (success or failure) or timeout.
        """
        elapsed = 0.0
        last_status = ""

        logger.info(f"Polling reader status (timeout={timeout}s)")

        while elapsed < timeout:
            if self._cancelled:
                logger.info("Payment was cancelled during polling")
                return PaymentResult(
                    success=False,
                    status="CANCELLED",
                    error_code="USER_CANCELLED",
                    error_message="Payment cancelled by user.",
                )

            try:
                status_data = self.get_reader_status()
                logger.debug(f"Reader status: {status_data}")

                # Parse the status response
                # The response structure may contain event info about the checkout
                status_str = str(status_data)

                # Check for transaction completion events in the response
                events = status_data.get("events", [])
                if isinstance(events, list):
                    for event in events:
                        event_type = event.get("type", "") if isinstance(event, dict) else ""
                        if event_type in ("checkout_success", "transaction_successful"):
                            tx_id = ""
                            event_data = event.get("data", {}) if isinstance(event, dict) else {}
                            if isinstance(event_data, dict):
                                tx_id = str(event_data.get("transaction_id", event_data.get("client_transaction_id", "")))
                            logger.info(f"Payment successful! tx_id={tx_id}")
                            return PaymentResult(
                                success=True,
                                status="PAID",
                                transaction_id=tx_id or self._client_transaction_id,
                            )
                        elif event_type in ("checkout_failed", "transaction_failed"):
                            error_msg = ""
                            event_data = event.get("data", {}) if isinstance(event, dict) else {}
                            if isinstance(event_data, dict):
                                error_msg = str(event_data.get("message", "Transaction failed"))
                            logger.warning(f"Payment failed: {error_msg}")
                            return PaymentResult(
                                success=False,
                                status="FAILED",
                                error_code="TRANSACTION_FAILED",
                                error_message=error_msg,
                            )

                # Fallback: check top-level status/screen fields
                current_status = status_data.get("status", "")
                screen = status_data.get("screen", "")

                # Detect idle after we were active – means transaction finished
                if current_status and current_status != last_status:
                    logger.debug(f"Reader status changed: {last_status} -> {current_status}")
                    last_status = current_status

                # If the reader reports a specific checkout result
                checkout_status = status_data.get("checkout_status", "")
                if checkout_status == "successful" or checkout_status == "paid":
                    logger.info("Checkout reported as successful via status")
                    return PaymentResult(
                        success=True,
                        status="PAID",
                        transaction_id=self._client_transaction_id,
                    )
                elif checkout_status in ("failed", "expired", "declined"):
                    logger.warning(f"Checkout reported as {checkout_status} via status")
                    return PaymentResult(
                        success=False,
                        status="FAILED",
                        error_code=checkout_status.upper(),
                        error_message=f"Payment {checkout_status}",
                    )

            except Exception as exc:
                logger.warning(f"Error polling reader status (will retry): {exc}")

            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        logger.warning(f"Payment timed out after {timeout}s")
        return PaymentResult(
            success=False,
            status="TIMEOUT",
            error_code="TIMEOUT",
            error_message=f"No payment completed within {timeout} seconds.",
        )

    def request_payment(self, amount: float, description: str, timeout: int | None = None) -> PaymentResult:
        """
        High-level payment function: create reader checkout → poll until result.

        This is the ONLY function the rest of the app should call.
        """
        if timeout is None:
            timeout = appconfig.payment.payment_timeout_seconds

        self._active = True
        self._cancelled = False
        self._client_transaction_id = ""

        logger.info(f"=== Payment requested: {amount} {appconfig.payment.currency} for '{description}' ===")

        # Step 1: Create checkout on reader
        try:
            checkout_data = self.create_reader_checkout(amount, description)

            # Extract client_transaction_id from response
            data = checkout_data.get("data", {})
            if isinstance(data, dict):
                self._client_transaction_id = data.get("client_transaction_id", "")

            logger.info(f"Checkout started on reader, client_transaction_id={self._client_transaction_id}")

        except Exception as exc:
            logger.error(f"Payment failed at checkout creation: {exc}")
            self._active = False
            return PaymentResult(
                success=False,
                status="ERROR",
                error_code="CHECKOUT_CREATION_FAILED",
                error_message=str(exc),
                amount=amount,
            )

        # Step 2: Poll for result
        try:
            result = self.poll_until_done(timeout)
            result.amount = amount
            result.checkout_id = self._client_transaction_id

            if result.success:
                logger.info(f"=== Payment successful: {amount} {appconfig.payment.currency} ===")
            else:
                logger.warning(f"=== Payment ended: status={result.status} ===")
                # Try to terminate if still pending
                if result.status == "TIMEOUT":
                    self.terminate_reader_checkout()

            return result

        except Exception as exc:
            logger.error(f"=== Payment failed during polling: {exc} ===")
            self.terminate_reader_checkout()
            self._active = False
            return PaymentResult(
                success=False,
                status="ERROR",
                error_code="POLL_FAILED",
                error_message=str(exc),
                amount=amount,
            )
        finally:
            self._active = False

    def cancel_current(self) -> None:
        """Cancel the currently active checkout, if any."""
        if self._active:
            self._cancelled = True
            self.terminate_reader_checkout()
            logger.info("Current payment cancelled by user")
        else:
            logger.debug("No active checkout to cancel")

    @property
    def has_active_checkout(self) -> bool:
        """Check if there is a currently active (pending) checkout."""
        return self._active

    # --- Payment confirmation token (used by action/share guards) ---

    _payment_confirmed: bool = False

    def set_payment_confirmed(self):
        """Mark that a payment was successfully completed. Used as a one-time token."""
        self._payment_confirmed = True
        logger.info("Payment confirmation flag set")

    def consume_payment_confirmation(self) -> bool:
        """Check and consume the payment confirmation (one-time use)."""
        if self._payment_confirmed:
            self._payment_confirmed = False
            logger.info("Payment confirmation consumed")
            return True
        return False

    @property
    def is_payment_confirmed(self) -> bool:
        return self._payment_confirmed

    def clear_payment_confirmation(self):
        self._payment_confirmed = False


def calculate_extra_copies_price(base_price: float, num_copies: int) -> float:
    """
    Calculate the total price for additional copies based on tiered pricing.

    Pricing tiers (configured in appconfig.payment.extra_copies):
    - 1st extra copy: base_price * price_percent / 100
    - 2nd to (bulk_threshold - 1) extra copies: base_price * bulk_price_percent / 100 each
    - From bulk_threshold onward: base_price * bulk_above_price_percent / 100 each
    """
    if num_copies <= 0:
        return 0.0

    ec = appconfig.payment.extra_copies
    total = 0.0

    for i in range(1, num_copies + 1):
        if i == 1:
            total += base_price * ec.price_percent / 100.0
        elif i < ec.bulk_threshold:
            total += base_price * ec.bulk_price_percent / 100.0
        else:
            total += base_price * ec.bulk_above_price_percent / 100.0

    return round(total, 2)
    