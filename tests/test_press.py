"""Exercise the demand script and the Fingerbot action through Home Assistant."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import probatio
import pytest
from homeassistant.components.blueprint.models import Blueprint, BlueprintInputs
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.const import CONF_ADDRESS, CONF_MODEL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    mock_integration,
)

from custom_components.miot_ble.const import CONF_TOKEN, DOMAIN, UUID_MIOT_WRITE
from custom_components.miot_ble.profile import PropertySpec, ValueType
from custom_components.miot_ble.profiles import ADP_MOTOR_F7
from custom_components.miot_ble.protocol.session import (
    BeforePayload,
    MiotBleSession,
    ResponseTimeoutError,
)
from custom_components.miot_ble.protocol.tlv import PropertyValue

ATTEMPT = "input_select.attempt"
DEMAND = "input_boolean.demand"
MASTER = "input_boolean.enabled"
ON = "switch.first_switch"
OFF = "switch.second_switch"
COORDINATOR = "custom_components.miot_ble.coordinator"


@pytest.fixture
async def radio(hass: HomeAssistant, enable_custom_integrations: None) -> AsyncIterator[MagicMock]:
    mock_integration(hass, MockModule("bluetooth_adapters"))
    radio = MagicMock()
    client = MagicMock(is_connected=True, disconnect=AsyncMock())
    radio.connect = AsyncMock(return_value=client)
    radio.session = MagicMock(spec=MiotBleSession)
    radio.session.authenticated = radio.session.receiver_running = True
    radio.session.property_values = {(2, 1): PropertyValue(2, 1, ValueType.BOOL, False)}
    radio.payload = AsyncMock()

    async def send(
        prop: PropertySpec, value: bool, *, before_payload: BeforePayload
    ) -> PropertyValue:
        await before_payload()
        await radio.payload(prop, value, hass.states.get(ATTEMPT).state)
        result = PropertyValue(prop.siid, prop.piid, prop.value_type, value)
        radio.session.property_values[prop.key] = result
        return result

    radio.session.set_property.side_effect = send
    with (
        patch(f"{COORDINATOR}.async_ble_device_from_address", return_value=MagicMock()),
        patch(f"{COORDINATOR}.establish_connection", radio.connect),
        patch(f"{COORDINATOR}.MiotBleSession", return_value=radio.session),
    ):
        for name, address in (("First", "00:11:22:33:44:55"), ("Second", "00:11:22:33:44:66")):
            entry = MockConfigEntry(
                domain=DOMAIN,
                title=name,
                data={
                    CONF_ADDRESS: address,
                    CONF_MODEL: ADP_MOTOR_F7.model,
                    CONF_TOKEN: "00" * 12,
                },
            )
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        assert await async_setup_component(
            hass, "input_select", {"input_select": {"attempt": {"options": ["none", "on", "off"]}}}
        )
        path = Path(__file__).parents[1] / "blueprints/script/miot_ble/press_for_binary_demand.yaml"
        source = await hass.async_add_executor_job(load_yaml, str(path))
        blueprint = Blueprint(source, expected_domain="script", schema=BLUEPRINT_SCHEMA)
        inputs = BlueprintInputs(
            blueprint,
            {
                "use_blueprint": {
                    "path": path.name,
                    "input": {
                        "demand": DEMAND,
                        "on_switch": ON,
                        "off_switch": OFF,
                        "attempt_helper": ATTEMPT,
                        "enabled_by": [MASTER],
                    },
                }
            },
        )
        inputs.validate()
        script = inputs.async_substitute()
        assert await async_setup_component(
            hass, "script", {"script": {"apply_test_demand": script}}
        )
        hass.states.async_set(MASTER, "on")
        hass.states.async_set(DEMAND, "off")
        await hass.async_block_till_done()
        yield radio


async def apply_demand(hass: HomeAssistant) -> None:
    await hass.services.async_call("script", "apply_test_demand", blocking=True)


async def test_cold_pair_retries_only_an_unsent_demand(
    hass: HomeAssistant, radio: MagicMock
) -> None:
    assert hass.states.get(ON).state == hass.states.get(OFF).state == "unknown"
    await apply_demand(hass)
    radio.connect.assert_not_awaited()
    hass.states.async_set(DEMAND, "on")
    radio.connect.side_effect = ConnectionError("out of range")
    with pytest.raises(HomeAssistantError, match="out of range"):
        await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == "none"
    radio.payload.assert_not_awaited()

    radio.connect.side_effect = None
    await apply_demand(hass)
    await apply_demand(hass)
    radio.payload.assert_awaited_once_with(ADP_MOTOR_F7.property(2, 1), True, "on")
    assert radio.connect.await_count == 2
    hass.states.async_set(DEMAND, "off")
    await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == "off"
    assert radio.payload.await_args.args[-1] == "off"
    assert radio.payload.await_count == 2


async def test_lost_reply_never_replays_payload(hass: HomeAssistant, radio: MagicMock) -> None:
    hass.states.async_set(DEMAND, "on")
    radio.payload.side_effect = ResponseTimeoutError("lost acknowledgement")
    with pytest.raises(HomeAssistantError, match="lost acknowledgement"):
        await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == "on"
    await apply_demand(hass)
    radio.connect.assert_awaited_once()
    radio.payload.assert_awaited_once()


@pytest.mark.parametrize("guard", [DEMAND, MASTER])
async def test_guards_rechecked_after_authentication(
    hass: HomeAssistant, radio: MagicMock, guard: str
) -> None:
    hass.states.async_set(DEMAND, "on")
    radio.session.login.side_effect = lambda: hass.states.async_set(guard, "off")
    await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == "none"
    radio.payload.assert_not_awaited()


async def test_concurrent_calls_share_the_helper(hass: HomeAssistant, radio: MagicMock) -> None:
    data = {"attempt_helper": ATTEMPT, "from_option": "none", "option": "on"}
    await asyncio.gather(
        *(
            hass.services.async_call(
                DOMAIN, "press_once", {**data, "entity_id": bot}, blocking=True
            )
            for bot in (ON, OFF)
        )
    )
    radio.payload.assert_awaited_once()
    radio.connect.assert_awaited_once()


@pytest.mark.parametrize("denied", [ATTEMPT, MASTER])
async def test_action_honors_helper_and_guard_permissions(
    hass: HomeAssistant, radio: MagicMock, denied: str
) -> None:
    user = MagicMock(is_admin=False)
    user.permissions.check_entity.side_effect = lambda entity, policy: entity != denied
    with (
        patch.object(type(hass.auth), "async_get_user", return_value=user),
        pytest.raises(Unauthorized),
    ):
        await hass.services.async_call(
            DOMAIN,
            "press_once",
            {
                "entity_id": ON,
                "attempt_helper": ATTEMPT,
                "from_option": "none",
                "option": "on",
                "required_states": {MASTER: "on"},
            },
            blocking=True,
            context=Context(user_id="test-user"),
        )
    assert hass.states.get(ATTEMPT).state == "none"
    radio.connect.assert_not_awaited()


async def test_multiple_targets_rejected_before_availability_filter(
    hass: HomeAssistant, radio: MagicMock
) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.first_pause_controls"}, blocking=True
    )
    with pytest.raises(probatio.Invalid, match="exactly one"):
        await hass.services.async_call(
            DOMAIN,
            "press_once",
            {
                "entity_id": [ON, OFF],
                "attempt_helper": ATTEMPT,
                "from_option": "none",
                "option": "on",
            },
            blocking=True,
        )
    radio.connect.assert_not_awaited()


async def test_failed_paused_refresh_leaves_demand_pending(
    hass: HomeAssistant, radio: MagicMock
) -> None:
    hass.states.async_set(DEMAND, "on")
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.first_pause_controls"}, blocking=True
    )
    radio.connect.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError, match="offline"):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.first_refresh_status"}, blocking=True
        )
    assert hass.states.get(ON).state == "unavailable"
    await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == "none"
    radio.connect.assert_awaited_once()
    radio.payload.assert_not_awaited()


@pytest.mark.parametrize("interruption", ["cancel", "demand", "lost link", "other marker"])
async def test_reservation_interrupted_before_payload(
    hass: HomeAssistant, radio: MagicMock, interruption: str
) -> None:
    hass.states.async_set(DEMAND, "on")
    original_call = hass.services.async_call

    async def intercept(
        domain: str, service: str, service_data: dict | None = None, **kwargs: object
    ) -> object:
        result = await original_call(domain, service, service_data, **kwargs)
        if domain == "input_select" and service_data["option"] == "on":
            if interruption == "cancel":
                raise asyncio.CancelledError
            if interruption == "demand":
                hass.states.async_set(DEMAND, "off")
            if interruption == "lost link":
                radio.session.receiver_running = False
            if interruption == "other marker":
                await original_call(
                    "input_select",
                    "select_option",
                    {"entity_id": ATTEMPT, "option": "off"},
                    blocking=True,
                )
        return result

    with patch.object(type(hass.services), "async_call", staticmethod(intercept)):
        if interruption == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await hass.services.async_call(
                    DOMAIN,
                    "press_once",
                    {
                        "entity_id": ON,
                        "attempt_helper": ATTEMPT,
                        "from_option": "none",
                        "option": "on",
                        "required_states": {DEMAND: "on"},
                    },
                    blocking=True,
                )
        else:
            await apply_demand(hass)
    assert hass.states.get(ATTEMPT).state == ("off" if interruption == "other marker" else "none")
    radio.payload.assert_not_awaited()


@pytest.mark.parametrize("failure", ["header", "ready", "reserve", "payload", "ack", "none"])
async def test_real_standard_auth_payload_boundary(failure: str) -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    session._encrypt = MagicMock(return_value=b"payload")
    stages: list[str] = []

    def step(name: str) -> None:
        stages.append(name)
        if name == failure:
            raise ConnectionError(name)

    async def write(uuid: str, data: bytes) -> None:
        assert uuid == UUID_MIOT_WRITE
        step("payload" if data.endswith(b"payload") else "header")

    async def wait(_uuid: str, expected: bytes) -> None:
        step("ready" if expected == bytes.fromhex("00000101") else "ack")

    async def reserve() -> None:
        step("reserve")

    session._write, session._wait_data = write, wait
    sequence = ["header", "ready", "reserve", "payload", "ack"]
    if failure == "none":
        await session._send_request(b"command", reserve)
        assert stages == sequence
    else:
        with pytest.raises(ConnectionError, match=failure):
            await session._send_request(b"command", reserve)
        assert stages == sequence[: sequence.index(failure) + 1]
