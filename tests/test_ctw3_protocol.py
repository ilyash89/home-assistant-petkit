"""Protocol checks for the Eversweet Max / Max 2 (CTW3) client."""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import unittest
import urllib.parse

_PROTOCOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "petkit"
    / "ctw3_protocol.py"
)
_SPEC = importlib.util.spec_from_file_location("ctw3_protocol", _PROTOCOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_PROTOCOL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PROTOCOL)

CTW3_TYPE = _PROTOCOL.CTW3_TYPE
MODE_BATTERY = _PROTOCOL.MODE_BATTERY
MODE_INTERMITTENT = _PROTOCOL.MODE_INTERMITTENT
MODE_STANDARD = _PROTOCOL.MODE_STANDARD
encode_ble_frame = _PROTOCOL.encode_ble_frame
flatten_device_catalog = _PROTOCOL.flatten_device_catalog
is_ctw3_type = _PROTOCOL.is_ctw3_type
mode_command = _PROTOCOL.mode_command
model_name_from_catalog = _PROTOCOL.model_name_from_catalog
normalize_ctw3_payload = _PROTOCOL.normalize_ctw3_payload
power_command = _PROTOCOL.power_command
reset_filter_command = _PROTOCOL.reset_filter_command
select_ble_type = _PROTOCOL.select_ble_type
known_number = _PROTOCOL.known_number
gateway_for_region = _PROTOCOL.gateway_for_region
modern_login_form = _PROTOCOL.modern_login_form
sanitize_mapping = _PROTOCOL.sanitize_mapping
device_state_summary = _PROTOCOL.device_state_summary
ctw3_request_headers = _PROTOCOL.ctw3_request_headers
ble_poll_connected = _PROTOCOL.ble_poll_connected
ble_control_accepted = _PROTOCOL.ble_control_accepted
expectation_met = _PROTOCOL.expectation_met


class Ctw3ProtocolTest(unittest.TestCase):
    def test_type_string_matches_cloud_name(self) -> None:
        self.assertTrue(is_ctw3_type("CTW3"))
        self.assertTrue(is_ctw3_type("ctw3"))
        self.assertFalse(is_ctw3_type("W5"))
        self.assertEqual(CTW3_TYPE, "ctw3")

    def test_mode_and_power_payloads(self) -> None:
        self.assertEqual(mode_command(MODE_STANDARD), [220, 1, 3, 0, 1, 1, 1])
        self.assertEqual(mode_command(MODE_INTERMITTENT), [220, 1, 3, 0, 1, 2, 1])
        self.assertEqual(mode_command(MODE_BATTERY), [220, 1, 3, 0, 1, 3, 1])
        self.assertEqual(power_command(False), [220, 1, 3, 0, 0, 1, 1])
        self.assertEqual(power_command(True), [220, 1, 3, 0, 1, 1, 1])
        self.assertEqual(reset_filter_command(), [222, 1, 0, 0])
        with self.assertRaises(ValueError):
            mode_command(9)

    def test_ble_frame_inserts_counter(self) -> None:
        encoded = encode_ble_frame([220, 1, 3, 0, 1, 1, 1], 4)
        frame = list(base64.b64decode(urllib.parse.unquote(encoded)))
        self.assertEqual(
            frame,
            [250, 252, 253, 220, 1, 4, 3, 0, 1, 1, 1, 251],
        )

    def test_normalize_lifts_nested_status_and_battery(self) -> None:
        payload = normalize_ctw3_payload(
            {
                "id": 7,
                "name": "Fountain",
                "status": {"powerStatus": 0, "runStatus": 1},
                "electricity": {"batteryPercent": 80},
            }
        )
        self.assertEqual(payload["powerStatus"], 0)
        self.assertEqual(payload["runStatus"], 1)
        self.assertEqual(payload["batteryPercent"], 80)
        self.assertEqual(payload["settings"], {})

    def test_catalog_requires_type_id_when_present(self) -> None:
        catalog = flatten_device_catalog(
            {
                "result": [
                    {
                        "devices": [
                            {
                                "deviceTypeId": 15,
                                "typeCode": 4,
                                "labelName": "Eversweet 3 Pro",
                            },
                            {
                                "deviceTypeId": 30,
                                "typeCode": 4,
                                "labelName": "Eversweet Max 2",
                            },
                        ]
                    }
                ]
            }
        )
        self.assertEqual(model_name_from_catalog(catalog, 30, 4), "Eversweet Max 2")
        self.assertIsNone(model_name_from_catalog(catalog, 99, 4))
        self.assertEqual(model_name_from_catalog(catalog, None, 4), "Eversweet 3 Pro")

    def test_ble_type_prefers_fountain_type_id(self) -> None:
        self.assertEqual(select_ble_type(30, 4, 1), 30)
        self.assertEqual(select_ble_type(0, 4, 1), 14)
        self.assertIsNone(select_ble_type(None, None, None))

    def test_placeholders_and_current_client_headers(self) -> None:
        self.assertIsNone(known_number(-1))
        self.assertIsNone(known_number(True))
        self.assertEqual(known_number(0), 0)
        self.assertFalse(ble_poll_connected(0))
        self.assertTrue(ble_poll_connected(1))
        self.assertFalse(ble_control_accepted(0))
        self.assertTrue(ble_control_accepted(1))
        session = {"X-Session": "token", "X-Api-Version": "11.2.1", "User-Agent": "old"}
        headers = ctw3_request_headers(session, "UTC")
        self.assertEqual(session["X-Api-Version"], "11.2.1")
        self.assertEqual(headers["X-Session"], "token")
        self.assertEqual(headers["X-Api-Version"], "13.2.1")
        self.assertEqual(headers["User-Agent"], "okhttp/3.14.9")
        self.assertEqual(headers["X-Client"], "android(16.1;23127PN0CG)")
        self.assertTrue(
            expectation_met(
                {"settings": {"lampRingSwitch": 0, "lampRingBrightness": 2}},
                {"settings": {"lampRingSwitch": 0}},
            )
        )

    def test_modern_login_form_and_redaction(self) -> None:
        gateway, region_id = gateway_for_region(
            [{"name": "Germany", "id": "DE", "gateway": "https://api.eu-pet.com/latest"}],
            "Germany",
        )
        self.assertEqual(gateway, "https://api.eu-pet.com/latest/")
        self.assertEqual(region_id, "de")
        form = modern_login_form("user@example.com", "abc", region_id, "UTC")
        self.assertEqual(form["oldVersion"], "13.2.1")
        self.assertEqual(form["region"], "de")
        self.assertIn("'version': '13.2.1'", form["client"])
        self.assertIn("'source': 'app.petkit-android'", form["client"])
        cleaned = sanitize_mapping(
            {"X-Session": "secret", "password": "abc", "id": 10, "cmd": "220", "mac": "aa"}
        )
        self.assertEqual(cleaned["X-Session"], "present")
        self.assertEqual(cleaned["password"], "present")
        self.assertEqual(cleaned["id"], "present")
        self.assertEqual(cleaned["mac"], "present")
        self.assertEqual(cleaned["cmd"], "220")
        self.assertEqual(
            device_state_summary(
                {"mode": 1, "powerStatus": 1, "runStatus": 0, "updateAt": 5, "settings": {}}
            )["updateAt"],
            5,
        )


if __name__ == "__main__":
    unittest.main()
