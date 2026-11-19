"""Discover and control Eversweet Max / Max 2 (CTW3) fountains.

petkitaio 0.1.x only fetches ``W5`` fountains from ``w5/deviceData``. Max 2
devices show up on the same device roster with type ``CTW3`` and are read
from ``ctw3/deviceData``. This subclass keeps the stock client for every
other device and adds the Max family beside those results.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from typing import Any

from petkitaio import PetKitClient
from petkitaio.constants import Endpoint
from petkitaio.exceptions import BluetoothError, PetKitError
from petkitaio.model import W5Fountain

from .const import LOGGER
from .ctw3_protocol import (
    BLE_POLL_ERROR,
    CTW3_DATA_ENDPOINT,
    CTW3_MODEL_FALLBACK,
    CTW3_SETTINGS_ENDPOINT,
    CTW3_TYPE,
    DEVICE_CATALOG_ENDPOINT,
    ble_control_accepted,
    ble_poll_connected,
    command_code,
    ctw3_request_headers,
    encode_ble_frame,
    expectation_met,
    flatten_device_catalog,
    is_ctw3_type,
    model_name_from_catalog,
    normalize_ctw3_payload,
    select_ble_type,
)

# Same cadence petkitaio uses before polling a W5 through a BLE relay.
_BLE_REFRESH_SECONDS = 420
_BLE_CONNECT_ATTEMPTS = 4
# py-petkit-api polls up to 32 times, 4s apart. A shorter window still waits
# until result == 1 instead of treating "connecting" as success.
_BLE_POLL_ATTEMPTS = 8
_BLE_POLL_WAIT = 2
_STATE_PULLS = 3
_STATE_PULL_WAIT = 3


class ExtendedPetKitClient(PetKitClient):
    """PetKit client that also loads CTW3 water fountains."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_rosters: dict[Any, Any] | None = None
        self._ctw3_meta: dict[int, dict[str, Any]] = {}
        self._relay_type_codes: dict[int, Any] = {}
        self._relay_online_without_code = False
        self._device_catalog: list[dict[str, Any]] | None = None
        self._ctw3_ble_counter: dict[int, int] = {}

    async def get_device_rosters(self) -> dict[int, Any]:
        """Fetch rosters and keep the latest copy for the CTW3 pass."""

        rosters = await super().get_device_rosters()
        self._last_rosters = rosters
        return rosters

    async def get_petkit_data(self):  # type: ignore[override]
        """Return the usual PetKit data plus any CTW3 fountains."""

        data = await super().get_petkit_data()
        try:
            fountains = await self._load_ctw3_fountains()
        except Exception as err:  # noqa: BLE001 - one device family must not break the rest
            LOGGER.error("Failed to load Eversweet Max 2 (CTW3) fountains: %s", err)
            fountains = {}
        if data.water_fountains is None:
            data.water_fountains = {}
        for fountain_id, fountain in fountains.items():
            data.water_fountains[fountain_id] = fountain
        if fountains:
            LOGGER.debug(
                "Loaded %s Eversweet Max 2 (CTW3) fountain(s)", len(fountains)
            )
        return data

    async def update_ctw3_settings(self, fountain: W5Fountain, settings: dict[str, Any]) -> None:
        """Write CTW3 settings through ``ctw3/updateSettings``.

        Light, brightness, and do-not-disturb use this cloud call. It does
        not open the BLE relay. py-petkit-api posts ``id`` plus a JSON ``kv``
        object for ``DeviceCommand.UPDATE_SETTING``.
        """

        url = f"{self.base_url}{CTW3_SETTINGS_ENDPOINT}"
        header = await self._ctw3_header()
        await self._post(
            url,
            header,
            {"id": fountain.id, "kv": json.dumps(settings)},
        )
        await self._pull_device_state(fountain, header, {"settings": settings}, attempts=2, delay=1)

    async def send_ctw3_command(
        self,
        fountain: W5Fountain,
        command: list[int],
        expect: dict[str, Any] | None = None,
    ) -> None:
        """Send a CTW3 BLE opcode through the account's BLE relay.

        The frame matches py-petkit-api ``FOUNTAIN_COMMAND`` (not the W5
        layout). The relay must report poll result 1 before the opcode is
        sent, and ``ble/controlDevice`` must return result 1. The connection
        stays up while a fresh ``deviceData`` is read, then it is closed.
        ``self.ble_sequence`` is left alone so W5 commands keep their counter.
        """

        if not fountain.group_relay:
            raise PetKitError(
                f'{fountain.data.get("name", fountain.id)} has no PetKit BLE relay. '
                "Power, mode, and filter reset need a relay device that is online "
                "in the PetKit app."
            )
        ble_type = fountain.ble_relay
        if not ble_type:
            raise PetKitError(
                f'BLE type for {fountain.data.get("name", fountain.id)} is unknown, '
                "so the command was not sent."
            )
        header = await self._ctw3_header()
        ble_data = {
            "bleId": fountain.data["id"],
            "mac": fountain.data["mac"],
            "type": ble_type,
        }
        counter = self._ctw3_ble_counter.get(fountain.id, 0)
        command_data = {
            "bleId": fountain.data["id"],
            "cmd": command_code(command),
            "data": encode_ble_frame(command, counter),
            "mac": fountain.data["mac"],
            "type": ble_type,
        }
        connected = False
        try:
            connected = await self._open_ble(header, ble_data)
            if not connected:
                raise BluetoothError(
                    "BLE connection step failed while attempting to send the command to the water fountain"
                )
            response = await self._post(
                f"{self.base_url}{Endpoint.CONTROL_WF}", header, command_data
            )
            if not ble_control_accepted(response.get("result")):
                raise BluetoothError(
                    "PetKit accepted the BLE session but did not accept the fountain command "
                    f"(result={response.get('result')!r})"
                )
            self._ctw3_ble_counter[fountain.id] = counter + 1
            self.last_ble_poll[fountain.id] = datetime.now()
            await self._pull_device_state(fountain, header, expect)
        finally:
            if connected:
                try:
                    await self._post(f"{self.base_url}{Endpoint.BLE_CANCEL}", header, ble_data)
                except Exception:  # noqa: BLE001 - disconnect is best-effort
                    LOGGER.debug("CTW3 BLE disconnect failed", exc_info=True)

    async def _ctw3_header(self) -> dict[str, str]:
        """Headers for CTW3 only. Login and every other device stay on petkitaio."""

        return ctw3_request_headers(await self.create_header(), self.tz)

    async def _pull_device_state(
        self,
        fountain: W5Fountain,
        header: dict[str, str],
        expect: dict[str, Any] | None,
        attempts: int = _STATE_PULLS,
        delay: int = _STATE_PULL_WAIT,
    ) -> None:
        """Replace fountain data with a fresh deviceData read."""

        model = fountain.data.get("_model")
        latest: dict[str, Any] | None = None
        for _attempt in range(attempts):
            await asyncio.sleep(delay)
            try:
                response = await self._post(
                    f"{self.base_url}{CTW3_DATA_ENDPOINT}",
                    header,
                    {"id": fountain.id},
                )
            except Exception:  # noqa: BLE001 - the command already went out
                LOGGER.debug("CTW3 deviceData refresh failed for %s", fountain.id, exc_info=True)
                continue
            result = response.get("result")
            if not isinstance(result, dict):
                continue
            payload = normalize_ctw3_payload(result)
            if model:
                payload["_model"] = model
            fountain.data = payload
            latest = payload
            if expectation_met(payload, expect):
                return
        if expect and latest is not None:
            LOGGER.warning(
                "CTW3 %s cloud state after the command did not match %s",
                fountain.id,
                expect,
            )

    async def _load_ctw3_fountains(self) -> dict[int, W5Fountain]:
        rosters = self._last_rosters
        if not rosters:
            rosters = await self.get_device_rosters()
        header = await self.create_header()
        await self._refresh_ctw3_meta(header)
        catalog = await self._load_catalog(header)
        found: dict[int, W5Fountain] = {}
        for roster in rosters.values():
            result = roster.get("result") or {}
            has_relay = bool(result.get("hasRelay"))
            for device in result.get("devices") or []:
                if not isinstance(device, dict) or not is_ctw3_type(device.get("type")):
                    continue
                try:
                    fountain = await self._build_ctw3(
                        device, has_relay, header, catalog
                    )
                except Exception:  # noqa: BLE001 - keep the other devices
                    LOGGER.exception(
                        "Failed to load CTW3 fountain %s", device.get("id")
                    )
                    continue
                if fountain is not None:
                    found[fountain.id] = fountain
        return found

    async def _build_ctw3(
        self,
        device: dict[str, Any],
        has_relay: bool,
        header: dict[str, str],
        catalog: list[dict[str, Any]],
    ) -> W5Fountain | None:
        try:
            device_id = int(device.get("id"))
        except (TypeError, ValueError):
            return None
        device = {**device, "id": device_id}
        url = f"{self.base_url}{CTW3_DATA_ENDPOINT}"
        response = await self._post(url, header, {"id": device_id})
        result = response.get("result")
        if not isinstance(result, dict):
            raise PetKitError(f"CTW3 deviceData for {device_id} did not return an object")
        meta = self._ctw3_meta.get(int(device_id), {})
        if not meta.get("type"):
            for key in ("typeId", "deviceTypeId"):
                if device.get(key):
                    meta = {**meta, "type": device.get(key)}
                    break
        self._ctw3_meta[int(device_id)] = meta
        relay_type_code = None
        if has_relay and self.use_ble_relay:
            result, relay_type_code = await self._maybe_refresh_via_ble(
                device, result, header
            )
        payload = normalize_ctw3_payload(result)
        type_id = meta.get("type")
        type_code = payload.get("typeCode", meta.get("typeCode"))
        label = model_name_from_catalog(catalog, type_id, type_code)
        payload["_model"] = label or CTW3_MODEL_FALLBACK
        ble_type = select_ble_type(type_id, type_code, relay_type_code)
        LOGGER.debug(
            "CTW3 %s model=%s ble_type=%s type_id=%s type_code=%s",
            device_id,
            payload["_model"],
            ble_type,
            type_id,
            type_code,
        )
        raw_id = payload.get("id", device_id)
        try:
            fountain_id = int(raw_id)
        except (TypeError, ValueError):
            fountain_id = device_id
        return W5Fountain(
            id=fountain_id,
            data=payload,
            type=CTW3_TYPE,
            group_relay=has_relay,
            ble_relay=ble_type or 0,
        )

    async def _refresh_ctw3_meta(self, header: dict[str, str]) -> None:
        """Read integer type ids from the family list.

        The roster ``type`` field is the string ``CTW3``. The BLE ``type``
        form field used by py-petkit-api is the integer ``type`` on the
        account device, which only ``group/family/list`` carries.
        """

        try:
            groups = await self._post(
                f"{self.base_url}{Endpoint.FAMILY_LIST}", header, {}
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("Could not refresh CTW3 account metadata", exc_info=True)
            return
        meta: dict[int, dict[str, Any]] = {}
        relay_by_id: dict[int, int] = {}
        for group in groups.get("result") or []:
            if not isinstance(group, dict):
                continue
            for entry in group.get("deviceList") or []:
                if not isinstance(entry, dict) or entry.get("deviceId") is None:
                    continue
                device_id = int(entry["deviceId"])
                record = {
                    "type": entry.get("type"),
                    "typeCode": entry.get("typeCode"),
                    "deviceType": entry.get("deviceType"),
                }
                if is_ctw3_type(entry.get("deviceType")):
                    meta[device_id] = record
                elif entry.get("typeCode") is not None:
                    relay_by_id[device_id] = entry.get("typeCode")
        self._ctw3_meta = meta
        self._relay_type_codes = relay_by_id

    async def _load_catalog(self, header: dict[str, str]) -> list[dict[str, Any]]:
        if self._device_catalog is not None:
            return self._device_catalog
        try:
            response = await self._get(
                f"{self.base_url}{DEVICE_CATALOG_ENDPOINT}", header
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("PetKit device catalog was not available", exc_info=True)
            return []
        self._device_catalog = flatten_device_catalog(response)
        return self._device_catalog

    async def _maybe_refresh_via_ble(
        self,
        device: dict[str, Any],
        payload: dict[str, Any],
        header: dict[str, str],
    ) -> tuple[dict[str, Any], Any]:
        """Ask the relay to sync the fountain, then re-read deviceData.

        CTW3 does not get the W5-only opcodes 215 and 216. Opening the relay
        connection is what py-petkit-api does before reading the fountain.
        """

        device_id = device["id"]
        current = datetime.now()
        last = self.last_ble_poll.get(device_id)
        if last is not None and (current - last).total_seconds() < _BLE_REFRESH_SECONDS:
            return payload, None
        header = await self._ctw3_header()
        relay_type_code = await self._online_relay_type_code(device, header)
        online = relay_type_code is not None or getattr(
            self, "_relay_online_without_code", False
        )
        if not online:
            return payload, relay_type_code
        meta = self._ctw3_meta.get(int(device_id), {})
        ble_type = select_ble_type(
            meta.get("type"),
            payload.get("typeCode", meta.get("typeCode")),
            relay_type_code,
        )
        mac = payload.get("mac")
        ble_id = payload.get("id", device_id)
        if not ble_type or not mac:
            return payload, relay_type_code
        ble_data = {"bleId": ble_id, "mac": mac, "type": ble_type}
        connected = await self._open_ble(header, ble_data, poll_attempts=3)
        if not connected:
            LOGGER.debug("CTW3 BLE refresh did not connect for %s", device_id)
            return payload, relay_type_code
        try:
            await asyncio.sleep(2)
            refreshed = await self._post(
                f"{self.base_url}{CTW3_DATA_ENDPOINT}",
                header,
                {"id": device_id},
            )
            self.last_ble_poll[device_id] = datetime.now()
            result = refreshed.get("result")
            if isinstance(result, dict):
                payload = result
        finally:
            await asyncio.sleep(1)
            try:
                await self._post(f"{self.base_url}{Endpoint.BLE_CANCEL}", header, ble_data)
            except Exception:  # noqa: BLE001
                LOGGER.debug("CTW3 BLE refresh disconnect failed", exc_info=True)
        return payload, relay_type_code

    async def _online_relay_type_code(
        self, device: dict[str, Any], header: dict[str, str]
    ) -> Any:
        group_id = device.get("groupId")
        try:
            relays = await self._post(
                f"{self.base_url}{Endpoint.BLE_DEVICES}",
                header,
                {"groupId": group_id},
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("Could not list BLE relays for CTW3 %s", device.get("id"), exc_info=True)
            self._relay_online_without_code = False
            return None
        relay_codes = self._relay_type_codes
        online_code = None
        any_online = False
        for relay in relays.get("result") or []:
            if not isinstance(relay, dict) or relay.get("pim") != 1:
                continue
            any_online = True
            try:
                code = relay_codes.get(int(relay.get("id")))
            except (TypeError, ValueError):
                code = None
            if code is not None:
                online_code = code
                break
        self._relay_online_without_code = any_online and online_code is None
        return online_code

    async def _open_ble(
        self,
        header: dict[str, str],
        ble_data: dict[str, Any],
        poll_attempts: int = _BLE_POLL_ATTEMPTS,
    ) -> bool:
        """Connect and poll the relay, returning False when either step fails.

        Connect success is ``result.state == 1`` (same in petkitaio and
        py-petkit-api). Poll success is ``result == 1`` (connected). Result 0
        means the relay is still connecting, so the loop keeps waiting.
        """

        connect_url = f"{self.base_url}{Endpoint.BLE_CONNECT}"
        poll_url = f"{self.base_url}{Endpoint.BLE_POLL}"
        connected = False
        for attempt in range(1, _BLE_CONNECT_ATTEMPTS + 1):
            response = await self._post(connect_url, header, ble_data)
            result = response.get("result")
            state = result.get("state") if isinstance(result, dict) else None
            if state == 1:
                connected = True
                break
            LOGGER.debug("CTW3 BLE connect attempt %s failed", attempt)
            await asyncio.sleep(3)
        if not connected:
            return False
        for attempt in range(1, poll_attempts + 1):
            response = await self._post(poll_url, header, ble_data)
            result = response.get("result")
            if ble_poll_connected(result):
                return True
            if result == BLE_POLL_ERROR:
                LOGGER.debug("CTW3 BLE poll reported an error")
                break
            LOGGER.debug("CTW3 BLE poll attempt %s still connecting (%s)", attempt, result)
            await asyncio.sleep(_BLE_POLL_WAIT)
        try:
            await self._post(f"{self.base_url}{Endpoint.BLE_CANCEL}", header, ble_data)
        except Exception:  # noqa: BLE001
            LOGGER.debug("CTW3 BLE cancel after failed poll failed", exc_info=True)
        return False

    async def _get(self, url: str, headers: dict[str, Any]) -> dict[str, Any]:
        """GET helper. petkitaio only implements POST."""

        async with self._session.get(url, headers=headers, timeout=self.timeout) as resp:
            return await self._response(resp)
