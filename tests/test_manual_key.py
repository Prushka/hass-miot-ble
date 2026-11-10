"""Validate local-key setup without exposing credentials in forms."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ADDRESS, CONF_MODEL, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.miot_ble.config_flow import MiotBleConfigFlow
from custom_components.miot_ble.const import CONF_TOKEN
from custom_components.miot_ble.profile import DeviceProfile
from custom_components.miot_ble.profiles import ADP_MOTOR_F7, SOOCARE_MAGIC_TOUCH_M14
from custom_components.miot_ble.protocol import AuthenticationError

ADDRESS = "AA:BB:CC:DD:EE:FF"
KEY = "abcdef0123456789abcdef01"


def make_flow(hass: HomeAssistant) -> MiotBleConfigFlow:
    flow = MiotBleConfigFlow()
    flow.hass = hass
    flow.context = {"source": SOURCE_USER}
    return flow


@pytest.mark.parametrize(
    ("profile", "key"),
    [
        pytest.param(ADP_MOTOR_F7, KEY, id="standard-auth"),
        pytest.param(SOOCARE_MAGIC_TOUCH_M14, "ab" * 32, id="security-chip"),
    ],
)
async def test_existing_key_authenticates_without_cloud(
    hass: HomeAssistant, profile: DeviceProfile, key: str
) -> None:
    flow = make_flow(hass)
    with (
        patch(
            "custom_components.miot_ble.config_flow._discovered_devices",
            return_value={ADDRESS: (profile.name, profile)},
        ),
        patch(
            "custom_components.miot_ble.config_flow._cloud_address_for_address", return_value=None
        ),
        patch("custom_components.miot_ble.config_flow.async_validate_connection") as validate,
        patch("custom_components.miot_ble.config_flow.get_cloud_account_store") as cloud_store,
    ):
        result = await flow.async_step_user(
            {CONF_ADDRESS: ADDRESS, CONF_NAME: "Test device", CONF_TOKEN: f"  {key.upper()}  "}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_MODEL: profile.model,
        CONF_NAME: "Test device",
        CONF_TOKEN: key,
    }
    validate.assert_awaited_once_with(hass, ADDRESS, bytes.fromhex(key), profile.model)
    cloud_store.assert_not_called()


async def test_invalid_key_stays_in_form_without_echoing_secret(hass: HomeAssistant) -> None:
    flow = make_flow(hass)
    with (
        patch(
            "custom_components.miot_ble.config_flow._discovered_devices",
            return_value={ADDRESS: (ADP_MOTOR_F7.name, ADP_MOTOR_F7)},
        ),
        patch(
            "custom_components.miot_ble.config_flow._cloud_address_for_address", return_value=None
        ),
        patch("custom_components.miot_ble.config_flow.async_validate_connection") as validate,
    ):
        result = await flow.async_step_user({CONF_ADDRESS: ADDRESS, CONF_TOKEN: "invalid-test-key"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TOKEN: "invalid_key"}
    marker = next(key for key in result["data_schema"].schema if key.schema == CONF_TOKEN)
    assert marker.default is vol.UNDEFINED
    assert "invalid-test-key" not in str(result)
    validate.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        pytest.param(AuthenticationError("Rejected test key"), "invalid_auth", id="auth"),
        pytest.param(ConnectionError("Device offline"), "cannot_connect", id="offline"),
    ],
)
async def test_failed_key_validation_does_not_create_entry(
    hass: HomeAssistant, error: Exception, error_key: str
) -> None:
    flow = make_flow(hass)
    with (
        patch(
            "custom_components.miot_ble.config_flow._discovered_devices",
            return_value={ADDRESS: (ADP_MOTOR_F7.name, ADP_MOTOR_F7)},
        ),
        patch(
            "custom_components.miot_ble.config_flow._cloud_address_for_address", return_value=None
        ),
        patch(
            "custom_components.miot_ble.config_flow.async_validate_connection",
            new=AsyncMock(side_effect=error),
        ),
    ):
        result = await flow.async_step_user({CONF_ADDRESS: ADDRESS, CONF_TOKEN: KEY})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error_key}
    marker = next(key for key in result["data_schema"].schema if key.schema == CONF_TOKEN)
    assert marker.default is vol.UNDEFINED
    assert KEY not in str(result)
