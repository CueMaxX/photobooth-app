"""
SumUp Cloud API payment service for terminal-based contactless payments.

Provides a single high-level function request_payment() that handles the
complete checkout lifecycle: create → send to reader → poll until result.
Used for both initial action payments and extra copies payments.

Uses 'requests' (synchronous) since the processing workflow runs in a
separate thread anyway. No async needed.

SumUp API reference: https://developer.sumup.com/api
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
    """Encapsulates the SumUp Cloud API for terminal-based card payments."""

    def __init__(self):
        self._current_checkout_id: str | None = None
        self._payment_confirmed: bool = False

    def _get_api_key(self) -> str:
        """Retrieve the API key from config (SecretStr)."""
        return appconfig.payment.sumup_api_key.get_secret_value()

    def _get_headers(self) -> dict[str, str]:
        """Build HTTP headers for SumUp API requests."""
        return {
            "Authorization": f"Bearer {self._get_api_key()}",
            "Content-Type": "application/json",
        }

    def _get_merchant_code(self) -> str:
        return appconfig.payment.sumup_merchant_code

    def _get_reader_id(self) -> str:
        return appconfig.payment.sumup_reader_id

    def create_checkout(self, amount: float, description: str, reference: str) -> dict:
        """
        Create a checkout at SumUp.

        POST https://api.sumup.com/v0.1/checkouts
        Returns the full checkout response dict including 'id'.

        Raises:
            RuntimeError: If the API call fails.
        """
        url = f"{SUMUP_API_BASE}/checkouts"
        payload = {
            "checkout_reference": reference,
            "amount": round(amount, 2),
            "currency": appconfig.payment.currency,
            "description": description,
            "merchant_code": self._get_merchant_code(),
        }

        logger.info(f"Creating SumUp checkout: amount={amount}, description='{description}', reference='{reference}'")

        try:
            response = requests.post(url, headers=self._get_headers(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Checkout created successfully: checkout_id={data.get('id')}")
            return data
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else "no response body"
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"SumUp API error creating checkout: {status_code} - {error_body}")
            raise RuntimeError(f"SumUp checkout creation failed: {status_code} - {error_body}") from exc
        except requests.RequestException as exc:
            logger.error(f"Network error creating checkout: {exc}")
            raise RuntimeError(f"Network error contacting SumUp API: {exc}") from exc

    def send_to_reader(self, checkout_id: str) -> dict:
        """
        Send a checkout to the paired Solo reader for payment.

        PUT https://api.sumup.com/v0.1/readers/{reader_id}/checkout
        Returns the API response dict.

        Raises:
            RuntimeError: If the API call fails.
        """
        reader_id = self._get_reader_id()
        url = f"{SUMUP_API_BASE}/readers/{reader_id}/checkout"
        payload = {"checkout_id": checkout_id}

        logger.info(f"Sending checkout {checkout_id} to reader {reader_id}")

        try:
            response = requests.put(url, headers=self._get_headers(), json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
            logger.info("Checkout sent to reader successfully")
            return data
        except requests.HTTPError as exc:
            error_body = exc.response.text if exc.response is not None else "no response body"
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            logger.error(f"SumUp API error sending to reader: {status_code} - {error_body}")
            raise RuntimeError(f"SumUp send-to-reader failed: {status_code} - {error_body}") from exc
        except requests.RequestException as exc:
            logger.error(f"Network error sending to reader: {exc}")
            raise RuntimeError(f"Network error contacting SumUp API: {exc}") from exc

    def poll_checkout_status(self, checkout_id: str, timeout: int) -> dict:
        """
        Poll the checkout status until terminal (PAID/FAILED/EXPIRED) or timeout.

        GET https://api.sumup.com/v0.1/checkouts/{checkout_id}
        Polls every POLL_INTERVAL_SECONDS seconds.

        Returns:
            The final checkout status response dict.

        Raises:
            TimeoutError: If the timeout is exceeded without a terminal status.
            RuntimeError: If the API call fails.
        """
        url = f"{SUMUP_API_BASE}/checkouts/{checkout_id}"
        terminal_statuses = {"PAID", "FAILED", "EXPIRED"}
        elapsed = 0.0

        logger.info(f"Polling checkout {checkout_id} status (timeout={timeout}s)")

        while elapsed < timeout:
            try:
                response = requests.get(url, headers=self._get_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()
                data = response.json()

                status = data.get("status", "UNKNOWN")
                logger.debug(f"Checkout {checkout_id} status: {status} (elapsed={elapsed:.0f}s)")

                if status in terminal_statuses:
                    logger.info(f"Checkout {checkout_id} reached terminal status: {status}")
                    return data

            except requests.HTTPError as exc:
                error_body = exc.response.text if exc.response is not None else "no response body"
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                logger.error(f"SumUp API error polling status: {status_code} - {error_body}")
                raise RuntimeError(f"SumUp status poll failed: {status_code} - {error_body}") from exc
            except requests.RequestException as exc:
                # Network errors during polling are logged but retried
                logger.warning(f"Network error polling status (will retry): {exc}")

            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        logger.warning(f"Checkout {checkout_id} timed out after {timeout}s")
        raise TimeoutError(f"Payment timed out after {timeout} seconds")

    def cancel_checkout(self, checkout_id: str) -> None:
        """
        Cancel / abort a pending checkout.

        DELETE https://api.sumup.com/v0.1/checkouts/{checkout_id}
        Logs errors but does not raise – cancellation is best-effort.
        """
        url = f"{SUMUP_API_BASE}/checkouts/{checkout_id}"

        logger.info(f"Cancelling checkout {checkout_id}")

        try:
            response = requests.delete(url, headers=self._get_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code < 300:
                logger.info(f"Checkout {checkout_id} cancelled successfully")
            else:
                logger.warning(f"Cancel checkout response: {response.status_code} - {response.text}")
        except Exception as exc:
            logger.warning(f"Failed to cancel checkout {checkout_id} (best-effort): {exc}")

    def request_payment(self, amount: float, description: str, timeout: int | None = None) -> PaymentResult:
        """
        High-level payment function: create checkout → send to reader → poll until result.

        This is the ONLY function the rest of the app should call.

        Args:
            amount: Amount to charge (in the configured currency).
            description: Product/service description shown on the terminal.
            timeout: Override for payment timeout (uses config default if None).

        Returns:
            PaymentResult with success status, transaction_id, error info, etc.
        """
        if timeout is None:
            timeout = appconfig.payment.payment_timeout_seconds

        reference = f"pb-{uuid.uuid4().hex[:12]}"

        logger.info(f"=== Payment requested: {amount} {appconfig.payment.currency} for '{description}' ===")

        # Step 1: Create checkout
        try:
            checkout_data = self.create_checkout(amount, description, reference)
            checkout_id = checkout_data["id"]
            self._current_checkout_id = checkout_id
        except Exception as exc:
            logger.error(f"Payment failed at checkout creation: {exc}")
            return PaymentResult(
                success=False,
                status="ERROR",
                error_code="CHECKOUT_CREATION_FAILED",
                error_message=str(exc),
                amount=amount,
            )

        # Step 2: Send to reader
        try:
            self.send_to_reader(checkout_id)
        except Exception as exc:
            logger.error(f"Payment failed at send-to-reader: {exc}")
            # Try to clean up the checkout
            self.cancel_checkout(checkout_id)
            self._current_checkout_id = None
            return PaymentResult(
                success=False,
                status="ERROR",
                error_code="SEND_TO_READER_FAILED",
                error_message=str(exc),
                amount=amount,
                checkout_id=checkout_id,
            )

        # Step 3: Poll for result
        try:
            result_data = self.poll_checkout_status(checkout_id, timeout)
            status = result_data.get("status", "UNKNOWN")
            transaction_id = ""

            # Extract transaction ID from completed checkout
            transactions = result_data.get("transactions", [])
            if transactions:
                transaction_id = str(transactions[0].get("id", ""))

            self._current_checkout_id = None

            if status == "PAID":
                logger.info(f"=== Payment successful: {amount} {appconfig.payment.currency}, tx={transaction_id} ===")
                return PaymentResult(
                    success=True,
                    status="PAID",
                    transaction_id=transaction_id,
                    amount=amount,
                    checkout_id=checkout_id,
                )
            else:
                logger.warning(f"=== Payment not successful: status={status} ===")
                return PaymentResult(
                    success=False,
                    status=status,
                    error_code=status,
                    error_message=f"Payment ended with status: {status}",
                    amount=amount,
                    checkout_id=checkout_id,
                )

        except TimeoutError:
            logger.warning(f"=== Payment timed out after {timeout}s ===")
            self.cancel_checkout(checkout_id)
            self._current_checkout_id = None
            return PaymentResult(
                success=False,
                status="TIMEOUT",
                error_code="TIMEOUT",
                error_message=f"No payment received within {timeout} seconds.",
                amount=amount,
                checkout_id=checkout_id,
            )
        except Exception as exc:
            logger.error(f"=== Payment failed during polling: {exc} ===")
            self.cancel_checkout(checkout_id)
            self._current_checkout_id = None
            return PaymentResult(
                success=False,
                status="ERROR",
                error_code="POLL_FAILED",
                error_message=str(exc),
                amount=amount,
                checkout_id=checkout_id,
            )

    def cancel_current(self) -> None:
        """Cancel the currently active checkout, if any."""
        if self._current_checkout_id:
            self.cancel_checkout(self._current_checkout_id)
            self._current_checkout_id = None
            logger.info("Current payment cancelled by user")
        else:
            logger.debug("No active checkout to cancel")

    @property
    def has_active_checkout(self) -> bool:
        """Check if there is a currently active (pending) checkout."""
        return self._current_checkout_id is not None

    def set_payment_confirmed(self):
        """Mark that a payment was successfully completed. Used as a one-time token."""
        self._payment_confirmed = True
        logger.info("Payment confirmation flag set")

    def consume_payment_confirmation(self) -> bool:
        """Check and consume the payment confirmation (one-time use).
        Returns True if a payment was confirmed, then resets the flag."""
        if self._payment_confirmed:
            self._payment_confirmed = False
            logger.info("Payment confirmation consumed")
            return True
        return False

    @property
    def is_payment_confirmed(self) -> bool:
        """Check if a payment has been confirmed (without consuming it)."""
        return self._payment_confirmed

    def clear_payment_confirmation(self):
        """Reset the payment confirmation flag (e.g. on new action start or timeout)."""
        self._payment_confirmed = False

def calculate_extra_copies_price(base_price: float, num_copies: int) -> float:
    """
    Calculate the total price for additional copies based on tiered pricing.

    Pricing tiers (configured in appconfig.payment.extra_copies):
    - 1st extra copy: base_price * price_percent / 100
    - 2nd to (bulk_threshold - 1) extra copies: base_price * bulk_price_percent / 100 each
    - From bulk_threshold onward: base_price * bulk_above_price_percent / 100 each

    Args:
        base_price: The original action price (e.g. 3.00 EUR).
        num_copies: Number of extra copies requested (must be >= 1).

    Returns:
        Total price for all requested extra copies, rounded to 2 decimal places.
    """
    if num_copies <= 0:
        return 0.0

    ec = appconfig.payment.extra_copies
    total = 0.0

    for i in range(1, num_copies + 1):
        if i == 1:
            # First extra copy
            total += base_price * ec.price_percent / 100.0
        elif i < ec.bulk_threshold:
            # Copies 2 up to (excluding) bulk_threshold
            total += base_price * ec.bulk_price_percent / 100.0
        else:
            # From bulk_threshold onward
            total += base_price * ec.bulk_above_price_percent / 100.0

    return round(total, 2)
    