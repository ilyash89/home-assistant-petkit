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
from datetime import datetime
import urllib.parse
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# py-petkit-api Header / Client (API 13.2.1). The one account login uses
# these values. petkitaio 0.1.12 would otherwise sign in as iOS 11.2.1, and
# that session is what made CTW3 settings return Error 97.
CTW3_API_VERSION = "13.2.1"
CTW3_USER_AGENT = "okhttp/3.14.9"
CTW3_CLIENT = "android(16.1;23127PN0CG)"

# py-petkit-api BluetoothState. Poll result 0 is still connecting.
BLE_POLL_CONNECTING = 0
BLE_POLL_CONNECTED = 1
BLE_POLL_ERROR = -1
# py-petkit-api treats ble/controlDevice result 1 as the command accepted.
BLE_CONTROL_OK = 1

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
    """CMD 220 payload that selects a CTW3 working mode while powered on.

    Standard and intermittent match ``FOUNTAIN_COMMAND`` in py-petkit-api.
    Battery uses the same layout with mode byte 3; that library has no
    battery sample.
    """

    if mode not in CTW3_MODES:
        raise ValueError(f"Unsupported CTW3 mode: {mode}")
    return [CMD_POWER_MODE, 1, 3, 0, 1, mode, 1]


def power_command(turn_on: bool) -> list[int]:
    """CMD 220 power payload from py-petkit-api ``FOUNTAIN_COMMAND``.

    Power on is ``[220, 1, 3, 0, 1, 1, 1]`` and power off is
    ``[220, 1, 3, 0, 0, 1, 1]``. The mode byte is 1 in both samples.
    """

    state = 1 if turn_on else 0
    return [CMD_POWER_MODE, 1, 3, 0, state, MODE_STANDARD, 1]


def known_number(value: Any) -> int | float | None:
    """Return a usable measurement.

    PetKit sends ``-1`` when total runtime, filter days, or energy were not
    calculated. That placeholder is unknown, not a real reading.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value == -1:
        return None
    return value


def timezone_offset(timezone: str | None) -> str:
    """Hour offset string used as ``X-Timezone`` by py-petkit-api."""

    if not timezone:
        return "0.0"
    try:
        offset = datetime.now(ZoneInfo(timezone)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return "0.0"
    if offset is None:
        return "0.0"
    return str(offset.total_seconds() / 3600)


def ctw3_request_headers(
    session_header: dict[str, str], timezone: str | None
) -> dict[str, str]:
    """Session headers with the current PetKit app identity.

    Version, user agent, client, locale, and timezone fields match
    py-petkit-api 13.2.1. The caller supplies the session token. Other
    devices keep using ``create_header()`` unchanged.
    """

    headers = dict(session_header)
    headers.update(
        {
            "X-Api-Version": CTW3_API_VERSION,
            "User-Agent": CTW3_USER_AGENT,
            "X-Client": CTW3_CLIENT,
            "X-Img-Version": "1",
            "X-Locale": "en-US",
            "X-Hour": "24",
            "X-Timezone": timezone_offset(timezone),
        }
    )
    if timezone:
        headers["X-TimezoneId"] = timezone
    return headers


def ble_poll_connected(result: Any) -> bool:
    """True when ``ble/poll`` reports the relay is connected.

    py-petkit-api ``BluetoothState``: 0 connecting, 1 connected, -1 error.
    petkitaio 0.1.12 treats 0 as success; that sent CTW3 commands before the
    relay was connected, and the fountain state did not change.
    """

    return result == BLE_POLL_CONNECTED


def ble_control_accepted(result: Any) -> bool:
    """True when ``ble/controlDevice`` reports the opcode was accepted."""

    return result == BLE_CONTROL_OK


def expectation_met(payload: dict[str, Any], expect: dict[str, Any] | None) -> bool:
    """Return True when a deviceData payload contains the expected values."""

    if not expect:
        return True
    for key, value in expect.items():
        current = payload.get(key)
        if isinstance(value, dict):
            if not isinstance(current, dict):
                return False
            for inner_key, inner_value in value.items():
                if current.get(inner_key) != inner_value:
                    return False
            continue
        if current != value:
            return False
    return True


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


# One login for the whole account. py-petkit-api uses passport v1 and this
# client blob. petkitaio 0.1.12 uses /6/account/regionservers and iOS 11.2.1.
PASSPORT_REGION_SERVERS = "https://passport.petkt.com/v1/regionservers"
CHINA_GATEWAY = "https://api.petkit.cn/6/"


def modern_client_blob(timezone: str | None) -> dict[str, str]:
    """Client object py-petkit-api puts in the login form."""

    return {
        "locale": "en-US",
        "name": "23127PN0CG",
        "osVersion": "16.1",
        "phoneBrand": "Xiaomi",
        "platform": "android",
        "source": "app.petkit-android",
        "version": CTW3_API_VERSION,
        "timezoneId": timezone or "UTC",
        "timezone": timezone_offset(timezone),
    }


def modern_login_form(
    username: str,
    password_md5: str,
    region_id: str,
    timezone: str | None,
) -> dict[str, str]:
    """Login form body. ``client`` is a Python dict string, as both libraries send."""

    return {
        "client": str(modern_client_blob(timezone)),
        "encrypt": "1",
        "oldVersion": CTW3_API_VERSION,
        "password": password_md5,
        "region": region_id,
        "username": username,
    }


def regionserver_list(response: Any) -> list[Any]:
    """Accept either the raw JSON body or an already unwrapped result."""

    if not isinstance(response, dict):
        return []
    result = response.get("result", response)
    if isinstance(result, dict):
        listing = result.get("list")
        if isinstance(listing, list):
            return listing
    if isinstance(result, list):
        return result
    return []


def index_region_servers(servers: Any) -> dict[str, dict[str, str]]:
    """Map a regionservers list to ``{country name: {id, url}}``."""

    indexed: dict[str, dict[str, str]] = {}
    if not isinstance(servers, list):
        return indexed
    for server in servers:
        if not isinstance(server, dict):
            continue
        name = str(server.get("name") or "").strip()
        region_id = str(server.get("id") or "").strip().lower()
        gateway = str(server.get("gateway") or "").strip()
        if gateway and not gateway.endswith("/"):
            gateway += "/"
        if name and region_id and gateway:
            indexed[name] = {"id": region_id, "url": gateway}
    return indexed


def feeder_settings_path(device_type: str) -> str:
    """Settings path py-petkit-api uses for this feeder.

    Fresh Element (``feeder``) and D3/D4 use ``updateSettings``. The Mini
    still uses ``update``. petkitaio 0.1.12 posts Fresh Element to
    ``feeder/update``.
    """

    if device_type == "feedermini":
        return "feedermini/update"
    if device_type == "feeder":
        return "feeder/updateSettings"
    return f"{device_type}/updateSettings"


_SECRET_KEYS = {
    "password",
    "client",
    "token",
    "session",
    "data",
    "mac",
    "sn",
    "id",
    "bleid",
    "username",
    "groupid",
}


def sanitize_mapping(value: Any) -> Any:
    """Copy a request or response with secrets and device identifiers removed."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS or str(key).lower() in {"x-session", "f-session"}:
                cleaned[key] = "present" if item not in (None, "") else "missing"
                continue
            cleaned[key] = sanitize_mapping(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_mapping(item) for item in value[:8]]
    if isinstance(value, str) and len(value) > 180:
        return f"<len {len(value)}>"
    return value


def device_state_summary(payload: Any) -> dict[str, Any]:
    """Fields that show whether a CTW3 command actually landed."""

    if not isinstance(payload, dict):
        return {"result": payload}
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    return {
        "mode": payload.get("mode", status.get("mode")),
        "powerStatus": payload.get("powerStatus", status.get("powerStatus")),
        "runStatus": payload.get("runStatus", status.get("runStatus")),
        "updateAt": payload.get("updateAt"),
        "lampRingSwitch": settings.get("lampRingSwitch"),
        "lampRingBrightness": settings.get("lampRingBrightness"),
        "noDisturbingSwitch": settings.get("noDisturbingSwitch"),
    }
