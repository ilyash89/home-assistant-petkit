"""Home Assistant entities for the Eversweet Max / Max 2 (CTW3) fountain.

Read-only values come from ``ctw3/deviceData``. Light, brightness, and
do-not-disturb are cloud settings (``ctw3/updateSettings``). Power, working
mode, and filter reset go through the BLE relay with the CTW3 command
layout. Other fountain models keep using the W5 entities.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from petkitaio.exceptions import BluetoothError, PetKitError

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTime
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LIGHT_BRIGHTNESS_NAMED, LIGHT_BRIGHTNESS_OPTIONS
from .ctw3_protocol import (
    CTW3_MODE_OPTIONS,
    CTW3_MODEL_FALLBACK,
    CTW3_MODES,
    SETTING_BRIGHTNESS,
    SETTING_DND,
    SETTING_LIGHT,
    known_number,
    mode_command,
    power_command,
    reset_filter_command,
)
from .exceptions import PetKitBluetoothError

_BRIGHTNESS_TO_LEVEL = {name: level for level, name in LIGHT_BRIGHTNESS_NAMED.items()}


def _settings(data: dict[str, Any]) -> dict[str, Any]:
    settings = data.get("settings")
    return settings if isinstance(settings, dict) else {}


def _energy_kwh(data: dict[str, Any]) -> float | None:
    """Prefer the cloud kWh field; otherwise use the same W5 pump formula."""

    if "todayUseElectricity" in data:
        direct = known_number(data.get("todayUseElectricity"))
        if direct is None:
            return None
        return round(float(direct), 4)
    runtime = known_number(data.get("todayPumpRunTime"))
    if runtime is None:
        return None
    return round((0.75 * float(runtime)) / 3_600_000, 4)


def _purified_count(data: dict[str, Any]) -> int | None:
    """Same derived count the other Eversweet entities expose."""

    runtime = known_number(data.get("todayPumpRunTime"))
    if runtime is None:
        return None
    return int(((1.5 * float(runtime)) / 60) / 2.0)


def _timestamp(data: dict[str, Any]) -> datetime | None:
    raw = data.get("updateAt")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace(".000Z", "+00:00"))
    except ValueError:
        return None


def _pump_status(data: dict[str, Any]) -> str | None:
    run = data.get("runStatus")
    if run == 1:
        return "running"
    if run == 0:
        return "stopped"
    return None


def _number(data: dict[str, Any], key: str, nested: str | None = None) -> int | float | None:
    source = _settings(data) if nested == "settings" else data
    return known_number(source.get(key))


class Max2Entity(CoordinatorEntity):
    """Shared device info for one CTW3 fountain."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, wf_id: int) -> None:
        super().__init__(coordinator)
        self.wf_id = wf_id

    @property
    def wf_data(self):
        return self.coordinator.data.water_fountains[self.wf_id]

    @property
    def device_info(self) -> dict[str, Any]:
        data = self.wf_data.data
        hardware = data.get("hardware")
        firmware = data.get("firmware")
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, self.wf_data.id)},
            "name": data.get("name"),
            "manufacturer": "PetKit",
            "model": data.get("_model") or CTW3_MODEL_FALLBACK,
        }
        if hardware is not None and firmware is not None:
            info["sw_version"] = f"{hardware}.{firmware}"
        return info

    @property
    def available(self) -> bool:
        try:
            self.wf_data
        except KeyError:
            return False
        return super().available


@dataclass(frozen=True)
class _SensorSpec:
    suffix: str
    key: str
    value: Callable[[dict[str, Any]], Any]
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    category: EntityCategory | None = None
    options: list[str] | None = None
    icon: str | Callable[[dict[str, Any]], str] | None = None
    present: Callable[[dict[str, Any]], bool] | None = None


def _has(key: str, nested: str | None = None) -> Callable[[dict[str, Any]], bool]:
    def present(data: dict[str, Any]) -> bool:
        source = _settings(data) if nested == "settings" else data
        return source.get(key) is not None
    return present


_SENSOR_SPECS: tuple[_SensorSpec, ...] = (
    _SensorSpec(
        "_energy_usage",
        "energy_usage",
        _energy_kwh,
        SensorDeviceClass.ENERGY,
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorStateClass.TOTAL,
        EntityCategory.DIAGNOSTIC,
        present=lambda data: _energy_kwh(data) is not None,
    ),
    _SensorSpec(
        "_last_update",
        "last_data_update",
        _timestamp,
        SensorDeviceClass.TIMESTAMP,
        category=EntityCategory.DIAGNOSTIC,
        present=lambda data: _timestamp(data) is not None,
    ),
    _SensorSpec(
        "_filter_percent",
        "filter",
        lambda data: _number(data, "filterPercent"),
        unit=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon=lambda data: "mdi:filter-off" if data.get("filterPercent") == 0 else "mdi:filter",
        present=_has("filterPercent"),
    ),
    _SensorSpec(
        "_purified_water",
        "purified_water_today",
        _purified_count,
        state_class=SensorStateClass.MEASUREMENT,
        category=EntityCategory.DIAGNOSTIC,
        icon="mdi:water-pump",
        present=lambda data: _purified_count(data) is not None,
    ),
    _SensorSpec(
        "_battery",
        "fountain_battery",
        lambda data: _number(data, "batteryPercent"),
        SensorDeviceClass.BATTERY,
        PERCENTAGE,
        SensorStateClass.MEASUREMENT,
        present=_has("batteryPercent"),
    ),
    _SensorSpec(
        "_pump_status",
        "pump_status",
        _pump_status,
        SensorDeviceClass.ENUM,
        options=["running", "stopped"],
        icon="mdi:pump",
        present=lambda data: data.get("runStatus") is not None,
    ),
    _SensorSpec(
        "_pump_runtime_today",
        "pump_runtime_today",
        lambda data: _number(data, "todayPumpRunTime"),
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        SensorStateClass.TOTAL,
        EntityCategory.DIAGNOSTIC,
        present=_has("todayPumpRunTime"),
    ),
    _SensorSpec(
        "_pump_runtime_total",
        "pump_runtime_total",
        lambda data: _number(data, "waterPumpRunTime"),
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        SensorStateClass.TOTAL_INCREASING,
        EntityCategory.DIAGNOSTIC,
        present=_has("waterPumpRunTime"),
    ),
    _SensorSpec(
        "_filter_days",
        "filter_days_remaining",
        lambda data: _number(data, "filterExpectedDays"),
        SensorDeviceClass.DURATION,
        UnitOfTime.DAYS,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
        present=_has("filterExpectedDays"),
    ),
    _SensorSpec(
        "_smart_working_time",
        "smart_working_time",
        lambda data: _number(data, "smartWorkingTime", "settings"),
        SensorDeviceClass.DURATION,
        UnitOfTime.MINUTES,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
        present=_has("smartWorkingTime", "settings"),
    ),
    _SensorSpec(
        "_smart_sleep_time",
        "smart_sleep_time",
        lambda data: _number(data, "smartSleepTime", "settings"),
        SensorDeviceClass.DURATION,
        UnitOfTime.MINUTES,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
        present=_has("smartSleepTime", "settings"),
    ),
    _SensorSpec(
        "_battery_working_time",
        "battery_working_time",
        lambda data: _number(data, "batteryWorkingTime", "settings"),
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
        present=_has("batteryWorkingTime", "settings"),
    ),
    _SensorSpec(
        "_battery_sleep_time",
        "battery_sleep_time",
        lambda data: _number(data, "batterySleepTime", "settings"),
        SensorDeviceClass.DURATION,
        UnitOfTime.SECONDS,
        SensorStateClass.MEASUREMENT,
        EntityCategory.DIAGNOSTIC,
        present=_has("batterySleepTime", "settings"),
    ),
)


class Max2Sensor(Max2Entity, SensorEntity):
    """One CTW3 sensor described by a spec."""

    def __init__(self, coordinator, wf_id: int, spec: _SensorSpec) -> None:
        super().__init__(coordinator, wf_id)
        self._spec = spec
        self._attr_translation_key = spec.key
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_state_class = spec.state_class
        self._attr_entity_category = spec.category
        self._attr_options = spec.options

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}{self._spec.suffix}"

    @property
    def native_value(self) -> Any:
        return self._spec.value(self.wf_data.data)

    @property
    def icon(self) -> str | None:
        icon = self._spec.icon
        if callable(icon):
            return icon(self.wf_data.data)
        return icon

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self._spec.present is None:
            return True
        return self._spec.present(self.wf_data.data)


def max2_sensors(coordinator, wf_id: int) -> list[Max2Sensor]:
    return [Max2Sensor(coordinator, wf_id, spec) for spec in _SENSOR_SPECS]


@dataclass(frozen=True)
class _BinarySpec:
    suffix: str
    key: str
    field: str
    device_class: BinarySensorDeviceClass | None = None
    on_icon: str = "mdi:alert"
    off_icon: str = "mdi:check"
    category: EntityCategory | None = None
    nested: str | None = None


_BINARY_SPECS: tuple[_BinarySpec, ...] = (
    _BinarySpec(
        "_water_level",
        "water_level",
        "lackWarning",
        BinarySensorDeviceClass.PROBLEM,
        "mdi:water-alert",
        "mdi:water",
    ),
    _BinarySpec(
        "_low_battery",
        "low_battery",
        "lowBattery",
        BinarySensorDeviceClass.BATTERY,
        "mdi:battery-alert",
        "mdi:battery",
    ),
    _BinarySpec(
        "_filter_warning",
        "filter_warning",
        "filterWarning",
        BinarySensorDeviceClass.PROBLEM,
        "mdi:filter-off",
        "mdi:filter",
    ),
    _BinarySpec(
        "_breakdown",
        "breakdown",
        "breakdownWarning",
        BinarySensorDeviceClass.PROBLEM,
        "mdi:alert-circle",
        "mdi:check-circle",
    ),
    _BinarySpec(
        "_pet_detected",
        "pet_detected",
        "detectStatus",
        BinarySensorDeviceClass.OCCUPANCY,
        "mdi:cat",
        "mdi:cat",
        EntityCategory.DIAGNOSTIC,
    ),
    _BinarySpec(
        "_night_dnd",
        "night_dnd",
        "isNightNoDisturbing",
        None,
        "mdi:weather-night",
        "mdi:weather-sunny",
        EntityCategory.DIAGNOSTIC,
    ),
)


class Max2BinarySensor(Max2Entity, BinarySensorEntity):
    """Problem and status flags reported by CTW3 deviceData."""

    def __init__(self, coordinator, wf_id: int, spec: _BinarySpec) -> None:
        super().__init__(coordinator, wf_id)
        self._spec = spec
        self._attr_translation_key = spec.key
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.category

    def _raw(self) -> Any:
        source = _settings(self.wf_data.data) if self._spec.nested else self.wf_data.data
        return source.get(self._spec.field)

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}{self._spec.suffix}"

    @property
    def is_on(self) -> bool:
        return self._raw() == 1

    @property
    def icon(self) -> str:
        return self._spec.on_icon if self.is_on else self._spec.off_icon

    @property
    def available(self) -> bool:
        return super().available and self._raw() is not None


def max2_binary_sensors(coordinator, wf_id: int) -> list[Max2BinarySensor]:
    return [Max2BinarySensor(coordinator, wf_id, spec) for spec in _BINARY_SPECS]


class _Max2Control(Max2Entity):
    """Shared command helpers."""

    def _name(self) -> str:
        return self.wf_data.data.get("name") or str(self.wf_data.id)

    def _require_relay(self) -> None:
        name = self._name()
        if not self.coordinator.client.use_ble_relay:
            raise HomeAssistantError(f"A PetKit BLE relay is required to control {name}")
        if not self.wf_data.group_relay:
            raise HomeAssistantError(
                f"A PetKit BLE relay is required to control {name}. "
                "PetKit did not return a valid relay device. If you do have a relay device, "
                "it may temporarily be offline."
            )

    async def _send_ble(self, command: list[int], expect: dict[str, Any] | None = None) -> None:
        self._require_relay()
        try:
            await self.coordinator.client.send_ctw3_command(self.wf_data, command, expect)
        except BluetoothError as err:
            raise PetKitBluetoothError(
                f"Bluetooth connection to {self._name()} failed. Please try again."
            ) from err

    async def _write_setting(self, key: str, value: int) -> None:
        try:
            await self.coordinator.client.update_ctw3_settings(self.wf_data, {key: value})
        except PetKitError as err:
            raise HomeAssistantError(str(err)) from err

    async def _settle(self) -> None:
        self.async_write_ha_state()
        await asyncio.sleep(1)
        await self.coordinator.async_request_refresh()


class Max2Light(_Max2Control, SwitchEntity):
    """Lamp ring. Cloud setting, no BLE relay required."""

    _attr_translation_key = "light"

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_light"

    @property
    def icon(self) -> str:
        return "mdi:lightbulb" if self.is_on else "mdi:lightbulb-off"

    @property
    def is_on(self) -> bool:
        return _settings(self.wf_data.data).get(SETTING_LIGHT) == 1

    @property
    def available(self) -> bool:
        return super().available and SETTING_LIGHT in _settings(self.wf_data.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write_setting(SETTING_LIGHT, 1)
        await self._settle()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write_setting(SETTING_LIGHT, 0)
        await self._settle()


class Max2DoNotDisturb(_Max2Control, SwitchEntity):
    """Do-not-disturb switch. The schedule itself stays in the PetKit app."""

    _attr_translation_key = "do_not_disturb"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_disturb"

    @property
    def icon(self) -> str:
        return "mdi:sleep" if self.is_on else "mdi:sleep-off"

    @property
    def is_on(self) -> bool:
        return _settings(self.wf_data.data).get(SETTING_DND) == 1

    @property
    def available(self) -> bool:
        return super().available and SETTING_DND in _settings(self.wf_data.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._write_setting(SETTING_DND, 1)
        await self._settle()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._write_setting(SETTING_DND, 0)
        await self._settle()


class Max2Power(_Max2Control, SwitchEntity):
    """Fountain power via the CTW3 BLE power-on / power-off frame."""

    _attr_translation_key = "power"

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_power"

    @property
    def icon(self) -> str:
        return "mdi:power-plug" if self.is_on else "mdi:power-plug-off"

    @property
    def is_on(self) -> bool:
        return self.wf_data.data.get("powerStatus") == 1

    @property
    def available(self) -> bool:
        return super().available and self.wf_data.data.get("powerStatus") is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send_ble(power_command(True), {"powerStatus": 1})
        await self._settle()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_ble(power_command(False), {"powerStatus": 0})
        await self._settle()


class Max2Brightness(_Max2Control, SelectEntity):
    """Lamp brightness. Only available while the lamp is on."""

    _attr_translation_key = "light_brightness"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = LIGHT_BRIGHTNESS_OPTIONS

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_light_brightness"

    @property
    def icon(self) -> str:
        level = _settings(self.wf_data.data).get(SETTING_BRIGHTNESS)
        if level == 1:
            return "mdi:lightbulb-on-30"
        if level == 2:
            return "mdi:lightbulb-on-50"
        return "mdi:lightbulb-on"

    @property
    def available(self) -> bool:
        return super().available and _settings(self.wf_data.data).get(SETTING_LIGHT) == 1

    @property
    def current_option(self) -> str | None:
        return LIGHT_BRIGHTNESS_NAMED.get(_settings(self.wf_data.data).get(SETTING_BRIGHTNESS))

    @property
    def options(self) -> list[str]:
        return LIGHT_BRIGHTNESS_OPTIONS

    async def async_select_option(self, option: str) -> None:
        level = _BRIGHTNESS_TO_LEVEL.get(option)
        if level is None:
            return
        await self._write_setting(SETTING_BRIGHTNESS, level)
        await self._settle()


class Max2Mode(_Max2Control, SelectEntity):
    """Standard, intermittent, or battery mode."""

    _attr_translation_key = "max2_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = CTW3_MODE_OPTIONS

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_mode"

    @property
    def icon(self) -> str:
        mode = self.wf_data.data.get("mode")
        if mode == 2:
            return "mdi:timer-sand"
        if mode == 3:
            return "mdi:battery"
        return "mdi:waves"

    @property
    def current_option(self) -> str | None:
        return CTW3_MODES.get(self.wf_data.data.get("mode"))

    @property
    def options(self) -> list[str]:
        return CTW3_MODE_OPTIONS

    async def async_select_option(self, option: str) -> None:
        mode = next((value for value, name in CTW3_MODES.items() if name == option), None)
        if mode is None:
            return
        await self._send_ble(mode_command(mode), {"mode": mode, "powerStatus": 1})
        await self._settle()


class Max2ResetFilter(_Max2Control, ButtonEntity):
    """Reset the filter-life counter through the BLE relay."""

    _attr_translation_key = "reset_filter"
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def unique_id(self) -> str:
        return f"{self.wf_data.id}_reset_filter"

    @property
    def available(self) -> bool:
        return super().available and bool(self.wf_data.group_relay) and bool(
            self.coordinator.client.use_ble_relay
        )

    async def async_press(self) -> None:
        await self._send_ble(reset_filter_command(), {"filterPercent": 100})
        await self._settle()


def max2_switches(coordinator, wf_id: int) -> list[SwitchEntity]:
    return [
        Max2Light(coordinator, wf_id),
        Max2Power(coordinator, wf_id),
        Max2DoNotDisturb(coordinator, wf_id),
    ]


def max2_selects(coordinator, wf_id: int) -> list[SelectEntity]:
    return [
        Max2Brightness(coordinator, wf_id),
        Max2Mode(coordinator, wf_id),
    ]


def max2_buttons(coordinator, wf_id: int) -> list[ButtonEntity]:
    return [Max2ResetFilter(coordinator, wf_id)]
