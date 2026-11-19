"""Client checks for CTW3 discovery and commands, without Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp

from petkitaio import PetKitClient
from petkitaio.exceptions import BluetoothError, PetKitError
from petkitaio.model import PetKitData, W5Fountain


def _install_stubs() -> None:
    root = Path(__file__).resolve().parents[1] / "custom_components"
    if "homeassistant.const" not in sys.modules:
        homeassistant = types.ModuleType("homeassistant")
        const = types.ModuleType("homeassistant.const")

        class Platform:
            BINARY_SENSOR = "binary_sensor"
            BUTTON = "button"
            FAN = "fan"
            NUMBER = "number"
            SELECT = "select"
            SENSOR = "sensor"
            SWITCH = "switch"
            TEXT = "text"

        const.Platform = Platform
        homeassistant.const = const
        sys.modules["homeassistant"] = homeassistant
        sys.modules["homeassistant.const"] = const
    if "custom_components.petkit" not in sys.modules:
        parent = types.ModuleType("custom_components")
        parent.__path__ = [str(root)]
        package = types.ModuleType("custom_components.petkit")
        package.__path__ = [str(root / "petkit")]
        package.__package__ = "custom_components.petkit"
        sys.modules["custom_components"] = parent
        sys.modules["custom_components.petkit"] = package


_install_stubs()
_CLIENT = importlib.import_module("custom_components.petkit.ctw3")
ExtendedPetKitClient = _CLIENT.ExtendedPetKitClient

asyncio.sleep = AsyncMock()


class Ctw3ClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = aiohttp.ClientSession()
        self.client = ExtendedPetKitClient(
            "user@example.com",
            "secret",
            session=self.session,
            region="US",
            timezone="UTC",
        )
        self.client.base_url = "https://petkit.example/"
        self._original_get = PetKitClient.get_petkit_data

    async def asyncTearDown(self) -> None:
        PetKitClient.get_petkit_data = self._original_get
        await self.session.close()

    async def test_roster_loads_ctw3_and_ignores_w5(self) -> None:
        self.client._last_rosters = {
            1: {
                "result": {
                    "hasRelay": True,
                    "devices": [
                        {"id": "10", "type": "CTW3", "groupId": 1, "typeId": 30},
                        {"id": 11, "type": "W5"},
                    ],
                }
            }
        }
        self.client.use_ble_relay = False
        self.client._ctw3_meta = {10: {"type": 30, "typeCode": 4}}

        async def post(url, _header, data):
            self.assertTrue(url.endswith("ctw3/deviceData"))
            self.assertEqual(data["id"], 10)
            return {
                "result": {
                    "id": 10,
                    "name": "Max 2",
                    "mac": "aa",
                    "powerStatus": 1,
                    "runStatus": 0,
                    "filterPercent": 40,
                    "typeCode": 4,
                    "settings": {"lampRingSwitch": 1},
                    "electricity": {"batteryPercent": 77},
                    "status": {"detectStatus": 1},
                }
            }

        self.client._post = post
        self.client.create_header = AsyncMock(return_value={})
        self.client._refresh_ctw3_meta = AsyncMock()
        self.client._load_catalog = AsyncMock(
            return_value=[{"deviceTypeId": 30, "typeCode": 4, "labelName": "Eversweet Max 2"}]
        )

        found = await self.client._load_ctw3_fountains()

        self.assertEqual(set(found), {10})
        fountain = found[10]
        self.assertEqual(fountain.type, "ctw3")
        self.assertEqual(fountain.data["_model"], "Eversweet Max 2")
        self.assertEqual(fountain.data["batteryPercent"], 77)
        self.assertEqual(fountain.data["detectStatus"], 1)
        self.assertEqual(fountain.ble_relay, 30)
        self.assertTrue(fountain.group_relay)

    async def test_ctw3_failure_keeps_other_devices(self) -> None:
        w5 = W5Fountain(id=5, data={"name": "Mini"}, type="w5", group_relay=False, ble_relay=14)

        async def parent(self):
            return PetKitData(
                user_id="1",
                feeders={1: object()},
                litter_boxes={},
                water_fountains={5: w5},
                pets={},
                purifiers={},
            )

        PetKitClient.get_petkit_data = parent
        self.client._load_ctw3_fountains = AsyncMock(side_effect=RuntimeError("catalog down"))

        data = await self.client.get_petkit_data()

        self.assertIs(data.water_fountains[5], w5)
        self.assertIn(1, data.feeders)

    async def test_settings_and_ble_command(self) -> None:
        fountain = W5Fountain(
            id=10,
            data={"id": 10, "mac": "aa:bb", "name": "Max 2"},
            type="ctw3",
            group_relay=True,
            ble_relay=30,
        )
        posts: list[tuple[str, dict, dict]] = []
        polls = {"count": 0}

        async def post(url, header, data):
            posts.append((url, data, header))
            if url.endswith("ble/connect"):
                return {"result": {"state": 1}}
            if url.endswith("ble/poll"):
                polls["count"] += 1
                return {"result": 0 if polls["count"] == 1 else 1}
            if url.endswith("ble/controlDevice"):
                return {"result": 1}
            if url.endswith("ctw3/deviceData"):
                return {
                    "result": {
                        "id": 10,
                        "mac": "aa:bb",
                        "name": "Max 2",
                        "powerStatus": 1,
                        "settings": {"lampRingSwitch": 1},
                    }
                }
            return {"result": 0}

        self.client._post = post
        self.client.token = "legacy-token"
        self.client._control_token = "modern-token"
        self.client._control_token_expiration = datetime.now() + timedelta(hours=5)
        self.client._control_base_url = self.client.base_url
        session_header = {"X-Session": "token", "X-Api-Version": "11.2.1"}
        self.client.create_header = AsyncMock(return_value=session_header)

        await self.client.update_ctw3_settings(fountain, {"lampRingSwitch": 1})
        await self.client.send_ctw3_command(fountain, [222, 1, 0, 0])

        self.assertEqual(posts[0][0], "https://petkit.example/ctw3/updateSettings")
        self.assertEqual(posts[0][1]["id"], 10)
        self.assertEqual(posts[0][1]["kv"], '{"lampRingSwitch": 1}')
        self.assertEqual(posts[0][2]["X-Api-Version"], "13.2.1")
        self.assertEqual(posts[0][2]["X-Session"], "modern-token")
        self.assertEqual(posts[0][2]["X-Locale"], "en-US")
        self.assertEqual(self.client.token, "legacy-token")
        self.assertEqual(session_header["X-Api-Version"], "11.2.1")
        self.assertEqual(fountain.data["settings"]["lampRingSwitch"], 1)
        control_index = next(
            index for index, item in enumerate(posts) if item[0].endswith("ble/controlDevice")
        )
        polls_before = [item for item in posts[:control_index] if item[0].endswith("ble/poll")]
        self.assertGreaterEqual(len(polls_before), 2)
        self.assertEqual(posts[control_index][1]["cmd"], "222")
        self.assertEqual(posts[control_index][1]["type"], 30)
        self.assertEqual(posts[control_index][2]["User-Agent"], "okhttp/3.14.9")
        self.assertTrue(any(url.endswith("ble/cancel") for url, _data, _header in posts))
        self.assertEqual(self.client.ble_sequence, 0)

        self.client._open_ble = AsyncMock(return_value=True)

        async def rejected(url, header, data):
            return {"result": 0}

        self.client._post = rejected
        with self.assertRaises(BluetoothError):
            await self.client.send_ctw3_command(fountain, [220, 1, 3, 0, 0, 1, 1])

        bare = W5Fountain(
            id=10,
            data={"id": 10, "mac": "aa", "name": "Max 2"},
            type="ctw3",
            group_relay=False,
            ble_relay=0,
        )
        with self.assertRaises(PetKitError):
            await self.client.send_ctw3_command(bare, [222, 1, 0, 0])

    async def test_modern_login_leaves_the_petkitaio_token(self) -> None:
        self.client.token = "legacy-token"
        self.client.region = "Germany"
        seen: list[tuple[str, str, dict]] = []

        async def get(url, header):
            seen.append(("GET", url, header))
            return {
                "result": {
                    "list": [
                        {
                            "name": "Germany",
                            "id": "DE",
                            "gateway": "https://api.eu-pet.com/latest/",
                        }
                    ]
                }
            }

        async def post(url, header, data):
            seen.append(("POST", url, data))
            return {
                "result": {
                    "session": {"id": "new-token", "userId": "u", "expiresIn": 7200}
                }
            }

        self.client._get = get
        self.client._post = post

        header, base_url = await self.client._control_header()

        self.assertEqual(self.client.token, "legacy-token")
        self.assertEqual(header["X-Session"], "new-token")
        self.assertEqual(header["F-Session"], "new-token")
        self.assertEqual(header["X-Api-Version"], "13.2.1")
        self.assertEqual(header["X-TimezoneId"], "UTC")
        self.assertEqual(base_url, "https://api.eu-pet.com/latest/")
        login = next(item for item in seen if item[0] == "POST")
        self.assertEqual(login[1], "https://api.eu-pet.com/latest/user/login")
        self.assertEqual(login[2]["oldVersion"], "13.2.1")
        self.assertEqual(login[2]["region"], "de")
        self.assertIn("13.2.1", login[2]["client"])
        self.assertNotEqual(login[2]["password"], "secret")
        regions = next(item for item in seen if item[0] == "GET")
        self.assertEqual(regions[1], "https://passport.petkt.com/v1/regionservers")


if __name__ == "__main__":
    unittest.main()
