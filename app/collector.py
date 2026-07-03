import math
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .config import DeviceConfig, MonitorConfig
from .database import InterfaceSample, MonitorDatabase
from .snmp_v2c import SnmpError, SnmpV2cClient


UTC = timezone.utc

IF_DESCR = ".1.3.6.1.2.1.2.2.1.2"
IF_OPER_STATUS = ".1.3.6.1.2.1.2.2.1.8"
IF_IN_OCTETS = ".1.3.6.1.2.1.2.2.1.10"
IF_OUT_OCTETS = ".1.3.6.1.2.1.2.2.1.16"
IF_SPEED = ".1.3.6.1.2.1.2.2.1.5"
IF_NAME = ".1.3.6.1.2.1.31.1.1.1.1"
IF_HC_IN_OCTETS = ".1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS = ".1.3.6.1.2.1.31.1.1.1.10"
IF_HIGH_SPEED = ".1.3.6.1.2.1.31.1.1.1.15"
IF_ALIAS = ".1.3.6.1.2.1.31.1.1.1.18"

OID_PROFILES = {
    "if_mib_64": {
        "label": "IF-MIB 64位接口流量",
        "in_octets": IF_HC_IN_OCTETS,
        "out_octets": IF_HC_OUT_OCTETS,
        "speed": IF_HIGH_SPEED,
        "speed_unit": "mbps",
    },
    "if_mib_32": {
        "label": "IF-MIB 32位兼容接口流量",
        "in_octets": IF_IN_OCTETS,
        "out_octets": IF_OUT_OCTETS,
        "speed": IF_SPEED,
        "speed_unit": "bps",
    },
    "custom_single": {
        "label": "自定义单向 OID",
    },
    "custom_dual": {
        "label": "自定义双向 OID",
    },
}

OPER_STATUS = {
    1: "up",
    2: "down",
    3: "testing",
    4: "unknown",
    5: "dormant",
    6: "notPresent",
    7: "lowerLayerDown",
}


class CollectorService:
    def __init__(self, config: MonitorConfig, db: MonitorDatabase) -> None:
        self.config = config
        self.db = db
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.collect_lock = threading.Lock()

    def update_config(self, config: MonitorConfig) -> None:
        self.config = config

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="traffic-collector", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def collect_once(self) -> None:
        with self.collect_lock:
            config = self.config
            self.db.sync_devices(config.devices)
            for device in config.devices:
                if not device.enabled:
                    continue
                try:
                    samples = collect_device(device, config)
                    self.db.save_samples(samples)
                    self.db.record_device_status(device.id, "online")
                    if samples:
                        self.db.add_event("info", f"{device.name} 采集正常，接口数 {len(samples)}", device.id)
                except Exception as exc:
                    self.db.record_device_status(device.id, "offline")
                    self.db.add_event("error", f"{device.name} 采集失败：{exc}", device.id)
            now = datetime.now(UTC)
            for period in ("day", "week", "month", "year"):
                self.db.rebuild_aggregates(period, now)
            self.db.enforce_retention(config.retention_days)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            self.collect_once()
            elapsed = time.monotonic() - started
            wait_seconds = max(1, self.config.sample_interval_seconds - elapsed)
            self.stop_event.wait(wait_seconds)


def collect_device(device: DeviceConfig, config: MonitorConfig) -> list[InterfaceSample]:
    if device.mock:
        return mock_samples(device)
    if device.snmp_version not in ("2c", "v2c"):
        raise SnmpError("当前离线版采集器仅支持 SNMPv2c；SNMPv3 需接入 pysnmp 后启用")
    client = SnmpV2cClient(
        host=device.host,
        community=device.community,
        port=device.port,
        timeout=config.snmp_timeout_seconds,
        retries=config.snmp_retries,
    )
    return snmp_interface_samples(device, client)


def discover_device_interfaces(device: DeviceConfig, config: MonitorConfig, allow_mock: bool = False) -> list[InterfaceSample]:
    if allow_mock and (config.mock_mode or device.mock):
        return mock_samples(device)
    if device.snmp_version not in ("2c", "v2c"):
        raise SnmpError("接口发现当前仅支持 SNMPv2c")
    client = SnmpV2cClient(
        host=device.host,
        community=device.community,
        port=device.port,
        timeout=config.snmp_timeout_seconds,
        retries=config.snmp_retries,
    )
    return snmp_interface_catalog(device, client)


def snmp_interface_samples(device: DeviceConfig, client: SnmpV2cClient) -> list[InterfaceSample]:
    if device.oid_profile == "custom_dual":
        return snmp_custom_dual_samples(device, client)
    if device.oid_profile == "custom_single":
        return snmp_custom_single_samples(device, client)
    profile = OID_PROFILES.get(device.oid_profile, OID_PROFILES["if_mib_64"])
    names = walk_table(client, IF_NAME)
    if not names:
        names = walk_table(client, IF_DESCR)
    aliases = walk_table(client, IF_ALIAS)
    statuses = walk_table(client, IF_OPER_STATUS)
    speeds = walk_table(client, str(profile["speed"]))
    in_octets = walk_table(client, str(profile["in_octets"]))
    out_octets = walk_table(client, str(profile["out_octets"]))
    indexes = sorted(set(names) | set(in_octets) | set(out_octets))
    now = datetime.now(UTC)
    samples: list[InterfaceSample] = []
    for if_index in indexes:
        if if_index not in in_octets or if_index not in out_octets:
            continue
        if not should_monitor_interface(device, if_index, names.get(if_index), aliases.get(if_index)):
            continue
        oper_value = statuses.get(if_index, 4)
        oper_status = OPER_STATUS.get(int_or_none(oper_value) or 4, "unknown")
        speed_value = int_or_none(speeds.get(if_index))
        speed_mbps = speed_value if profile["speed_unit"] == "mbps" else (speed_value // 1_000_000 if speed_value else None)
        samples.append(
            InterfaceSample(
                device_id=device.id,
                if_index=if_index,
                if_name=str(names.get(if_index) or f"ifIndex-{if_index}"),
                if_alias=str(aliases.get(if_index) or ""),
                oper_status=oper_status,
                if_speed_mbps=speed_mbps,
                in_octets=int(in_octets[if_index]),
                out_octets=int(out_octets[if_index]),
                sampled_at=now,
            )
        )
    if not samples:
        raise SnmpError("未读取到接口 64 位流量计数器，请确认设备支持 IF-MIB ifHCInOctets/ifHCOutOctets")
    return samples


def snmp_interface_catalog(device: DeviceConfig, client: SnmpV2cClient) -> list[InterfaceSample]:
    names = walk_table(client, IF_NAME)
    if not names:
        names = walk_table(client, IF_DESCR)
    aliases = walk_table(client, IF_ALIAS)
    statuses = walk_table(client, IF_OPER_STATUS)
    speeds = walk_table(client, IF_HIGH_SPEED)
    speed_unit = "mbps"
    if not speeds:
        speeds = walk_table(client, IF_SPEED)
        speed_unit = "bps"
    indexes = sorted(set(names) | set(aliases) | set(statuses) | set(speeds))
    if not indexes:
        raise SnmpError("未从设备读取到真实接口，请确认交换机已放行本机 SNMPv2c")
    now = datetime.now(UTC)
    samples: list[InterfaceSample] = []
    for if_index in indexes:
        if not should_monitor_interface(device, if_index, names.get(if_index), aliases.get(if_index)):
            continue
        speed_value = int_or_none(speeds.get(if_index))
        samples.append(
            InterfaceSample(
                device_id=device.id,
                if_index=if_index,
                if_name=str(names.get(if_index) or f"ifIndex-{if_index}"),
                if_alias=str(aliases.get(if_index) or ""),
                oper_status=OPER_STATUS.get(int_or_none(statuses.get(if_index)) or 4, "unknown"),
                if_speed_mbps=speed_value if speed_unit == "mbps" else (speed_value // 1_000_000 if speed_value else None),
                in_octets=0,
                out_octets=0,
                sampled_at=now,
            )
        )
    return samples


def snmp_custom_single_samples(device: DeviceConfig, client: SnmpV2cClient) -> list[InterfaceSample]:
    custom_oid = device.custom_oid.strip()
    if not custom_oid:
        raise SnmpError("自定义单向监控必须填写 SNMP OID")
    names = walk_table(client, IF_NAME)
    if not names:
        names = walk_table(client, IF_DESCR)
    aliases = walk_table(client, IF_ALIAS)
    statuses = walk_table(client, IF_OPER_STATUS)
    speeds = walk_table(client, IF_HIGH_SPEED)
    values = read_oid_index_values(client, custom_oid)
    now = datetime.now(UTC)
    samples: list[InterfaceSample] = []
    for if_index in sorted(values):
        raw_value = int_or_none(values.get(if_index))
        if raw_value is None:
            continue
        if not should_monitor_interface(device, if_index, names.get(if_index), aliases.get(if_index)):
            continue
        is_out = device.custom_direction.lower() == "out"
        samples.append(
            InterfaceSample(
                device_id=device.id,
                if_index=if_index,
                if_name=str(names.get(if_index) or f"customOid-{if_index}"),
                if_alias=str(aliases.get(if_index) or custom_oid),
                oper_status=OPER_STATUS.get(int_or_none(statuses.get(if_index)) or 4, "unknown"),
                if_speed_mbps=int_or_none(speeds.get(if_index)),
                in_octets=0 if is_out else raw_value,
                out_octets=raw_value if is_out else 0,
                sampled_at=now,
            )
        )
    if not samples:
        raise SnmpError(f"自定义 OID 没有返回可用数值：{custom_oid}")
    return samples


def snmp_custom_dual_samples(device: DeviceConfig, client: SnmpV2cClient) -> list[InterfaceSample]:
    in_oid = device.custom_in_oid.strip()
    out_oid = device.custom_out_oid.strip()
    if not in_oid or not out_oid:
        raise SnmpError("自定义双向监控必须填写入方向和出方向 SNMP OID")
    names = walk_table(client, IF_NAME)
    if not names:
        names = walk_table(client, IF_DESCR)
    aliases = walk_table(client, IF_ALIAS)
    statuses = walk_table(client, IF_OPER_STATUS)
    speeds = walk_table(client, IF_HIGH_SPEED)
    in_values = read_oid_index_values(client, in_oid)
    out_values = read_oid_index_values(client, out_oid)
    now = datetime.now(UTC)
    samples: list[InterfaceSample] = []
    for if_index in sorted(set(in_values) | set(out_values)):
        in_value = int_or_none(in_values.get(if_index))
        out_value = int_or_none(out_values.get(if_index))
        if in_value is None and out_value is None:
            continue
        if not should_monitor_interface(device, if_index, names.get(if_index), aliases.get(if_index)):
            continue
        samples.append(
            InterfaceSample(
                device_id=device.id,
                if_index=if_index,
                if_name=str(names.get(if_index) or f"customOid-{if_index}"),
                if_alias=str(aliases.get(if_index) or f"{in_oid} / {out_oid}"),
                oper_status=OPER_STATUS.get(int_or_none(statuses.get(if_index)) or 4, "unknown"),
                if_speed_mbps=int_or_none(speeds.get(if_index)),
                in_octets=in_value or 0,
                out_octets=out_value or 0,
                sampled_at=now,
            )
        )
    if not samples:
        raise SnmpError(f"自定义双向 OID 没有返回可用数值：{in_oid} / {out_oid}")
    return samples


def read_oid_index_values(client: SnmpV2cClient, oid: str) -> dict[int, Any]:
    values = walk_table(client, oid)
    if values:
        return values
    result: dict[int, Any] = {}
    for item in client.get([oid]):
        if item.value is not None:
            result[int(item.oid[-1])] = item.value
    return result


def should_monitor_interface(device: DeviceConfig, if_index: int, if_name: Any, if_alias: Any) -> bool:
    if not device.monitor_interfaces:
        return True
    candidates = {
        str(if_index).lower(),
        str(if_name or "").lower(),
        str(if_alias or "").lower(),
    }
    return any(item.lower() in candidates for item in device.monitor_interfaces)


def walk_table(client: SnmpV2cClient, base_oid: str) -> dict[int, Any]:
    base = tuple(int(part) for part in base_oid.strip(".").split("."))
    result: dict[int, Any] = {}
    for varbind in client.walk(base):
        if len(varbind.oid) <= len(base):
            continue
        result[int(varbind.oid[-1])] = varbind.value
    return result


def int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def mock_samples(device: DeviceConfig) -> list[InterfaceSample]:
    now = datetime.now(UTC)
    current_second = int(time.time())
    interfaces = [
        (1, "GigabitEthernet0/0/1", "上联-办公网", 1000),
        (2, "GigabitEthernet0/0/2", "服务器区", 1000),
        (24, "GigabitEthernet0/0/24", "核心互联", 10000),
        (101, "XGigabitEthernet0/0/1", "汇聚上联", 10000),
    ]
    rows: list[InterfaceSample] = []
    for if_index, name, alias, speed in interfaces:
        if not should_monitor_interface(device, if_index, name, alias):
            continue
        phase = current_second / 300 + if_index
        base = 600_000_000_000 + if_index * 100_000_000_000
        in_rate = 18_000_000 + if_index * 220_000
        out_rate = 4_000_000 + if_index * 60_000
        in_wave = int((math.sin(phase) + 1.0) * 90_000_000)
        out_wave = int((math.cos(phase / 1.7) + 1.0) * 20_000_000)
        counter_base = current_second
        rows.append(
            InterfaceSample(
                device_id=device.id,
                if_index=if_index,
                if_name=name,
                if_alias=alias,
                oper_status="up" if if_index != 2 else "down",
                if_speed_mbps=speed,
                in_octets=base + counter_base * in_rate + in_wave,
                out_octets=base // 10 + counter_base * out_rate + out_wave,
                sampled_at=now,
            )
        )
    return rows
