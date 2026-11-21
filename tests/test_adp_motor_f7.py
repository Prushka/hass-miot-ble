"""Fingerbot discovery, spec permissions, and safe command behavior."""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    async_fire_time_changed,
    mock_integration,
)

from custom_components.miot_ble.config_flow import _profile_for_discovery, normalize_device_key
from custom_components.miot_ble.const import CONF_TOKEN, DOMAIN, UUID_MI_SERVICE
from custom_components.miot_ble.coordinator import MiotBleCoordinator, property_store
from custom_components.miot_ble.profile import ValueType
from custom_components.miot_ble.profiles import ADP_MOTOR_F7, get_profile, profile_for_product_id
from custom_components.miot_ble.protocol.session import (
    MiotBleError,
    MiotBleSession,
    ResponseTimeoutError,
)
from custom_components.miot_ble.protocol.tlv import PropertyValue, build_set, parse_properties
from custom_components.miot_ble.select import MiotBleSelect
from custom_components.miot_ble.sensor import MiotBleSensor, MiotBleStateSensor
from custom_components.miot_ble.switch import MiotBleSwitch

COORDINATOR = "custom_components.miot_ble.coordinator"


@pytest.fixture
def fingerbot(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Fingerbot",
        data={
            CONF_ADDRESS: "AA:BB:CC:DD:EE:FF",
            CONF_MODEL: ADP_MOTOR_F7.model,
            CONF_TOKEN: "00" * 12,
        },
    )
    entry.add_to_hass(hass)
    return entry


def test_f7_profile_respects_bluetooth_permissions() -> None:
    spec = json.loads((Path(__file__).parent / "fixtures/adp_motor_f7_spec.json").read_text())
    service = next(service for service in spec["services"] if service["iid"] == 2)
    properties = {prop["iid"]: prop for prop in service["properties"]}
    assert get_profile("adp.motor.f7") is ADP_MOTOR_F7
    assert ADP_MOTOR_F7.spec_type == spec["type"]
    assert ADP_MOTOR_F7.auth_type == 4
    assert {prop.key for prop in ADP_MOTOR_F7.properties if prop.writable} == {(2, 1), (2, 2)}
    for prop in ADP_MOTOR_F7.properties:
        public = properties[prop.piid]
        assert prop.siid == 2
        assert public["access"] == []
        assert prop.value_type.name.lower() == public["format"]
        assert prop.readable == ("read" in public["gatt-access"])
        assert prop.notifiable == ("notify" in public["gatt-access"])
        assert not prop.writable or "write" in public["gatt-access"]
    assert not ADP_MOTOR_F7.property(2, 1).retry_writes
    assert not ADP_MOTOR_F7.property(2, 2).retry_writes


def test_f7_discovery_from_observed_product_id() -> None:
    # Preserve the observed header/PID while replacing the owner's MAC.
    info = SimpleNamespace(
        name="Unknown",
        service_data={UUID_MI_SERVICE: bytes.fromhex("105b969300ffeeddccbbaa")},
    )
    assert profile_for_product_id(37782) is ADP_MOTOR_F7
    assert _profile_for_discovery(info) is ADP_MOTOR_F7
    assert normalize_device_key("ab" * 12, ADP_MOTOR_F7.model) == "ab" * 12


@pytest.mark.parametrize("length", [16, 32])
def test_f7_rejects_beacon_and_mesh_keys(length: int) -> None:
    with pytest.raises(ValueError, match="invalid format"):
        normalize_device_key("ab" * length, ADP_MOTOR_F7.model)


@pytest.mark.parametrize(
    ("piid", "value", "expected"),
    [
        pytest.param(1, True, "0c20b1000001020100010001", id="switch-on"),
        pytest.param(1, False, "0c20b1000001020100010000", id="switch-off"),
        pytest.param(2, 0, "0c20b1000001020200011000", id="click-mode"),
        pytest.param(2, 1, "0c20b1000001020200011001", id="switch-mode"),
        pytest.param(2, 2, "0c20b1000001020200011002", id="program-mode"),
    ],
)
def test_f7_property_wire_format(piid: int, value: int | bool, expected: str) -> None:
    assert build_set(0xB1, ADP_MOTOR_F7.property(2, piid), value) == bytes.fromhex(expected)


def test_f7_rejects_invalid_controls() -> None:
    with pytest.raises(TypeError, match="boolean"):
        build_set(1, ADP_MOTOR_F7.property(2, 1), 1)
    with pytest.raises(ValueError, match="value map"):
        build_set(1, ADP_MOTOR_F7.property(2, 2), 3)
    with pytest.raises(ValueError, match="not writable"):
        build_set(1, ADP_MOTOR_F7.property(2, 6), 100)


async def test_notify_only_battery_does_not_block_initial_state() -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    session._send_request = AsyncMock()
    session._receive_encrypted = AsyncMock(
        return_value=bytes.fromhex("0e20b10003010201000000010000")
    )
    values = await session.read_properties(
        (ADP_MOTOR_F7.property(2, 1), ADP_MOTOR_F7.property(2, 6))
    )
    session._send_request.assert_awaited_once_with(bytes.fromhex("0920b1000201020100"))
    assert {key: prop.value for key, prop in values.items()} == {(2, 1): False}


async def test_notify_only_battery_arrives_after_subscription() -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    session._send_request = AsyncMock()
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    session._receive_encrypted = incoming.get
    changed = asyncio.Event()
    try:
        initial = await session.subscribe_properties(
            (ADP_MOTOR_F7.property(2, 6),), lambda values: changed.set()
        )
        assert initial == {}
        session._send_request.assert_not_awaited()
        incoming.put_nowait(bytes.fromhex("0c20a1000401020600011064"))
        await asyncio.wait_for(changed.wait(), 1)
        assert session.property_values[(2, 6)].value == 100
    finally:
        await session.async_close()


@pytest.mark.parametrize("piid", [1, 2])
async def test_f7_does_not_repeat_an_unconfirmed_command(
    hass: HomeAssistant, fingerbot: MockConfigEntry, piid: int
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    coordinator.async_set_updated_data({(2, 1): False, (2, 2): 0})
    session = SimpleNamespace(
        set_property=AsyncMock(side_effect=ResponseTimeoutError("Reply lost after transmission"))
    )
    coordinator._ensure_session = AsyncMock(return_value=session)
    coordinator._drop_connection = AsyncMock()
    value = {1: True, 2: 1}[piid]
    with pytest.raises(ResponseTimeoutError, match="Reply lost"):
        await coordinator.async_set_property(ADP_MOTOR_F7.property(2, piid), value)
    session.set_property.assert_awaited_once_with(
        ADP_MOTOR_F7.property(2, piid), value, before_payload=None
    )
    coordinator._ensure_session.assert_awaited_once()
    coordinator._drop_connection.assert_awaited_once()
    assert coordinator.data == {(2, 1): False, (2, 2): 0}


async def test_f7_entities_use_device_state_and_explicit_commands() -> None:
    coordinator = SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        profile=ADP_MOTOR_F7,
        entry=SimpleNamespace(title="Fingerbot"),
        data={(2, 1): False, (2, 2): 0, (2, 6): 87, (2, 16): 2},
        last_update_success=True,
        connected=True,
        async_set_property=AsyncMock(),
    )
    switch = MiotBleSwitch(coordinator, ADP_MOTOR_F7.property(2, 1))
    mode = MiotBleSelect(coordinator, ADP_MOTOR_F7.property(2, 2))
    battery = MiotBleSensor(coordinator, ADP_MOTOR_F7.property(2, 6))
    charging = MiotBleStateSensor(coordinator, ADP_MOTOR_F7.state_sensors[0])
    assert switch.is_on is False
    assert mode.options == ["Click", "Switch", "Program"]
    assert mode.current_option == "Click"
    assert battery.native_value == 87
    assert charging.native_value == "Charged"
    coordinator.async_set_property.assert_not_awaited()
    await switch.async_turn_on()
    coordinator.async_set_property.assert_awaited_once_with(ADP_MOTOR_F7.property(2, 1), True)
    await mode.async_select_option("Switch")
    assert coordinator.async_set_property.await_args.args == (ADP_MOTOR_F7.property(2, 2), 1)
    coordinator.data = {}
    assert switch.is_on is None
    assert battery.native_value is None


def test_f7_notification_values() -> None:
    values = parse_properties(bytes.fromhex("0c20a1000401020100010000"))
    assert values[0].key == (2, 1)
    assert values[0].value is False


@pytest.mark.parametrize("echo", [True, False])
async def test_f7_mode_notification_during_channel_setup(echo: bool) -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7, timeout=0.01)
    session._send_request = AsyncMock()
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    session._receive_encrypted = incoming.get
    incoming.put_nowait(bytes.fromhex("0c2010000401020200011001"))
    responses = {True: [bytes.fromhex("0520b100f0")], False: []}
    for response in responses[echo]:
        incoming.put_nowait(response)
    await session.open_property_session()
    assert session.property_values[(2, 2)].value == 1


async def test_f7_sanitized_read_reply_and_delayed_channel_echo() -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    session._transaction_id = 0xB1
    session._send_request = AsyncMock()
    session._receive_encrypted = AsyncMock(
        side_effect=[
            bytes.fromhex("0520b100f0"),
            bytes.fromhex(
                "2920b20003040201000000010000020200000001100102100000000110010215000000045007000000"
            ),
        ]
    )
    values = await session.read_properties(ADP_MOTOR_F7.properties)
    session._send_request.assert_awaited_once_with(
        bytes.fromhex("1220b2000204020100020200021000021500")
    )
    assert {key: prop.value for key, prop in values.items()} == {
        (2, 1): False,
        (2, 2): 1,
        (2, 16): 1,
        (2, 21): 7,
    }


@pytest.mark.parametrize(
    ("piid", "value", "reply"),
    [
        pytest.param(1, True, "0b20b10001010201000000", id="boolean"),
        pytest.param(2, 1, "0b20b10001010202000000", id="integer"),
    ],
)
async def test_standard_auth_accepts_matching_set_ack(
    piid: int, value: int | bool, reply: str
) -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    session._receive_encrypted = incoming.get

    async def send(request: bytes, before_payload: object) -> None:
        incoming.put_nowait(bytes.fromhex("0b20b00001010201000000"))
        incoming.put_nowait(bytes.fromhex("0b20b10001010203000000"))
        incoming.put_nowait(bytes.fromhex(reply))

    session._send_request = AsyncMock(side_effect=send)
    session._receiver_task = asyncio.create_task(session._receive_property_notifications())
    try:
        changed = await session.set_property(ADP_MOTOR_F7.property(2, piid), value)
        assert changed.key == (2, piid)
        assert changed.value == value
        assert session.property_values[changed.key] == changed
        session._send_request.assert_awaited_once()
        assert session._pending_responses == {}
    finally:
        await session.async_close()


async def test_standard_auth_rejects_failed_set_ack() -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    session._receive_encrypted = incoming.get

    async def send(request: bytes, before_payload: object) -> None:
        incoming.put_nowait(bytes.fromhex("0b20b10001010201000300"))

    session._send_request = AsyncMock(side_effect=send)
    session._receiver_task = asyncio.create_task(session._receive_property_notifications())
    try:
        with pytest.raises(MiotBleError, match="rejected property write: 3"):
            await session.set_property(ADP_MOTOR_F7.property(2, 1), True)
        assert session.property_values == {}
        assert session._pending_responses == {}
    finally:
        await session.async_close()


async def test_poll_snapshot_cannot_confirm_a_rejected_write() -> None:
    session = MiotBleSession(AsyncMock(), bytes(12), ADP_MOTOR_F7)
    incoming: asyncio.Queue[bytes] = asyncio.Queue()
    session._receive_encrypted = incoming.get

    async def send(request: bytes, before_payload: object) -> None:
        incoming.put_nowait(bytes.fromhex("0e20b00003010201000000010001"))
        incoming.put_nowait(bytes.fromhex("0b20b10001010201000300"))

    session._send_request = AsyncMock(side_effect=send)
    session._receiver_task = asyncio.create_task(session._receive_property_notifications())
    try:
        with pytest.raises(MiotBleError, match="rejected property write: 3"):
            await session.set_property(ADP_MOTOR_F7.property(2, 1), True)
        assert session._pending_responses == {}
    finally:
        await session.async_close()


async def test_f7_newer_notification_wins_over_write_confirmation(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    session = SimpleNamespace(
        set_property=AsyncMock(return_value=PropertyValue(2, 1, ValueType.BOOL, True)),
        property_values={(2, 1): PropertyValue(2, 1, ValueType.BOOL, False)},
    )
    coordinator._ensure_session = AsyncMock(return_value=session)
    await coordinator.async_set_property(ADP_MOTOR_F7.property(2, 1), True)
    assert coordinator.data[(2, 1)] is False


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_f7_services_and_reload_while_idle(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    mock_integration(hass, MockModule("bluetooth_adapters"))
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Second Fingerbot",
        data={**fingerbot.data, CONF_ADDRESS: "11:22:33:44:55:66"},
    )
    store = property_store(hass, fingerbot)
    await store.async_save({"switch": True, "battery": 81, "mode": 123, "unknown": 9})
    registry = er.async_get(hass)
    client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    session = MagicMock(spec=MiotBleSession)
    session.property_values = {(2, 1): PropertyValue(2, 1, ValueType.BOOL, False)}
    prop = ADP_MOTOR_F7.property(2, 1)

    async def confirm_write(_prop: object, value: bool, *, before_payload: object) -> PropertyValue:
        changed = PropertyValue(2, 1, ValueType.BOOL, value)
        session.property_values[changed.key] = changed
        return changed

    session.set_property.side_effect = confirm_write
    with (
        patch(f"{COORDINATOR}.async_ble_device_from_address", return_value=MagicMock()),
        patch(f"{COORDINATOR}.establish_connection", return_value=client) as connect,
        patch(f"{COORDINATOR}.MiotBleSession", return_value=session),
    ):
        assert await hass.config_entries.async_setup(fingerbot.entry_id)
        second.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
        switch = registry.async_get_entity_id("switch", DOMAIN, "aabbccddeeff_2_1")
        second_switch = registry.async_get_entity_id("switch", DOMAIN, "112233445566_2_1")
        assert hass.states.get(second_switch).state == "unknown"
        refresh = registry.async_get_entity_id("button", DOMAIN, "aabbccddeeff_connect")
        pause = registry.async_get_entity_id("button", DOMAIN, "aabbccddeeff_disconnect")
        resume = registry.async_get_entity_id("button", DOMAIN, "aabbccddeeff_resume_controls")
        battery = registry.async_get_entity_id("sensor", DOMAIN, "aabbccddeeff_2_6")
        mode = registry.async_get_entity_id("select", DOMAIN, "aabbccddeeff_2_2")
        connection = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, "aabbccddeeff_connection"
        )
        assert hass.states.get(switch).state == "on"
        assert hass.states.get(mode).state == "unknown"
        assert hass.states.get(battery).state == "81"
        assert hass.states.get(connection).state == "off"
        assert fingerbot.runtime_data.update_interval is None
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=3))
        await hass.async_block_till_done()
        connect.assert_not_awaited()

        # The motor changed outside HA while it was asleep. Invert the live False value.
        await hass.services.async_call("switch", "toggle", {"entity_id": switch}, blocking=True)
        session.set_property.assert_awaited_once_with(prop, True, before_payload=None)
        client.disconnect.assert_awaited_once()
        await hass.services.async_call("button", "press", {"entity_id": pause}, blocking=True)
        assert hass.states.get(switch).state == "unavailable"
        session.property_values[prop.key] = PropertyValue(2, 1, ValueType.BOOL, False)
        await hass.services.async_call("button", "press", {"entity_id": refresh}, blocking=True)
        assert session.set_property.await_count == 1
        assert client.disconnect.await_count == connect.await_count == 2
        assert hass.states.get(switch).state == "unavailable"
        assert hass.states.get(battery).state == "81"
        await hass.services.async_call("button", "press", {"entity_id": resume}, blocking=True)
        assert connect.await_count == 2
        assert hass.states.get(switch).state == "off"
        assert hass.states.get(battery).state == "81"
        assert hass.states.get(connection).state == "off"

        assert await hass.config_entries.async_reload(fingerbot.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(switch).state == "off"
        assert hass.states.get(battery).state == "81"
        assert connect.await_count == 2
        assert hass.states.get(second_switch).state == "unknown"
        await hass.config_entries.async_remove(fingerbot.entry_id)
        assert await property_store(hass, fingerbot).async_load() is None


async def test_f7_unexpected_disconnect_does_not_reconnect(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    client = MagicMock()
    session = MagicMock(spec=MiotBleSession)
    coordinator._client, coordinator._session = client, session
    with patch.object(coordinator, "async_refresh") as refresh:
        coordinator._disconnected(client)
        await hass.async_block_till_done()
        refresh.assert_not_awaited()
    session.cancel_background_tasks.assert_called_once()
    assert not coordinator.connected


async def test_f7_missing_live_switch_never_writes(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    coordinator.async_set_updated_data({(2, 1): True})
    session = MagicMock(spec=MiotBleSession)
    session.property_values = {}
    with (
        patch.object(coordinator, "_ensure_session", return_value=session),
        patch.object(coordinator, "_drop_connection") as release,
        pytest.raises(HomeAssistantError, match="switch position"),
    ):
        await coordinator.async_toggle_switch(ADP_MOTOR_F7.property(2, 1))
    session.set_property.assert_not_awaited()
    release.assert_awaited_once()


async def test_f7_stalled_login_times_out_and_disconnects(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
    session = MagicMock(spec=MiotBleSession)
    session.login.side_effect = asyncio.Event().wait
    with (
        patch(f"{COORDINATOR}.ON_DEMAND_TIMEOUT", 0.02),
        patch(f"{COORDINATOR}.async_ble_device_from_address", return_value=MagicMock()),
        patch(f"{COORDINATOR}.establish_connection", return_value=client) as connect,
        patch(f"{COORDINATOR}.MiotBleSession", return_value=session),
        pytest.raises(TimeoutError),
    ):
        await coordinator.async_connect()
    connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    session.async_close.assert_awaited_once()
    session.set_property.assert_not_awaited()
    assert not coordinator.connected


async def test_f7_cancelled_command_releases_link_before_queued_refresh(
    hass: HomeAssistant, fingerbot: MockConfigEntry
) -> None:
    coordinator = MiotBleCoordinator(hass, fingerbot)
    session = MagicMock(spec=MiotBleSession)
    session.property_values = {}
    writing = asyncio.Event()
    events: list[str] = []

    async def connect(*, read_only: bool) -> MiotBleSession:
        events.append("connect")
        return session

    async def wait_for_reply(*_args: object, **_kwargs: object) -> None:
        writing.set()
        await asyncio.Event().wait()

    session.set_property.side_effect = wait_for_reply
    with (
        patch.object(coordinator, "_ensure_session", side_effect=connect),
        patch.object(coordinator, "_drop_connection", side_effect=lambda: events.append("release")),
    ):
        command = asyncio.create_task(
            coordinator.async_set_property(ADP_MOTOR_F7.property(2, 1), True)
        )
        await asyncio.wait_for(writing.wait(), 1)
        refresh = asyncio.create_task(coordinator.async_connect())
        await asyncio.sleep(0)
        assert events == ["connect"]
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        await refresh
    assert events == ["connect", "release", "connect", "release"]
    session.set_property.assert_awaited_once()
