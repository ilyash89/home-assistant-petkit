"""Discover and control Eversweet Max / Max 2 (CTW3) fountains.

petkitaio 0.1.x only fetches ``W5`` fountains from ``w5/deviceData``. Max 2
devices show up on the same device roster with type ``CTW3`` and are read
from ``ctw3/deviceData``. This subclass keeps the stock client for every
other device and adds the Max family beside those results.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from typing import Any

from petkitaio import PetKitClient
from petkitaio.constants import Endpoint
from petkitaio.exceptions import BluetoothError, PetKitError
from petkitaio.model import W5Fountain

from .const import LOGGER
from .ctw3_protocol import (
    BLE_POLL_ERROR,
    CHINA_GATEWAY,
    CTW3_DATA_ENDPOINT,
    CTW3_MODEL_FALLBACK,
    CTW3_SETTINGS_ENDPOINT,
    CTW3_TYPE,
    DEVICE_CATALOG_ENDPOINT,
    ble_control_accepted,
    ble_poll_connected,
    command_code,
    ctw3_request_headers,
    device_state_summary,
    encode_ble_frame,
    expectation_met,
    flatten_device_catalog,
    gateway_for_region,
    is_ctw3_type,
    model_name_from_catalog,
    modern_login_form,
    normalize_ctw3_payload,
    PASSPORT_REGION_SERVERS,
    regionserver_list,
    sanitize_mapping,
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
        self._control_token: str | None = None
        self._control_token_expiration: datetime | None = None
        self._control_base_url: str | None = None
        self._last_poll_results: list[Any] = []
        self._last_connect_states: list[Any] = []

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

        header, base_url = await self._control_header()
        url = f"{base_url}{CTW3_SETTINGS_ENDPOINT}"
        body = {"id": fountain.id, "kv": json.dumps(settings)}
        LOGGER.warning(
            "CTW3 settings write keys=%s session=api-%s",
            settings,
            header.get("X-Api-Version"),
        )
        await self._exchange("updateSettings", url, header, body)
        await self._pull_device_state(
            fountain, header, {"settings": settings}, attempts=2, delay=1, base_url=base_url
        )

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
        header, base_url = await self._control_header()
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
        LOGGER.warning(
            "CTW3 BLE start cmd=%s type=%s type_source=%s counter=%s bytes=%s session=api-%s",
            command_data["cmd"],
            ble_type,
            fountain.data.get("_ble_type_source", "not recorded"),
            counter,
            command,
            header.get("X-Api-Version"),
        )
        await self._log_relay_type_ids(header, base_url, fountain.id)
        connected = False
        control_result: Any = None
        self._last_poll_results = []
        self._last_connect_states = []
        try:
            connected = await self._open_ble(header, ble_data, base_url=base_url)
            if not connected:
                raise BluetoothError(
                    "BLE connection step failed while attempting to send the command to the water fountain. "
                    f"connect states={self._last_connect_states!r} poll results={self._last_poll_results!r}"
                )
            response = await self._exchange(
                "controlDevice",
                f"{base_url}{Endpoint.CONTROL_WF}",
                header,
                command_data,
            )
            control_result = response.get("result")
            if not ble_control_accepted(control_result):
                raise BluetoothError(
                    "PetKit accepted the BLE session but did not accept the fountain command "
                    f"(result={control_result!r}). result=1 only means the relay accepted the "
                    "packet in py-petkit-api; this response was not 1."
                )
            self._ctw3_ble_counter[fountain.id] = counter + 1
            self.last_ble_poll[fountain.id] = datetime.now()
            await self._pull_device_state(
                fountain, header, expect, base_url=base_url
            )
        finally:
            if connected:
                try:
                    await self._exchange(
                        "ble/cancel",
                        f"{base_url}{Endpoint.BLE_CANCEL}",
                        header,
                        ble_data,
                    )
                except Exception:  # noqa: BLE001 - disconnect is best-effort
                    LOGGER.debug("CTW3 BLE disconnect failed", exc_info=True)
                await self._pull_device_state(
                    fountain, header, None, attempts=1, delay=1, base_url=base_url
                )
            LOGGER.warning(
                "CTW3 BLE done cmd=%s type=%s connect=%s poll=%s control=%s state=%s. "
                "control result 1 means the relay accepted the packet.",
                command_data["cmd"],
                ble_type,
                self._last_connect_states,
                self._last_poll_results,
                control_result,
                device_state_summary(fountain.data),
            )

    async def _ctw3_header(self) -> dict[str, str]:
        """Headers for background CTW3 reads. The shared login is left as-is."""

        return ctw3_request_headers(await self.create_header(), self.tz)

    async def _control_header(self) -> tuple[dict[str, str], str]:
        """Headers and base URL for a CTW3 write.

        The shared petkitaio session was created as app 11.2.1. Putting
        13.2.1 on that session still returns Error 97, so commands use a
        second login that matches py-petkit-api. The original token is not
        replaced.
        """

        await self._ensure_control_session()
        header = ctw3_request_headers(
            {
                "Accept": "*/*",
                "Accept-Language": "en-US;q=1, it-US;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Session": self._control_token or "",
                "F-Session": self._control_token or "",
            },
            self.tz,
        )
        return header, self._control_base_url or self.base_url

    async def _ensure_control_session(self) -> None:
        """Log in once as API 13.2.1 without touching the petkitaio token."""

        now = datetime.now()
        if (
            self._control_token
            and self._control_token_expiration
            and self._control_token_expiration > now + timedelta(seconds=60)
        ):
            return
        saved_token = self.token
        gateway, region_id = await self._control_gateway()
        form = modern_login_form(
            self.username,
            hashlib.md5(self.password.encode()).hexdigest(),
            region_id,
            self.tz,
        )
        header = ctw3_request_headers(
            {
                "Accept": "*/*",
                "Accept-Language": "en-US;q=1, it-US;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            self.tz,
        )
        url = f"{gateway}user/login"
        LOGGER.warning(
            "CTW3 opening a separate API %s session at %s region=%s. "
            "The petkitaio session is left in place. If other devices then "
            "stop updating, PetKit allowed only one session and this login replaced it.",
            header.get("X-Api-Version"),
            gateway.split("/")[2] if "://" in gateway else gateway,
            region_id,
        )
        response = await self._exchange("login", url, header, form)
        session = (response.get("result") or {}).get("session") or {}
        token = session.get("id")
        if not token:
            raise PetKitError("CTW3 modern login did not return a session")
        self._control_token = token
        self._control_base_url = gateway
        expires_in = int(session.get("expiresIn") or 3600)
        self._control_token_expiration = now + timedelta(seconds=expires_in)
        if self.token != saved_token:
            self.token = saved_token

    async def _control_gateway(self) -> tuple[str, str]:
        """Resolve the same country to the current PetKit gateway."""

        if str(self.region).lower() in {"china", "cn"}:
            # py-petkit-api skips the region list for China and sends the
            # configured country name as the login region.
            return CHINA_GATEWAY, str(self.region).lower()
        header = ctw3_request_headers(
            {
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            self.tz,
        )
        try:
            response = await self._exchange(
                "regionservers", PASSPORT_REGION_SERVERS, header, None, method="GET"
            )
            match = gateway_for_region(regionserver_list(response), self.region)
            if match:
                return match[0], match[1].lower()
            LOGGER.warning(
                "CTW3 v1 regionservers had no entry named %s; using the gateway from the existing login",
                self.region,
            )
        except Exception:  # noqa: BLE001 - fall back to the gateway petkitaio already uses
            LOGGER.warning("CTW3 v1 regionservers lookup failed", exc_info=True)
        known = self.servers_dict.get(self.region) or {}
        gateway = known.get("url") or self.base_url
        region_id = known.get("id")
        if not gateway or not region_id:
            raise PetKitError(
                f"CTW3 could not resolve a PetKit gateway for region {self.region}"
            )
        if not str(gateway).endswith("/"):
            gateway = f"{gateway}/"
        return str(gateway), str(region_id)

    async def _exchange(
        self,
        step: str,
        url: str,
        header: dict[str, str],
        data: dict[str, Any] | None,
        method: str = "POST",
    ) -> dict[str, Any]:
        """Perform one CTW3 call and log a sanitized request and response."""

        LOGGER.debug(
            "CTW3 %s %s %s headers=%s body=%s",
            step,
            method,
            url.split("?")[0],
            sanitize_mapping(header),
            sanitize_mapping(data or {}),
        )
        try:
            if method == "GET":
                response = await self._get(url, header)
            else:
                response = await self._post(url, header, data or {})
        except Exception as err:
            LOGGER.warning("CTW3 %s failed: %s", step, err)
            raise
        result = response.get("result") if isinstance(response, dict) else response
        summary = device_state_summary(result) if isinstance(result, dict) else {"result": result}
        LOGGER.debug("CTW3 %s result %s", step, summary)
        return response

    async def _pull_device_state(
        self,
        fountain: W5Fountain,
        header: dict[str, str],
        expect: dict[str, Any] | None,
        attempts: int = _STATE_PULLS,
        delay: int = _STATE_PULL_WAIT,
        base_url: str | None = None,
    ) -> None:
        """Replace fountain data with a fresh deviceData read."""

        model = fountain.data.get("_model")
        root = base_url or self.base_url
        latest: dict[str, Any] | None = None
        for attempt in range(1, attempts + 1):
            await asyncio.sleep(delay)
            try:
                response = await self._exchange(
                    f"deviceData#{attempt}",
                    f"{root}{CTW3_DATA_ENDPOINT}",
                    header,
                    {"id": fountain.id},
                )
            except Exception:  # noqa: BLE001 - the command already went out
                LOGGER.debug("CTW3 deviceData refresh failed for %s", fountain.id, exc_info=True)
                continue
            result = response.get("result")
            if not isinstance(result, dict):
                LOGGER.warning("CTW3 deviceData#%s was not an object: %s", attempt, result)
                continue
            payload = normalize_ctw3_payload(result)
            if model:
                payload["_model"] = model
            fountain.data = payload
            latest = payload
            LOGGER.warning(
                "CTW3 deviceData#%s %s",
                attempt,
                device_state_summary(payload),
            )
            if expectation_met(payload, expect):
                return
        if expect and latest is not None:
            LOGGER.warning(
                "CTW3 cloud state after the command did not match %s; last=%s. "
                "controlDevice result 1 means the relay accepted the packet, "
                "not that deviceData has changed.",
                expect,
                device_state_summary(latest),
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
        try:
            type_source = "family-list type" if int(type_id) > 0 else "typeCode concatenation"
        except (TypeError, ValueError):
            type_source = "typeCode concatenation"
        payload["_ble_type_source"] = type_source
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
        LOGGER.debug(
            "CTW3 relay scan online=%s typeCode=%s typeIds=%s",
            any_online,
            online_code,
            [
                relay.get("typeId")
                for relay in relays.get("result") or []
                if isinstance(relay, dict) and relay.get("pim") == 1
            ],
        )
        return online_code

    def _group_id_for(self, device_id: int) -> Any:
        """Group id from the last roster. deviceData does not carry it."""

        for roster in (self._last_rosters or {}).values():
            result = roster.get("result") or {}
            for device in result.get("devices") or []:
                if not isinstance(device, dict):
                    continue
                try:
                    if int(device.get("id")) == int(device_id):
                        return device.get("groupId")
                except (TypeError, ValueError):
                    continue
        return None

    async def _log_relay_type_ids(
        self, header: dict[str, str], base_url: str, device_id: int
    ) -> None:
        """Log relay typeId values. py-petkit sends the fountain type, not this id."""

        group_id = self._group_id_for(device_id)
        if group_id is None:
            LOGGER.warning("CTW3 relay list skipped because the group id is unknown")
            return
        url = f"{base_url}{Endpoint.BLE_DEVICES}"
        LOGGER.debug("CTW3 ownSupportBleDevices query groupId set")
        try:
            # py-petkit-api sends groupId as a query parameter, not a form field.
            async with self._session.post(
                url,
                headers=header,
                params={"groupId": group_id},
                data="",
                timeout=self.timeout,
            ) as resp:
                response = await self._response(resp)
        except Exception:  # noqa: BLE001
            LOGGER.warning("CTW3 relay list failed", exc_info=True)
            return
        type_ids = [
            {"typeId": relay.get("typeId"), "pim": relay.get("pim")}
            for relay in response.get("result") or []
            if isinstance(relay, dict)
        ]
        LOGGER.warning("CTW3 relays %s", type_ids)

    async def _open_ble(
        self,
        header: dict[str, str],
        ble_data: dict[str, Any],
        poll_attempts: int = _BLE_POLL_ATTEMPTS,
        base_url: str | None = None,
    ) -> bool:
        """Connect and poll the relay, returning False when either step fails.

        Connect success is ``result.state == 1`` (same in petkitaio and
        py-petkit-api). Poll success is ``result == 1`` (connected). Result 0
        means the relay is still connecting, so the loop keeps waiting.
        """

        root = base_url or self.base_url
        connect_url = f"{root}{Endpoint.BLE_CONNECT}"
        poll_url = f"{root}{Endpoint.BLE_POLL}"
        self._last_poll_results = []
        self._last_connect_states = []
        connected = False
        for attempt in range(1, _BLE_CONNECT_ATTEMPTS + 1):
            response = await self._exchange(f"ble/connect#{attempt}", connect_url, header, ble_data)
            result = response.get("result")
            state = result.get("state") if isinstance(result, dict) else None
            self._last_connect_states.append(state)
            if state == 1:
                connected = True
                break
            LOGGER.debug("CTW3 ble/connect attempt %s state=%s", attempt, state)
            await asyncio.sleep(3)
        if not connected:
            return False
        for attempt in range(1, poll_attempts + 1):
            response = await self._exchange(f"ble/poll#{attempt}", poll_url, header, ble_data)
            result = response.get("result")
            self._last_poll_results.append(result)
            if ble_poll_connected(result):
                return True
            if result == BLE_POLL_ERROR:
                LOGGER.debug("CTW3 ble/poll attempt %s reported an error", attempt)
                break
            LOGGER.debug("CTW3 ble/poll attempt %s still connecting (%s)", attempt, result)
            await asyncio.sleep(_BLE_POLL_WAIT)
        try:
            await self._exchange("ble/cancel", f"{root}{Endpoint.BLE_CANCEL}", header, ble_data)
        except Exception:  # noqa: BLE001
            LOGGER.debug("CTW3 BLE cancel after failed poll failed", exc_info=True)
        return False

    async def _get(self, url: str, headers: dict[str, Any]) -> dict[str, Any]:
        """GET helper. petkitaio only implements POST."""

        async with self._session.get(url, headers=headers, timeout=self.timeout) as resp:
            return await self._response(resp)
