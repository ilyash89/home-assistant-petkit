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
from petkitaio.exceptions import PetKitError
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
        posts: list[tuple[str, dict]] = []

        async def post(url, _header, data):
            posts.append((url, data))
            if url.endswith("ble/connect"):
                return {"result": {"state": 1}}
            return {"result": 0}

        self.client._post = post
        self.client.create_header = AsyncMock(return_value={"X": "1"})

        await self.client.update_ctw3_settings(fountain, {"lampRingSwitch": 1})
        await self.client.send_ctw3_command(fountain, [222, 1, 0, 0])

        self.assertEqual(posts[0][0], "https://petkit.example/ctw3/updateSettings")
        self.assertEqual(posts[0][1]["id"], 10)
        self.assertEqual(posts[0][1]["kv"], '{"lampRingSwitch": 1}')
        control = next(item for item in posts if item[0].endswith("ble/controlDevice"))
        self.assertEqual(control[1]["cmd"], "222")
        self.assertEqual(control[1]["type"], 30)
        self.assertTrue(any(url.endswith("ble/cancel") for url, _data in posts))

        bare = W5Fountain(
            id=10,
            data={"id": 10, "mac": "aa", "name": "Max 2"},
            type="ctw3",
            group_relay=False,
            ble_relay=0,
        )
        with self.assertRaises(PetKitError):
            await self.client.send_ctw3_command(bare, [222, 1, 0, 0])


if __name__ == "__main__":
    unittest.main()
