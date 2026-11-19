"""Cloud and BLE protocol details for the PetKit Eversweet Max family.

The cloud device-type string is ``ctw3``. That is the identifier used by
Jezza34000/py-petkit-api (``CTW3 = "ctw3"`` in ``pypetkitapi/const.py``) for
the cordless Eversweet fountains whose Bluetooth name is ``Petkit_CTW3*``.
Local integrations that have been tested on an Eversweet Max 2 (including the
UVC variant) advertise that same ``Petkit_CTW3`` family
(maksym-pasichnyk/PetKit-Eversweet-Max, b12e/petkit-local). No separate cloud
type string for "Max" versus "Max 2" was found; the official catalog
``labelName`` is used when PetKit returns one.

BLE payloads follow the CTW3 CMD 220 / CMD 222 layout documented by
py-petkit-api ``FOUNTAIN_COMMAND`` and the Home Assistant PetKit commit
5c99de9: ``[cmd, type, length_lo, length_hi, state, mode, op_type]``.
The counter byte is inserted after the type byte before the frame is
base64-encoded, which is how the cloud BLE relay forwards the command.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

CTW3_TYPE = "ctw3"
CTW3_MODEL_FALLBACK = "Eversweet Max / Max 2"
CTW3_DATA_ENDPOINT = "ctw3/deviceData"
CTW3_SETTINGS_ENDPOINT = "ctw3/updateSettings"
DEVICE_CATALOG_ENDPOINT = "device/getPetkitDevices"

# Unsigned form of the petkitaio BLE header/footer (-6, -4, -3 ... -5).
BLE_START = [250, 252, 253]
BLE_END = [251]

CMD_POWER_MODE = 220
CMD_RESET_FILTER = 222

# py-petkit-api FOUNTAIN_WORKING_MODE_CTW3
MODE_STANDARD = 1
MODE_INTERMITTENT = 2
MODE_BATTERY = 3
CTW3_MODES = {
    MODE_STANDARD: "standard",
    MODE_INTERMITTENT: "intermittent",
    MODE_BATTERY: "battery",
}
CTW3_MODE_OPTIONS = ["standard", "intermittent", "battery"]

# Settings written with {type}/updateSettings. Confirmed for water fountains,
# including CTW3, by Jezza34000/homeassistant_petkit commit 5c99de9.
SETTING_LIGHT = "lampRingSwitch"
SETTING_BRIGHTNESS = "lampRingBrightness"
SETTING_DND = "noDisturbingSwitch"
SETTING_SMART_WORK = "smartWorkingTime"
SETTING_SMART_SLEEP = "smartSleepTime"
SETTING_BATTERY_WORK = "batteryWorkingTime"
SETTING_BATTERY_SLEEP = "batterySleepTime"

_STATUS_KEYS = (
    "powerStatus",
    "runStatus",
    "suspendStatus",
    "electricStatus",
    "detectStatus",
)


def is_ctw3_type(value: Any) -> bool:
    """Return True when a roster/account type string is the Max family."""

    return str(value or "").strip().lower() == CTW3_TYPE


def normalize_ctw3_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy a deviceData result into the flat shape the entities read.

    CTW3 responses may nest ``powerStatus`` / ``runStatus`` under ``status``
    (see ``WaterFountain.status_from_top_level`` in py-petkit-api). Battery
    percentage is nested under ``electricity`` on this model.
    """

    data = dict(payload)
    status = data.get("status")
    if isinstance(status, dict):
        for key in _STATUS_KEYS:
            if data.get(key) is None and status.get(key) is not None:
                data[key] = status[key]
    electricity = data.get("electricity")
    if isinstance(electricity, dict) and data.get("batteryPercent") is None:
        percent = electricity.get("batteryPercent")
        if percent is not None:
            data["batteryPercent"] = percent
    if not isinstance(data.get("settings"), dict):
        data["settings"] = {}
    return data


def flatten_device_catalog(response: Any) -> list[dict[str, Any]]:
    """Flatten ``device/getPetkitDevices`` into a list of model records."""

    result = response.get("result", response) if isinstance(response, dict) else response
    devices: list[dict[str, Any]] = []
    if not isinstance(result, list):
        return devices
    for group in result:
        if isinstance(group, dict):
            entries = group.get("devices") or []
            devices.extend(item for item in entries if isinstance(item, dict))
    return devices


def model_name_from_catalog(
    catalog: list[dict[str, Any]],
    type_id: Any,
    type_code: Any,
) -> str | None:
    """Return PetKit's label for this type id and type code, when present."""

    exact: str | None = None
    code_only: str | None = None
    for device in catalog:
        label = device.get("labelName")
        if not label:
            continue
        id_matches = type_id is not None and device.get("deviceTypeId") == type_id
        code_matches = type_code is not None and device.get("typeCode") == type_code
        if id_matches and code_matches:
            exact = str(label)
            break
        if code_matches and code_only is None and type_id is None:
            code_only = str(label)
    return exact or code_only


def select_ble_type(
    fountain_type_id: Any,
    fountain_type_code: Any = None,
    relay_type_code: Any = None,
) -> int | None:
    """Choose the ``type`` field sent to the cloud BLE relay.

    py-petkit-api sends the fountain's integer ``type`` from the account
    device list for CTW3 commands. If that id is missing, fall back to the
    concatenation described for W5 relays (relay typeCode + fountain typeCode,
    e.g. 1 and 4 -> 14). The fallback has not been confirmed on a Max 2.
    """

    try:
        type_id = int(fountain_type_id)
    except (TypeError, ValueError):
        type_id = 0
    if type_id > 0:
        return type_id
    try:
        if fountain_type_code is None or relay_type_code is None:
            return None
        return int(f"{int(relay_type_code)}{int(fountain_type_code)}")
    except (TypeError, ValueError):
        return None


def mode_command(mode: int) -> list[int]:
    """CMD 220 payload that selects a CTW3 working mode while powered on."""

    if mode not in CTW3_MODES:
        raise ValueError(f"Unsupported CTW3 mode: {mode}")
    return [CMD_POWER_MODE, 1, 3, 0, 1, mode, 1]


def power_command(turn_on: bool, mode: int) -> list[int]:
    """CMD 220 power payload.

    ``state`` is 1 when turning on and 0 when turning off. The mode byte is
    kept so the command does not rewrite Standard / Intermittent / Battery.
    Upstream's stock POWER_OFF sample hardcodes mode 1; preserving the current
    mode is an assumption and was not checked on hardware.
    """

    if mode not in CTW3_MODES:
        mode = MODE_STANDARD
    state = 1 if turn_on else 0
    return [CMD_POWER_MODE, 1, 3, 0, state, mode, 1]


def reset_filter_command() -> list[int]:
    """CMD 222, empty payload, matching FOUNTAIN_COMMAND RESET_FILTER."""

    return [CMD_RESET_FILTER, 1, 0, 0]


def encode_ble_frame(command: list[int], counter: int) -> str:
    """Insert the rolling counter and return the URL-encoded base64 frame."""

    modified = [*command[:2], counter, *command[2:]]
    frame = [*BLE_START, *modified, *BLE_END]
    raw = bytearray(value % 256 for value in frame)
    encoded = base64.b64encode(raw)
    return urllib.parse.quote(encoded)


def command_code(command: list[int]) -> str:
    """Cloud ``cmd`` form field (the BLE opcode as a decimal string)."""

    return str(command[0])
