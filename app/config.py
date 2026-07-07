import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CUSTOM_IN_OID = ".1.3.6.1.2.1.31.1.1.1.6.45"
DEFAULT_CUSTOM_OUT_OID = ".1.3.6.1.2.1.31.1.1.1.10.45"

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 8088,
    "sample_interval_seconds": 30,
    "retention_days": 400,
    "snmp_timeout_seconds": 2.0,
    "snmp_retries": 1,
    "mock_mode": True,
    "devices": [
        {
            "id": "demo-core-01",
            "name": "S5700-Core-01",
            "host": "127.0.0.1",
            "snmp_version": "2c",
            "community": "public",
            "oid_profile": "if_mib_64",
            "custom_oid": "",
            "custom_direction": "in",
            "custom_in_oid": DEFAULT_CUSTOM_IN_OID,
            "custom_out_oid": DEFAULT_CUSTOM_OUT_OID,
            "monitor_interfaces": [],
            "enabled": True,
            "mock": True,
        }
    ],
}


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    name: str
    host: str
    snmp_version: str = "2c"
    community: str = "public"
    oid_profile: str = "if_mib_64"
    custom_oid: str = ""
    custom_direction: str = "in"
    custom_in_oid: str = DEFAULT_CUSTOM_IN_OID
    custom_out_oid: str = DEFAULT_CUSTOM_OUT_OID
    monitor_interfaces: tuple[str, ...] = ()
    port: int = 161
    enabled: bool = True
    mock: bool = False


@dataclass(frozen=True)
class MonitorConfig:
    listen_host: str
    listen_port: int
    sample_interval_seconds: int
    retention_days: int
    snmp_timeout_seconds: float
    snmp_retries: int
    mock_mode: bool
    devices: list[DeviceConfig]


def ensure_default_config(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def read_config_json(data_dir: Path) -> dict[str, Any]:
    config_path = data_dir / "config.json"
    ensure_default_config(config_path)
    return json.loads(config_path.read_text(encoding="utf-8-sig"))


def save_config_json(data_dir: Path, value: dict[str, Any]) -> None:
    validate_config_json(value)
    config_path = data_dir / "config.json"
    config_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def validate_config_json(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("配置必须是 JSON 对象")
    if "devices" not in value or not isinstance(value["devices"], list):
        raise ValueError("配置必须包含 devices 数组")
    for index, device in enumerate(value["devices"], start=1):
        if not isinstance(device, dict):
            raise ValueError(f"第 {index} 个设备配置必须是对象")
        if not device.get("id"):
            raise ValueError(f"第 {index} 个设备缺少 id")
        if not device.get("host"):
            raise ValueError(f"第 {index} 个设备缺少 host")
        version = str(device.get("snmp_version", "2c")).lower()
        if version not in ("2c", "v2c"):
            raise ValueError("当前版本仅支持 snmp_version=2c")
        profile = str(device.get("oid_profile", "if_mib_64"))
        if profile not in ("if_mib_64", "if_mib_32", "custom_single", "custom_dual"):
            raise ValueError("oid_profile must be if_mib_64, if_mib_32, custom_single, or custom_dual")
        custom_direction = str(device.get("custom_direction", "in")).lower()
        if custom_direction not in ("in", "out"):
            raise ValueError("custom_direction must be in or out")
        custom_oid = str(device.get("custom_oid", "")).strip()
        if profile == "custom_single" and not is_oid_text(custom_oid):
            raise ValueError("custom_single requires a valid SNMP OID")
        custom_in_oid = str(device.get("custom_in_oid") or DEFAULT_CUSTOM_IN_OID).strip()
        custom_out_oid = str(device.get("custom_out_oid") or DEFAULT_CUSTOM_OUT_OID).strip()
        if profile == "custom_dual":
            device["custom_in_oid"] = custom_in_oid
            device["custom_out_oid"] = custom_out_oid
            if not is_oid_text(custom_in_oid) or not is_oid_text(custom_out_oid):
                raise ValueError("custom_dual requires valid in and out SNMP OIDs")
        monitor_interfaces = device.get("monitor_interfaces", [])
        if monitor_interfaces is None:
            device["monitor_interfaces"] = []
        elif not isinstance(monitor_interfaces, list):
            raise ValueError("monitor_interfaces 必须是数组")


def load_config(data_dir: Path) -> MonitorConfig:
    config_path = data_dir / "config.json"
    ensure_default_config(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    devices = [_load_device(item) for item in raw.get("devices", [])]
    if not devices:
        devices = [_load_device(DEFAULT_CONFIG["devices"][0])]
    return MonitorConfig(
        listen_host=str(raw.get("listen_host", "127.0.0.1")),
        listen_port=int(raw.get("listen_port", 8088)),
        sample_interval_seconds=max(30, int(raw.get("sample_interval_seconds", 30))),
        retention_days=max(366, int(raw.get("retention_days", 400))),
        snmp_timeout_seconds=float(raw.get("snmp_timeout_seconds", 2.0)),
        snmp_retries=max(0, int(raw.get("snmp_retries", 1))),
        mock_mode=bool(raw.get("mock_mode", False)),
        devices=devices,
    )


def _load_device(item: dict[str, Any]) -> DeviceConfig:
    host = str(item.get("host") or item.get("mgmt_ip") or "127.0.0.1")
    device_id = str(item.get("id") or host.replace(".", "-"))
    oid_profile = str(item.get("oid_profile", "if_mib_64"))
    return DeviceConfig(
        id=device_id,
        name=str(item.get("name") or device_id),
        host=host,
        snmp_version=str(item.get("snmp_version", "2c")).lower(),
        community=str(item.get("community", "public")),
        oid_profile=oid_profile,
        custom_oid=str(item.get("custom_oid", "")).strip(),
        custom_direction=str(item.get("custom_direction", "in")).lower(),
        custom_in_oid=str(item.get("custom_in_oid") or (DEFAULT_CUSTOM_IN_OID if oid_profile == "custom_dual" else "")).strip(),
        custom_out_oid=str(item.get("custom_out_oid") or (DEFAULT_CUSTOM_OUT_OID if oid_profile == "custom_dual" else "")).strip(),
        monitor_interfaces=tuple(str(value).strip() for value in item.get("monitor_interfaces", []) if str(value).strip()),
        port=int(item.get("port", 161)),
        enabled=bool(item.get("enabled", True)),
        mock=bool(item.get("mock", False)),
    )


def is_oid_text(value: str) -> bool:
    parts = value.strip(".").split(".")
    return len(parts) >= 2 and all(part.isdigit() for part in parts)
