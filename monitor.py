import argparse
import signal
import sys
import threading
from pathlib import Path

from app.collector import CollectorService
from app.config import load_config
from app.database import MonitorDatabase
from app.web import MonitorHttpServer


def main() -> int:
    parser = argparse.ArgumentParser(description="Huawei switch interface traffic monitor")
    parser.add_argument("--data-dir", default="data", help="配置和数据库目录")
    parser.add_argument("--collect-once", action="store_true", help="只执行一次采集后退出")
    args = parser.parse_args()

    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir

    config = load_config(data_dir)
    db = MonitorDatabase(data_dir / "traffic.db")
    collector = CollectorService(config, db)

    def reload_runtime_config() -> None:
        new_config = load_config(data_dir)
        collector.update_config(new_config)
        db.sync_devices(new_config.devices, reset_interfaces=True)
        threading.Thread(target=collector.collect_once, name="traffic-collect-after-save", daemon=True).start()

    collector.collect_once()
    if args.collect_once:
        print(f"采集完成，数据库：{data_dir / 'traffic.db'}")
        return 0

    server = MonitorHttpServer((config.listen_host, config.listen_port), db, data_dir, on_config_saved=reload_runtime_config)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        collector.stop()
        server.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    collector.start()

    url = f"http://{config.listen_host}:{config.listen_port}/"
    print(f"Huawei Traffic Monitor 已启动：{url}")
    print(f"配置文件：{data_dir / 'config.json'}")
    print(f"数据库：{data_dir / 'traffic.db'}")
    try:
        server.serve_forever()
    finally:
        collector.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
