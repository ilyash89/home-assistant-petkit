"""Client checks for CTW3 discovery and commands, without Home Assistant."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import aiohttp

from petkitaio import PetKitClient
from petkitaio.constants import FeederSetting, LitterBoxSetting, PurifierSetting, W5Command
from petkitaio.exceptions import BluetoothError, PetKitError
from petkitaio.model import Feeder, LitterBox, PetKitData, Purifier, W5Fountain


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
        self.client.token = "shared-token"
        self.client.tz = "UTC"

        await self.client.update_ctw3_settings(fountain, {"lampRingSwitch": 1})
        await self.client.send_ctw3_command(fountain, [222, 1, 0, 0])

        self.assertEqual(posts[0][0], "https://petkit.example/ctw3/updateSettings")
        self.assertEqual(posts[0][1]["id"], 10)
        self.assertEqual(posts[0][1]["kv"], '{"lampRingSwitch": 1}')
        self.assertEqual(posts[0][2]["X-Api-Version"], "13.2.1")
        self.assertEqual(posts[0][2]["X-Session"], "shared-token")
        self.assertEqual(posts[0][2]["F-Session"], "shared-token")
        self.assertEqual(posts[0][2]["X-Locale"], "en-US")
        self.assertEqual(posts[0][2]["User-Agent"], "okhttp/3.14.9")
        self.assertEqual(self.client.token, "shared-token")
        self.assertFalse(any("user/login" in url for url, _data, _header in posts))
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

    async def test_one_modern_session_covers_existing_devices(self) -> None:
        from datetime import datetime, timedelta

        posts: list[tuple[str, dict, dict]] = []

        async def get(url, header):
            self.assertEqual(url, "https://passport.petkt.com/v1/regionservers")
            self.assertEqual(header["X-Api-Version"], "13.2.1")
            self.assertNotIn("X-Session", header)
            return {
                "result": {
                    "list": [
                        {
                            "name": "Germany",
                            "id": "DE",
                            "gateway": "https://api.eu-pet.com/latest",
                        }
                    ]
                }
            }

        async def post(url, header, data):
            posts.append((url, data, header))
            if url.endswith("user/login"):
                return {"result": {"session": {"id": "shared-token", "userId": "u", "expiresIn": 7200}}}
            if url.endswith("group/family/list"):
                return {"result": [{"groupId": 7}]}
            if url.endswith("device_detail") or url.endswith("w5/deviceData"):
                return {"result": {"id": data.get("id", 1), "name": "device", "mac": "aa"}}
            return {"result": []}

        self.client.region = "Germany"
        self.client.password = "secret"
        self.client.tz = "UTC"
        self.client._get = get
        self.client._post = post

        await self.client.login()
        await self.client.check_token()

        logins = [item for item in posts if item[0].endswith("user/login")]
        self.assertEqual(len(logins), 1)
        self.assertEqual(logins[0][1]["oldVersion"], "13.2.1")
        self.assertEqual(logins[0][1]["region"], "de")
        self.assertNotIn("11.2.1", logins[0][1]["client"])
        self.assertNotEqual(logins[0][1]["password"], "secret")
        self.assertEqual(logins[0][2]["X-Api-Version"], "13.2.1")
        self.assertEqual(self.client.token, "shared-token")
        self.assertEqual(self.client.base_url, "https://api.eu-pet.com/latest/")
        self.assertGreater(
            (self.client.token_expiration - datetime.now()).total_seconds(), 3600
        )

        posts.clear()
        await self.client.update_feeder_settings(
            Feeder(id=3, data={}, type="feeder"), FeederSetting.CHILD_LOCK, 1
        )
        await self.client.update_feeder_settings(
            Feeder(id=4, data={}, type="feedermini"), FeederSetting.MINI_CHILD_LOCK, 0
        )
        await self.client.update_feeder_settings(
            Feeder(id=5, data={}, type="d4"), FeederSetting.INDICATOR_LIGHT, 1
        )
        await self.client.update_litter_box_settings(
            LitterBox(
                id=8,
                device_detail={},
                device_record={},
                statistics={},
                type="t4",
                manually_paused=False,
            ),
            LitterBoxSetting.AUTO_CLEAN,
            1,
        )
        await self.client.update_purifier_settings(
            Purifier(id=9, device_detail={}, type="k2"), PurifierSetting.LIGHT, 1
        )
        self.client.use_ble_relay = False
        header = await self.client.create_header()
        feeder, feeder_id = await self.client._handle_feeder({"id": 5, "type": "D4"}, header)
        purifier, purifier_id = await self.client._handle_purifier({"id": 9, "type": "K2"}, header)
        box, box_id = await self.client._handle_litter_box({"id": 8, "type": "T4"}, header)
        fountain, fountain_id = await self.client._handle_water_fountain(
            {"id": 11, "type": "W5"}, False, header
        )
        self.client.start_ble_connection = AsyncMock(return_value=True)
        self.client.poll_ble_connection = AsyncMock(return_value=True)
        await self.client.control_water_fountain(
            W5Fountain(
                id=11,
                data={"id": 11, "name": "Solo", "mac": "aa", "powerStatus": 1, "mode": 1},
                type="w5",
                group_relay=True,
                ble_relay=14,
            ),
            W5Command.NORMAL,
        )

        paths = [url.removeprefix(self.client.base_url) for url, _data, _header in posts]
        self.assertIn("feeder/updateSettings", paths)
        self.assertIn("feedermini/update", paths)
        self.assertIn("d4/updateSettings", paths)
        self.assertIn("t4/updateSettings", paths)
        self.assertIn("k2/updateSettings", paths)
        self.assertIn("d4/device_detail", paths)
        self.assertIn("k2/device_detail", paths)
        self.assertIn("t4/device_detail", paths)
        self.assertIn("t4/getDeviceRecord", paths)
        self.assertIn("t4/statistic", paths)
        self.assertIn("w5/deviceData", paths)
        self.assertIn("ble/controlDevice", paths)
        self.assertNotIn("feeder/update", paths)
        self.assertTrue(all(item[2]["X-Session"] == "shared-token" for item in posts))
        self.assertTrue(all(item[2]["X-Api-Version"] == "13.2.1" for item in posts))
        self.assertFalse(any(url.endswith("user/login") for url, _data, _header in posts))
        self.assertEqual(feeder.type, "d4")
        self.assertEqual(feeder_id, 5)
        self.assertEqual(purifier.type, "k2")
        self.assertEqual(purifier_id, 9)
        self.assertEqual(box.type, "t4")
        self.assertEqual(box_id, 8)
        self.assertEqual(fountain.type, "w5")
        self.assertEqual(fountain_id, 11)
        self.assertGreater(self.client.token_expiration, datetime.now() + timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
