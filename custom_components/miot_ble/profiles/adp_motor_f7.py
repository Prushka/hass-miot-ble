"""Adaprox Fingerbot Pro properties from Xiaomi's Bluetooth-specific spec."""

from ..profile import DeviceProfile, EntityKind, PropertySpec, StateSensorSpec, ValueType

PROFILE = DeviceProfile(
    model="adp.motor.f7",
    name="Adaprox Fingerbot Pro",
    spec_type="urn:miot-spec-v2:device:motor-controller:0000A01D:adp-f7:1",
    auth_type=4,
    properties=(
        PropertySpec(
            name="switch",
            siid=2,
            piid=1,
            value_type=ValueType.BOOL,
            entity_kind=EntityKind.SWITCH,
            writable=True,
            notifiable=True,
            # In Click mode a repeated write can cause a second physical press.
            retry_writes=False,
        ),
        PropertySpec(
            name="mode",
            siid=2,
            piid=2,
            value_type=ValueType.UINT8,
            entity_kind=EntityKind.SELECT,
            writable=True,
            notifiable=True,
            value_map=((0, "Click"), (1, "Switch"), (2, "Program")),
            retry_writes=False,
        ),
        PropertySpec(
            name="battery",
            siid=2,
            piid=6,
            value_type=ValueType.UINT8,
            entity_kind=EntityKind.SENSOR,
            readable=False,
            notifiable=True,
            minimum=0,
            maximum=100,
            unit="%",
        ),
        PropertySpec(
            name="charging_status",
            siid=2,
            piid=16,
            value_type=ValueType.UINT8,
            entity_kind=EntityKind.SENSOR,
            notifiable=True,
            value_map=((0, "Not charging"), (1, "Charging"), (2, "Charged")),
        ),
        PropertySpec(
            name="total_movement",
            siid=2,
            piid=21,
            value_type=ValueType.UINT32,
            entity_kind=EntityKind.SENSOR,
            notifiable=True,
            minimum=0,
            maximum=0xFFFFFFFF,
        ),
    ),
    state_sensors=(
        StateSensorSpec(
            name="charging_state",
            source=(2, 16),
            value_map=((0, "Not charging"), (1, "Charging"), (2, "Charged")),
        ),
    ),
)
