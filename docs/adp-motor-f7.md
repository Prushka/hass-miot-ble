# Adaprox Fingerbot Pro (`adp.motor.f7`)

## Implementation status

This checkout adds a profile for direct Bluetooth control through Home Assistant's
adapter/proxy selection. On firmware `2.1.0_0119`, a USB adapter successfully
connected, authenticated with the existing 12-byte Xiaomi token, opened an encrypted
property session, and read device status. No gateway was involved in that exchange.
Motor and mode writes are implemented and unit-tested, but have not been exercised
on the physical device. This remains experimental until a supervised actuation test.

The initial hardware probe loaded protocol source into a temporary Python process, read the
existing Xiaomi credential in memory, allowed only property-read/channel-open
opcodes, and disconnected. It did not pair, rebind, change device settings, or
issue actions. Credentials are not part of this repository or the fixtures.

## Sources and project choice

- Base: [orangeboyChen/hass-miot-ble](https://github.com/orangeboyChen/hass-miot-ble),
  MIT license, commit `397874b9eb0f75c8c93cde13cd37f8301e57debd`. It already provides
  Standard Auth, encrypted transport, discovery, account setup, and HA entities.
  Fork: [Prushka/hass-miot-ble](https://github.com/Prushka/hass-miot-ble).
- [Xiaomi's full device specification](https://miot-spec.org/miot-spec-v2/instance?type=urn:miot-spec-v2:device:motor-controller:0000A01D:adp-f7:1),
  retrieved 2026-11-10; saved as `tests/fixtures/adp_motor_f7_spec.json`.
- [E7G/xiaomi-ble-control](https://github.com/E7G/xiaomi-ble-control) is another
  Standard Auth controller, currently targeting a fan. It adds no needed capability
  beyond the selected base, so it was not copied into this fork.
- [MIoT BLE SDK protocol notes](https://github.com/haohaozi328-arch/miot-ble-sdk/blob/main/docs/protocol.md)
  describe the separate GATT permissions and Spec-v2 write acknowledgements.
  This is a device-side SDK with a restrictive license; no SDK implementation code
  was imported. A new generic action transport is unnecessary for this profile.

## Exposed properties

| HA entity | SIID / PIID | Type | Access in this integration |
| --- | --- | --- | --- |
| Switch | 2 / 1 | Boolean | Read, write, notify |
| Mode | 2 / 2 | uint8 | Read, write, notify; Click (0), Switch (1), Program (2) |
| Battery | 2 / 6 | uint8 | Notify only, 0–100% |
| Charging status | 2 / 16 | uint8 | Read, notify; 0 none, 1 charging, 2 charged |
| Total movement | 2 / 21 | uint32 | Read, notify |

A derived Charging state sensor displays the charging enumeration as text.
Although the public spec permits writes to several other properties, this profile
does not expose arm travel, resistance, calibration, clock, schedules, or counters
as writable controls. Setup, reconnect, and status polling do not write any property.
Mode changes only happen after an explicit select command.

The switch value is the Fingerbot's raw logical property. It is not feedback from
the appliance it presses and does not establish whether an exhaust fan is running.
In Click mode, the physical behavior of both Boolean writes still needs validation.
Battery can remain unknown until the device publishes a battery notification.

## Read-only protocol evidence

The observed FE95 header starts `10 5b 96 93 00`; the little-endian product ID is
`0x9396`. Tests replace the owner's MAC with a synthetic address.

The following examples preserve the observed message structure and ordering.
Addresses and device state values are replaced with synthetic test data:

```text
TX channel open:  05 20 b1 00 f0
RX mode notify:   0c 20 b2 00 04 01 02 02 00 01 10 01
TX property get:  12 20 b2 00 02 04 02 01 00 02 02 00 02 10 00 02 15 00
RX delayed echo:  05 20 b1 00 f0
RX read reply:    29 20 b2 00 03 04 02 01 00 00 00 01 00 00
                 02 02 00 00 00 01 10 01 02 10 00 00 00 01 10 01
                 02 15 00 00 00 04 50 07 00 00 00
```

The synthetic reply decodes to switch `false`, mode `1` (Switch), charging status
`1`, and total movement `7`. Actual device readings are not included in this repository.
One reconnect attempt timed out; a subsequent connection through the already-known
BlueZ device path succeeded. Range/reconnection reliability needs longer observation.

## Transport changes

- Preserve property notifications received during channel opening; accept a delayed
  channel echo during the initial read. The existing one-second optional echo wait
  remains bounded even if notifications continue arriving.
- Exclude notify-only properties from the initial read request so a missing battery
  snapshot cannot prevent setup. They still update through GATT notifications.
- Accept transaction- and property-matched Spec-v2 write acknowledgements for
  Standard Auth as well as security-chip sessions. Propagate device rejection codes.
  This acknowledgement path is unit-tested, not captured from an F7 write.
- Do not retry F7 switch or mode commands after an error. A timeout can mean the
  motor received a command but its reply was lost. Inspect the device before retrying.
- Keep a newer notification if it arrives before the write caller resumes.
- Discard stale authentication parcels that arrived before the next transfer
  header. A duplicated 16-byte challenge fragment could otherwise be combined with
  part of the proof, producing a 30-byte payload where 32 bytes are required.
  A synthetic retransmission test reproduces the failure and verifies the fix.
- Refresh readable F7 properties every 60 seconds on an established connection.
  This keeps readings current and avoids leaving the connection idle indefinitely.
  The poll uses only GetProperty requests; notify-only battery is excluded.

## Local validation and installation

The repository requires Python 3.14 and `uv`. The Home Assistant test plugin imports
Linux-only modules, so the complete suite runs in an isolated Linux virtual
environment, separate from the production Home Assistant environment:

```sh
uv sync --locked
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest
```

Tests cover real advertisement discovery and read-response captures, GATT access
permissions, Boolean/enum wire encoding, late battery notifications, startup ordering,
write acknowledgements/rejections, and prevention of duplicate motor writes.
Validation on 2026-11-10: 251 tests passed, 92.02% combined line/branch coverage;
Ruff lint and formatting checks passed. The unchanged upstream baseline passed
217 tests with 90.69% coverage in the same Linux environment.

When ready for a supervised test, install `custom_components/miot_ble` from this
checkout using the manual installation steps in the README, then add Xiaomi Miot
BLE and select the discovered Fingerbot. The existing Xiaomi binding and token are
used; resetting or pairing the device again is unnecessary. If you already have
the device key, the optional Existing Bluetooth key field allows setup without
a new Xiaomi sign-in. Keys are stored only in Home Assistant's local config entry
and are not echoed into setup forms after validation errors. Verify status first.
Keep the physical control visible for the first explicit command and verify its
effect before connecting it to an automation. The upstream HACS release does not
contain this experimental profile.

Version 0.12.0 accepts `xiaomi-ble>=1.21.2,<2`, avoiding a downgrade of the newer
library bundled with Home Assistant 2026.11. Standard Auth and security-chip key
formats are both covered by setup tests.
