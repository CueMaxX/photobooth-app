"""
Payment configuration group for SumUp terminal integration.

Defines all settings related to the contactless payment system:
- SumUp API credentials and reader configuration
- Payment timing and timeout settings
- Extra copies pricing tiers
- UI hints (terminal position)
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, SerializationInfo, field_serializer

from ..serializer import contextual_serializer_password


class ExtraCopiesConfig(BaseModel):
    """Configure pricing tiers for additional print copies after the initial print."""

    model_config = ConfigDict(title="Extra copies configuration")

    enabled: bool = Field(
        default=True,
        description="Offer additional copies after the first print.",
    )
    price_percent: float = Field(
        default=80.0,
        ge=0,
        le=100,
        description="Price for the 2nd copy as percentage of the action base price.",
    )
    bulk_price_percent: float = Field(
        default=60.0,
        ge=0,
        le=100,
        description="Price per copy for copies 3 up to (excluding) the bulk threshold, as percentage of the action base price.",
    )
    bulk_threshold: int = Field(
        default=5,
        ge=2,
        description="From this number of extra copies onward, the bulk_above price applies.",
    )
    bulk_above_price_percent: float = Field(
        default=50.0,
        ge=0,
        le=100,
        description="Price per copy from the bulk threshold onward, as percentage of the action base price.",
    )
    decision_timeout_seconds: int = Field(
        default=20,
        ge=5,
        le=120,
        description="Seconds the extra copies overlay is shown before automatically returning to the start screen.",
    )


class GroupPayment(BaseModel):
    """Configure the SumUp contactless payment system for the photobooth."""

    model_config = ConfigDict(title="Payment Configuration")

    enabled: bool = Field(
        default=False,
        description="Master toggle: enable the payment system. When disabled, the photobooth operates without any payment requirements.",
    )

    payment_before_capture: bool = Field(
        default=True,
        description="If true (Variant A), payment is required before the photo is taken. If false (Variant B), payment is required when the user presses 'Print'.",
    )

    # --- SumUp API credentials ---

    sumup_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="SumUp API key (starts with sk_live_... or sk_test_... for sandbox).",
    )

    @field_serializer("sumup_api_key")
    def contextual_serializer(self, value, info: SerializationInfo):
        return contextual_serializer_password(value, info)

    sumup_merchant_code: str = Field(
        default="",
        description="SumUp merchant code identifying your account.",
    )
    sumup_reader_id: str = Field(
        default="",
        description="ID of the paired SumUp Solo card reader.",
    )
    sumup_affiliate_key: str = Field(
        default="",
        description="Affiliate Key for Cloud API checkout requests. Create one at https://developer.sumup.com under Affiliate Keys.",
    )
    sumup_affiliate_app_id: str = Field(
        default="",
        description="Application ID associated with your Affiliate Key. Set this on the Affiliate Keys page at https://developer.sumup.com.",
    )

    # --- Timeouts ---

    payment_timeout_seconds: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Maximum time in seconds to wait for a payment to complete at the terminal.",
    )
    error_retry_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Seconds the error overlay is shown before automatically returning to the start screen.",
    )

    # --- Currency and UI ---

    currency: str = Field(
        default="EUR",
        min_length=3,
        max_length=3,
        description="ISO 4217 currency code (e.g. EUR, USD, GBP).",
    )
    terminal_position_hint: Literal["left", "right", "top", "bottom"] = Field(
        default="right",
        description="Position of the card terminal relative to the screen. Used to show a directional arrow in the payment overlay.",
    )

    # --- Extra copies ---

    extra_copies: ExtraCopiesConfig = ExtraCopiesConfig()
    