import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.database import InterfaceSample, MonitorDatabase, calculate_delta
from app.collector import IF_ALIAS, IF_NAME, IF_OPER_STATUS, snmp_custom_dual_samples, snmp_custom_single_samples, snmp_interface_catalog
from app.config import DEFAULT_CONFIG, DEFAULT_CUSTOM_IN_OID, DEFAULT_CUSTOM_OUT_OID, DeviceConfig, load_config, save_config_json
from app.snmp_v2c import decode_response, encode_message, encode_oid, parse_oid
from app.snmp_v2c import VarBind
from app.tray import WindowsTrayIcon, is_tray_supported, pixel_for
from app.web import SETTINGS_HTML, build_stats, merge_saved_communities, safe_config_json, traffic_window
from monitor import build_parser


UTC = timezone.utc


class SnmpV2cTests(unittest.TestCase):
    def test_oid_round_trip_in_packet(self) -> None:
        oid = parse_oid(".1.3.6.1.2.1.31.1.1.1.6")
        encoded = encode_oid(oid)
        self.assertEqual(encoded[0], 0x06)
        self.assertIn(b"\x2b\x06\x01\x02\x01", encoded)

    def test_decode_response_counter64(self) -> None:
        request_id = 100
        oid = parse_oid(".1.3.6.1.2.1.31.1.1.1.6.24")
        varbind = b"\x30" + bytes([len(encode_oid(oid)) + 8]) + encode_oid(oid) + b"\x46\x06\x01\x02\x03\x04\x05\x06"
        pdu_body = b"\x02\x01\x64\x02\x01\x00\x02\x01\x00" + b"\x30" + bytes([len(varbind)]) + varbind
        packet = b"\x30" + bytes([3 + 8 + len(pdu_body) + 2]) + b"\x02\x01\x01\x04\x06public" + b"\xa2" + bytes([len(pdu_body)]) + pdu_body
        rows = decode_response(packet, expected_request_id=request_id)
        self.assertEqual(rows[0].oid, oid)
        self.assertEqual(rows[0].value, 0x010203040506)

    def test_encode_getbulk_message(self) -> None:
        packet = encode_message("public", 0xA5, 7, 0, 25, [parse_oid(".1.3.6.1.2.1.2.2.1.8")])
        self.assertIn(b"public", packet)
        self.assertIn(b"\xa5", packet)


class DeltaTests(unittest.TestCase):
    def test_first_sample_has_no_delta(self) -> None:
        sample = InterfaceSample("d1", 1, "GE0/0/1", "", "up", 1000, 100, 200, datetime.now(UTC))
        delta = calculate_delta(None, sample)
        self.assertEqual(delta["sample_status"], "first_sample")
        self.assertIsNone(delta["in_bps"])

    def test_delta_and_bps(self) -> None:
        now = datetime.now(UTC)
        prev = row({"sampled_at": (now - timedelta(seconds=60)).isoformat(), "in_octets": 1000, "out_octets": 500})
        sample = InterfaceSample("d1", 1, "GE0/0/1", "", "up", 1000, 1600, 1100, now)
        delta = calculate_delta(prev, sample)
        self.assertEqual(delta["in_delta_bytes"], 600)
        self.assertEqual(delta["out_delta_bytes"], 600)
        self.assertAlmostEqual(float(delta["in_bps"]), 80.0)

    def test_counter_reset(self) -> None:
        now = datetime.now(UTC)
        prev = row({"sampled_at": (now - timedelta(seconds=60)).isoformat(), "in_octets": 1000, "out_octets": 500})
        sample = InterfaceSample("d1", 1, "GE0/0/1", "", "up", 1000, 900, 1100, now)
        delta = calculate_delta(prev, sample)
        self.assertEqual(delta["sample_status"], "counter_reset")
        self.assertIsNone(delta["in_bps"])


class StatsTests(unittest.TestCase):
    def test_stats_ignore_invalid_samples(self) -> None:
        stats = build_stats(
            [
                {"rx": 0, "tx": 0, "total": 0, "status": "first_sample"},
                {"rx": 10, "tx": 5, "total": 15, "status": "ok"},
                {"rx": 20, "tx": 7, "total": 27, "status": "ok"},
            ]
        )
        self.assertEqual(stats["total"]["min"], 15)
        self.assertEqual(stats["total"]["max"], 27)
        self.assertEqual(stats["total"]["p95"], 27)

    def test_traffic_window_hour(self) -> None:
        now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
        start, end = traffic_window({"period": ["hour"]}, now)
        self.assertEqual(end, now)
        self.assertEqual(start, now - timedelta(hours=1))

    def test_traffic_window_custom(self) -> None:
        now = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)
        start, end = traffic_window(
            {
                "start": ["2026-07-01T08:30:00+00:00"],
                "end": ["2026-07-02T08:30:00+00:00"],
            },
            now,
        )
        self.assertEqual(start, datetime(2026, 7, 1, 8, 30, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 7, 2, 8, 30, tzinfo=UTC))

    def test_settings_save_has_visible_status_feedback(self) -> None:
        self.assertIn('id="status" class="status" role="status"', SETTINGS_HTML)
        self.assertIn("正在保存配置", SETTINGS_HTML)
        self.assertIn("保存时间", SETTINGS_HTML)

    def test_monitor_accepts_no_tray_option(self) -> None:
        args = build_parser().parse_args(["--no-tray"])
        self.assertTrue(args.no_tray)

    def test_monitor_accepts_background_child_option(self) -> None:
        args = build_parser().parse_args(["--background-child"])
        self.assertTrue(args.background_child)

    def test_tray_module_constructs_on_supported_platform(self) -> None:
        if not is_tray_supported():
            self.skipTest("Windows tray icon is only supported on Windows")
        tray = WindowsTrayIcon("test", "http://127.0.0.1/", "http://127.0.0.1/settings", lambda: None)
        self.assertEqual(tray.title, "test")

    def test_tray_icon_pixels_use_project_specific_shape(self) -> None:
        self.assertEqual(pixel_for(32, 0, 0), (0, 0, 0, 0))
        self.assertEqual(pixel_for(32, 16, 8), (207, 10, 44, 255))
        self.assertEqual(pixel_for(32, 16, 17), (255, 255, 255, 255))


class CollectorTests(unittest.TestCase):
    def test_interface_catalog_uses_device_metadata(self) -> None:
        client = FakeSnmpClient(
            {
                IF_NAME: {24: "GigabitEthernet0/0/24"},
                IF_ALIAS: {24: "核心互联"},
                IF_OPER_STATUS: {24: 1},
            }
        )
        device = DeviceConfig("d1", "core", "192.0.2.1")

        samples = snmp_interface_catalog(device, client)

        self.assertEqual(samples[0].if_index, 24)
        self.assertEqual(samples[0].if_name, "GigabitEthernet0/0/24")
        self.assertEqual(samples[0].oper_status, "up")
        self.assertEqual(samples[0].in_octets, 0)
        self.assertEqual(samples[0].out_octets, 0)

    def test_custom_single_oid_records_one_direction(self) -> None:
        custom_oid = ".1.3.6.1.4.1.2011.5.25.31.1.1"
        client = FakeSnmpClient(
            {
                IF_NAME: {24: "GigabitEthernet0/0/24"},
                custom_oid: {24: 123456},
            }
        )
        device = DeviceConfig(
            "d1",
            "core",
            "192.0.2.1",
            oid_profile="custom_single",
            custom_oid=custom_oid,
            custom_direction="out",
        )

        samples = snmp_custom_single_samples(device, client)

        self.assertEqual(samples[0].in_octets, 0)
        self.assertEqual(samples[0].out_octets, 123456)

    def test_custom_dual_oid_records_rx_and_tx(self) -> None:
        in_oid = ".1.3.6.1.2.1.31.1.1.1.6.45"
        out_oid = ".1.3.6.1.2.1.31.1.1.1.10.45"
        client = FakeSnmpClient(
            {
                IF_NAME: {45: "Eth-Trunk1"},
                in_oid: {45: 1000},
                out_oid: {45: 3000},
            }
        )
        device = DeviceConfig(
            "d1",
            "core",
            "192.0.2.1",
            oid_profile="custom_dual",
            custom_in_oid=in_oid,
            custom_out_oid=out_oid,
        )

        samples = snmp_custom_dual_samples(device, client)

        self.assertEqual(samples[0].if_index, 45)
        self.assertEqual(samples[0].in_octets, 1000)
        self.assertEqual(samples[0].out_octets, 3000)


class ConfigTests(unittest.TestCase):
    def test_sample_interval_minimum_is_thirty_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = DEFAULT_CONFIG.copy()
            config["sample_interval_seconds"] = 3
            (data_dir / "config.json").write_text(__import__("json").dumps(config), encoding="utf-8")

            loaded = load_config(data_dir)

            self.assertEqual(loaded.sample_interval_seconds, 30)

    def test_safe_config_hides_community(self) -> None:
        config = {"devices": [{"id": "d1", "community": "secret"}]}

        safe = safe_config_json(config)

        self.assertEqual(safe["devices"][0]["community"], "")
        self.assertTrue(safe["devices"][0]["community_set"])
        self.assertEqual(config["devices"][0]["community"], "secret")

    def test_blank_community_keeps_saved_secret(self) -> None:
        existing = {"devices": [{"id": "d1", "community": "secret"}]}
        incoming = {"devices": [{"id": "d1", "community": ""}, {"id": "d2", "community": ""}]}

        merged = merge_saved_communities(existing, incoming)

        self.assertEqual(merged["devices"][0]["community"], "secret")
        self.assertEqual(merged["devices"][1]["community"], "public")

    def test_custom_dual_oids_default_when_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            config = {
                "listen_host": "127.0.0.1",
                "listen_port": 8088,
                "sample_interval_seconds": 30,
                "retention_days": 400,
                "snmp_timeout_seconds": 2.0,
                "snmp_retries": 1,
                "mock_mode": False,
                "devices": [
                    {
                        "id": "d1",
                        "name": "core",
                        "host": "192.0.2.1",
                        "snmp_version": "2c",
                        "community": "public",
                        "oid_profile": "custom_dual",
                        "custom_in_oid": "",
                        "custom_out_oid": "",
                    }
                ],
            }

            save_config_json(data_dir, config)
            loaded = load_config(data_dir)

            self.assertEqual(config["devices"][0]["custom_in_oid"], DEFAULT_CUSTOM_IN_OID)
            self.assertEqual(config["devices"][0]["custom_out_oid"], DEFAULT_CUSTOM_OUT_OID)
            self.assertEqual(loaded.devices[0].custom_in_oid, DEFAULT_CUSTOM_IN_OID)
            self.assertEqual(loaded.devices[0].custom_out_oid, DEFAULT_CUSTOM_OUT_OID)


class DatabaseSummaryTests(unittest.TestCase):
    def test_daily_traffic_totals_group_by_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = MonitorDatabase(Path(tmp) / "traffic.db")
            db.upsert_device("d1", "core", "192.0.2.1", "2c", True)
            db.save_samples(
                [
                    InterfaceSample("d1", 1, "Eth1", "", "up", 1000, 100, 200, datetime(2026, 7, 1, 0, 0, tzinfo=UTC)),
                    InterfaceSample("d1", 1, "Eth1", "", "up", 1000, 160, 260, datetime(2026, 7, 1, 0, 1, tzinfo=UTC)),
                    InterfaceSample("d1", 1, "Eth1", "", "up", 1000, 250, 320, datetime(2026, 7, 2, 0, 1, tzinfo=UTC)),
                ]
            )
            rows = db.daily_traffic_totals("d1", 1, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 3, tzinfo=UTC))

            self.assertEqual(rows[0]["day"], "2026-07-02")
            self.assertEqual(rows[0]["in_total_bytes"], 90)
            self.assertEqual(rows[0]["out_total_bytes"], 60)
            self.assertEqual(rows[1]["day"], "2026-07-01")
            self.assertEqual(rows[1]["in_total_bytes"], 60)
            self.assertEqual(rows[1]["out_total_bytes"], 60)

    def test_recent_events_can_filter_problem_severity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = MonitorDatabase(Path(tmp) / "traffic.db")
            db.add_event("info", "normal")
            db.add_event("warn", "warning")
            db.add_event("error", "failure")

            rows = db.recent_events(severities=("warn", "error"))

            self.assertEqual([row["severity"] for row in rows], ["error", "warn"])

    def test_interface_and_device_totals_for_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = MonitorDatabase(Path(tmp) / "traffic.db")
            db.upsert_device("d1", "core", "192.0.2.1", "2c", True)
            db.save_samples(
                [
                    InterfaceSample("d1", 1, "Eth1", "", "up", 1000, 100, 200, datetime(2026, 7, 1, 0, 0, tzinfo=UTC)),
                    InterfaceSample("d1", 1, "Eth1", "", "up", 1000, 250, 500, datetime(2026, 7, 1, 0, 1, tzinfo=UTC)),
                ]
            )

            start = datetime(2026, 7, 1, tzinfo=UTC)
            end = datetime(2026, 7, 2, tzinfo=UTC)
            interface_rows = db.interface_traffic_totals("d1", start, end)
            device_rows = db.device_traffic_totals(start, end)

            self.assertEqual(interface_rows[0]["in_total_bytes"], 150)
            self.assertEqual(interface_rows[0]["out_total_bytes"], 300)
            self.assertEqual(device_rows[0]["in_total_bytes"], 150)
            self.assertEqual(device_rows[0]["out_total_bytes"], 300)


class FakeSnmpClient:
    def __init__(self, tables: dict[str, dict[int, object]]) -> None:
        self.tables = {tuple(parse_oid(base)): values for base, values in tables.items()}

    def walk(self, base_oid: str | tuple[int, ...], max_repetitions: int = 25, max_rows: int = 10000) -> list[VarBind]:
        base = parse_oid(base_oid)
        return [
            VarBind(oid=base + (if_index,), value=value, tag=0x02)
            for if_index, value in sorted(self.tables.get(base, {}).items())
        ]

    def get(self, oids: list[str | tuple[int, ...]]) -> list[VarBind]:
        rows: list[VarBind] = []
        for oid in oids:
            parsed = parse_oid(oid)
            base = parsed[:-1]
            value = self.tables.get(base, {}).get(parsed[-1])
            rows.append(VarBind(oid=parsed, value=value, tag=0x02))
        return rows


def row(values: dict) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t(sampled_at TEXT, in_octets INTEGER, out_octets INTEGER)")
    conn.execute(
        "INSERT INTO t(sampled_at, in_octets, out_octets) VALUES(?, ?, ?)",
        (values["sampled_at"], values["in_octets"], values["out_octets"]),
    )
    return conn.execute("SELECT * FROM t").fetchone()


if __name__ == "__main__":
    unittest.main()
