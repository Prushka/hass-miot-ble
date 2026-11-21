"""Deduplicate an automation's Fingerbot presses at the motor payload boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

import probatio
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.const import ATTR_ENTITY_ID, ATTR_OPTION, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_batched_platform_entity_service

from .const import DOMAIN

if TYPE_CHECKING:
    from .switch import MiotBleSwitch


class _RequestChangedError(Exception):
    """The caller's conditions changed before the payload could be sent."""


def _single_target(data: dict[str, Any]) -> dict[str, Any]:
    targets = data.get(ATTR_ENTITY_ID)
    if (
        not isinstance(targets, list)
        or len(targets) != 1
        or any(key in data for key in ("area_id", "device_id", "floor_id", "label_id"))
    ):
        raise probatio.Invalid("Target exactly one Fingerbot switch by entity ID")
    return data


@callback
def async_register_press_once(hass: HomeAssistant) -> None:
    """Register the action once and serialize requests sharing a helper."""
    locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def press_once(entities: list[MiotBleSwitch], call: ServiceCall) -> None:
        if len(entities) != 1 or not entities[0].coordinator.profile.on_demand:
            raise ServiceValidationError("Select exactly one on-demand Fingerbot switch")
        entity = entities[0]
        helper = call.data["attempt_helper"]
        previous = call.data["from_option"]
        option = call.data[ATTR_OPTION]
        required = call.data["required_states"]
        if user_id := call.context.user_id:
            user = await hass.auth.async_get_user(user_id)
            if user is None or not user.permissions.check_entity(helper, POLICY_CONTROL):
                raise Unauthorized(
                    context=call.context, entity_id=helper, permission=POLICY_CONTROL
                )
            for entity_id in required:
                if not user.permissions.check_entity(entity_id, POLICY_READ):
                    raise Unauthorized(
                        context=call.context, entity_id=entity_id, permission=POLICY_READ
                    )

        def demand_matches() -> bool:
            return all(hass.states.is_state(key, value) for key, value in required.items())

        marker_context = Context(parent_id=call.context.id, user_id=call.context.user_id)

        async def select(value: str) -> None:
            await hass.services.async_call(
                "input_select",
                "select_option",
                {ATTR_ENTITY_ID: helper, ATTR_OPTION: value},
                blocking=True,
                context=marker_context,
            )

        def owns_marker() -> bool:
            state = hass.states.get(helper)
            return (
                state is not None
                and state.state == option
                and state.context.id == marker_context.id
            )

        async def reserve() -> None:
            if not hass.states.is_state(helper, previous) or not demand_matches():
                raise _RequestChangedError
            try:
                await select(option)
                if not owns_marker() or not demand_matches() or not entity.coordinator.connected:
                    raise _RequestChangedError
            except BaseException:
                # Nothing carrying a motor command has been handed to Bluetooth yet.
                if owns_marker():
                    await select(previous)
                raise

        async with locks.setdefault(helper, asyncio.Lock()):
            state = hass.states.get(helper)
            if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
                raise ServiceValidationError("The attempt helper is unavailable")
            if previous == option or state.state != previous or not demand_matches():
                return
            if option not in state.attributes.get("options", ()):
                raise ServiceValidationError(
                    "The requested option is missing from the attempt helper"
                )
            try:
                await entity.coordinator.async_toggle_switch(
                    entity.property_spec, before_payload=reserve
                )
            except _RequestChangedError:
                return
            except HomeAssistantError:
                raise
            except Exception as error:
                raise HomeAssistantError(f"Fingerbot press failed: {error}") from error

    async_register_batched_platform_entity_service(
        hass,
        DOMAIN,
        "press_once",
        entity_domain="switch",
        func=press_once,
        schema=probatio.All(
            cv.make_entity_service_schema(
                {
                    probatio.Required("attempt_helper"): cv.entity_domain("input_select"),
                    probatio.Required("from_option"): cv.string,
                    probatio.Required(ATTR_OPTION): cv.string,
                    probatio.Optional("required_states", default=dict): {cv.entity_id: cv.string},
                }
            ),
            _single_target,
        ),
    )
