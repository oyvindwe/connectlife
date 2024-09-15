import datetime as dt
import re
from enum import StrEnum
from typing import Dict


class DeviceType(StrEnum):
    """Known device types."""
    AIRCONDITIONER = "airconditioner"
    DEHUMIDIFIER = "dehumidifier"
    DISHWASHER = "dishwasher"
    HEAT_PUMP = "heat_pump"
    HOB = "hob"
    HOOD = "hood"
    OVEN = "oven"
    REFRIGERATOR = "refrigerator"
    TUMBLE_DRYER = "tumble_dryer"
    WASHING_MACHINE = "washing_machine"
    UNKNOWN = "unknown"


DEVICE_TYPES = {
    "003": DeviceType.WASHING_MACHINE,
    "004": DeviceType.TUMBLE_DRYER,
    "006": DeviceType.DEHUMIDIFIER,
    "009": DeviceType.AIRCONDITIONER,
    "010": DeviceType.HOOD,
    "013": DeviceType.OVEN,
    "015": DeviceType.DISHWASHER,
    "016": DeviceType.HEAT_PUMP,
    "020": DeviceType.HOOD,
    "021": DeviceType.HOOD,
    "023": DeviceType.OVEN,
    "025": DeviceType.WASHING_MACHINE,
    "026": DeviceType.REFRIGERATOR,
    "027": DeviceType.WASHING_MACHINE,
}


RE_DATETIME = re.compile(r"^(\d{4,5})/(\d{1,2})/(\d{1,2})T(\d{1,2}):(\d{1,2}):(\d{1,2})$")
MAX_DATETIME = dt.datetime(dt.MAXYEAR, 12, 31, 23, 59, 59, tzinfo=dt.UTC)


class ConnectLifeAppliance:
    """Class representing a single appliance."""

    def __init__(self, api, data):
        self._api = api
        self._wifi_id = data["wifiId"]
        self._device_id = data["deviceId"]
        self._puid = data["puid"]
        self._device_nickname = data["deviceNickName"]
        self._device_feature_code = data["deviceFeatureCode"]
        self._device_feature_name = data["deviceFeatureName"]
        self._device_type_code = data["deviceTypeCode"]
        self._device_type_name = data["deviceTypeName"]
        self._role = data["role"]
        self._room_id = data["roomId"]
        self._room_name = data["roomName"]
        self._offline_state = data["offlineState"]
        self._seq = data["seq"]
        self._bind_time = dt.datetime.fromtimestamp(data["bindTime"]/1000) if data["bindTime"] else None
        self._use_time = dt.datetime.fromtimestamp(data["useTime"]/1000) if data["useTime"] else None
        self._create_time = dt.datetime.fromtimestamp(data["createTime"]/1000) if data["createTime"] else None
        self._status_list = {k: convert(v) for k, v in data["statusList"].items()}
        self._device_type = DEVICE_TYPES[self._device_type_code] \
            if self._device_type_code in DEVICE_TYPES \
            else DeviceType.UNKNOWN

    @property
    def wifi_id(self) -> str:
        return self._wifi_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def puid(self) -> str:
        return self._puid

    @property
    def device_nickname(self) -> str:
        return self._device_nickname

    @property
    def device_feature_code(self) -> str:
        return self._device_feature_code

    @property
    def device_feature_name(self) -> str:
        return self._device_feature_name

    @property
    def device_type_code(self) -> str:
        return self._device_type_code

    @property
    def device_type_name(self) -> str:
        return self._device_type_name

    @property
    def bind_time(self) -> dt.datetime | None:
        return self._bind_time

    @property
    def role(self) -> int:
        return self._role

    @property
    def room_id(self) -> int:
        return self._room_id

    @property
    def room_name(self) -> str:
        return self._room_name

    @property
    def status_list(self) -> Dict[str, str | int | float | dt.datetime]:
        return self._status_list

    @property
    def use_time(self) -> dt.datetime | None:
        return self._use_time

    @property
    def offline_state(self) -> int:
        return self._offline_state

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def create_time(self) -> dt.datetime | None:
        return self._create_time

    @property
    def device_type(self) -> DeviceType:
        return self._device_type


def convert(value: str | float) -> float | int | str | dt.datetime:
    if isinstance(value, float):
        return value
    try:
        return int(value)
    except ValueError:
        pass
    try:
        # Unknown if timezone depends on property or appliance. Some properties include UTC in the name.
        # Extreme values observed:
        # "0002/11/30T00:00:00" (probably represents no value)
        # "16679/02/18T23:47:45" (probably represents no value)
        if match := RE_DATETIME.match(value):
            (year, month, day, hour, minute, seconds) = map(int, match.groups())
            if year > dt.MAXYEAR:
                return MAX_DATETIME
            return dt.datetime(year, month, day, hour, minute, seconds, tzinfo=dt.UTC)
    except ValueError:
        pass
    return value
