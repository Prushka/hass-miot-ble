"""Connection and update coordinator for Xiaomi Miot BLE."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from bleak import BleakClient
from bleak_retry_connector import establish_connection
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_MODEL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_TOKEN, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .profile import DeviceProfile, PropertySpec
from .profiles import get_profile
from .protocol import AuthenticationError, MiotBleSession, PropertyValue
from .protocol.session import BeforePayload

_LOGGER = logging.getLogger(__name__)
ON_DEMAND_TIMEOUT = 45

type PropertyData = dict[tuple[int, int], int | bool]


def property_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, int | bool]]:
    """Return the last reported values for one config entry."""
    return Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.state")


def async_ble_device_from_address(hass: HomeAssistant, address: str):
    """Resolve a BLE device after Home Assistant loaded Bluetooth dependencies."""
    from homeassistant.components import bluetooth

    return bluetooth.async_ble_device_from_address(hass, address, connectable=True)


class MiotBleCoordinator(DataUpdateCoordinator[PropertyData]):
    """Manage one authenticated BLE connection and its property cache."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.profile: DeviceProfile = get_profile(entry.data[CONF_MODEL])
        self._token = bytes.fromhex(entry.data[CONF_TOKEN])
        self._client: BleakClient | None = None
        self._session: MiotBleSession | None = None
        self._connection_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._manually_disconnected = False
        self._store = property_store(hass, entry) if self.profile.on_demand else None
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{self.address}",
            update_interval=None
            if self.profile.on_demand
            else timedelta(seconds=DEFAULT_UPDATE_INTERVAL),
            always_update=False,
        )

    async def async_initialize(self) -> None:
        """Restore on-demand devices without opening a Bluetooth connection."""
        if self._store is None:
            await self.async_config_entry_first_refresh()
            return
        stored = await self._store.async_load() or {}
        data: PropertyData = {}
        for prop in self._properties:
            if prop.name in stored:
                value = stored[prop.name]
                try:
                    prop.validate_value(value)
                except TypeError, ValueError:
                    continue
                data[prop.key] = value
        self.async_set_updated_data(data)

    def _disconnected(self, client: BleakClient) -> None:
        if client is self._client:
            _LOGGER.warning("Xiaomi BLE device %s disconnected unexpectedly", self.address)
            session = self._session
            self._client = None
            self._session = None
            if session is not None:
                session.cancel_background_tasks()
            self.async_update_listeners()
            if self.profile.on_demand or self._manually_disconnected or self._shutdown_requested:
                return
            self.hass.async_create_task(
                self.async_refresh(),
                name=f"{DOMAIN}_reconnect_{self.address}",
            )

    @property
    def connected(self) -> bool:
        """Return whether the authenticated property channel is usable."""
        return bool(
            self._client is not None
            and self._client.is_connected
            and self._session is not None
            and self._session.authenticated
            and self._session.receiver_running
        )

    @property
    def manually_disconnected(self) -> bool:
        """Return whether automatic reconnection is paused by the user."""
        return self._manually_disconnected

    @property
    def _properties(self) -> tuple[PropertySpec, ...]:
        """Return properties that can produce local state."""
        return tuple(prop for prop in self.profile.properties if prop.readable or prop.notifiable)

    def _handle_property_values(
        self,
        session: MiotBleSession,
        values: tuple[PropertyValue, ...],
    ) -> None:
        """Publish property notifications from the active BLE session."""
        if session is not self._session:
            return
        data = dict(self.data or {})
        data.update((value.key, value.value) for value in values)
        self.async_set_updated_data(data)

    async def _ensure_session(self, *, read_only: bool = False) -> MiotBleSession:
        if self._shutdown_requested:
            raise ConnectionError("Bluetooth connection manager has stopped")
        if self._manually_disconnected and not (read_only and self.profile.on_demand):
            raise ConnectionError("Bluetooth connection is manually disconnected")
        if self.connected:
            assert self._session is not None
            self._session.raise_if_receiver_failed()
            return self._session
        async with self._connection_lock:
            if self.connected:
                assert self._session is not None
                self._session.raise_if_receiver_failed()
                return self._session
            if self._session is not None or self._client is not None:
                await self._drop_connection()
            ble_device = async_ble_device_from_address(self.hass, self.address)
            if ble_device is None:
                _LOGGER.warning(
                    "Xiaomi BLE device %s is not available to a Home Assistant "
                    "Bluetooth adapter or proxy",
                    self.address,
                )
                raise ConnectionError(
                    f"{self.address} is not in range of a Home Assistant Bluetooth adapter or proxy"
                )
            try:
                client = await establish_connection(
                    BleakClient,
                    ble_device,
                    self.address,
                    disconnected_callback=self._disconnected,
                )
            except Exception as error:
                _LOGGER.warning(
                    "Failed to establish a GATT connection to Xiaomi BLE device %s: %s",
                    self.address,
                    error,
                    exc_info=True,
                )
                raise
            session = MiotBleSession(client, self._token, self.profile)
            self._client = client
            self._session = session
            stage = "starting the Miot protocol session"
            try:
                await session.start()
                stage = "authenticating with the device"
                await session.login()
                stage = "subscribing to property notifications"
                await session.subscribe_properties(
                    self._properties,
                    lambda values: self._handle_property_values(session, values),
                )
                self.async_update_listeners()
            except (Exception, asyncio.CancelledError) as error:
                _LOGGER.warning(
                    "Xiaomi BLE device %s failed while %s: %s",
                    self.address,
                    stage,
                    error,
                    exc_info=True,
                )
                if session is self._session:
                    self._session = None
                    self._client = None
                try:
                    await session.async_close()
                finally:
                    await client.disconnect()
                raise
            return session

    @asynccontextmanager
    async def _operation_session(self, *, read_only: bool = False) -> AsyncIterator[MiotBleSession]:
        """Release an on-demand link before saving the latest device reports."""
        if self._store is None:
            try:
                yield await self._ensure_session(read_only=read_only)
            except Exception:
                await self._drop_connection()
                raise
            return
        previous = self.data
        try:
            async with asyncio.timeout(ON_DEMAND_TIMEOUT):
                yield await self._ensure_session(read_only=read_only)
        finally:
            try:
                await self._drop_connection()
            finally:
                if self.data is not None and self.data != previous:
                    await self._store.async_save(
                        {p.name: self.data[p.key] for p in self._properties if p.key in self.data}
                    )

    async def _read_current(self) -> PropertyData:
        async with self._operation_session(read_only=True) as session:
            snapshot = {key: result.value for key, result in session.property_values.items()}
            if self.profile.on_demand:
                # Notify-only readings can be absent from the next session.
                snapshot = (self.data or {}) | snapshot
                self.async_set_updated_data(snapshot)
            return snapshot

    async def _async_update_data(self) -> PropertyData:
        try:
            async with self._operation_lock:
                if self._manually_disconnected and not self.profile.on_demand:
                    return dict(self.data or {})
                return await self._read_current()
        except AuthenticationError as error:
            _LOGGER.warning(
                "Xiaomi BLE device %s rejected authentication: %s",
                self.address,
                error,
                exc_info=True,
            )
            raise ConfigEntryAuthFailed("The device rejected its BLE token") from error
        except Exception as error:
            _LOGGER.warning(
                "Xiaomi BLE update failed for %s: %s",
                self.address,
                error,
                exc_info=True,
            )
            raise UpdateFailed(str(error)) from error

    async def async_set_property(self, prop: PropertySpec, value: int | bool) -> None:
        """Write one property and update the coordinator cache."""
        async with self._operation_lock:
            attempts = 2 if prop.retry_writes else 1
            for attempt in range(attempts):
                try:
                    async with self._operation_session() as session:
                        await self._write_property(session, prop, value)
                    return
                except Exception as error:
                    _LOGGER.warning(
                        "Xiaomi BLE write failed for %s (attempt %d/%d): %s",
                        self.address,
                        attempt + 1,
                        attempts,
                        error,
                        exc_info=True,
                    )
                    if attempt + 1 == attempts:
                        raise

    async def _write_property(
        self,
        session: MiotBleSession,
        prop: PropertySpec,
        value: int | bool,
        before_payload: BeforePayload | None = None,
    ) -> None:
        changed = await session.set_property(prop, value, before_payload=before_payload)
        # A momentary control can report a newer value before this task resumes.
        changed = session.property_values.get(changed.key, changed)
        self.async_set_updated_data((self.data or {}) | {changed.key: changed.value})

    async def async_toggle_switch(
        self, prop: PropertySpec, *, before_payload: BeforePayload | None = None
    ) -> None:
        """Read and invert the on-demand switch in one authenticated session."""
        async with self._operation_lock, self._operation_session() as session:
            current = session.property_values.get(prop.key)
            if current is None or not isinstance(current.value, bool):
                raise HomeAssistantError("Device did not report its switch position")
            await self._write_property(session, prop, not current.value, before_payload)

    async def async_connect(self) -> None:
        """Read an on-demand device or resume a persistent connection."""
        async with self._operation_lock:
            if not self.profile.on_demand:
                self._manually_disconnected = False
            self.async_set_updated_data(await self._read_current())

    async def async_resume_controls(self) -> None:
        """Allow on-demand commands again without opening a connection."""
        async with self._operation_lock:
            self._manually_disconnected = False
            self.async_update_listeners()

    async def async_disconnect(self) -> None:
        """Disconnect and pause automatic reconnection."""
        async with self._operation_lock:
            self._manually_disconnected = True
            await self._drop_connection()

    async def _drop_connection(self) -> None:
        client = self._client
        session = self._session
        self._client = None
        self._session = None
        self.async_update_listeners()
        try:
            if session is not None:
                await session.async_close()
        finally:
            if client is not None and client.is_connected:
                await client.disconnect()

    async def async_shutdown(self) -> None:
        """Stop polling and disconnect from the device."""
        await super().async_shutdown()
        async with self._operation_lock:
            await self._drop_connection()
