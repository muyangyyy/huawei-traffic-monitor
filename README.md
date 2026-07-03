# 华为交换机接口流量监控

这是一个 Windows 本机部署的轻量监控程序，用于采集华为交换机接口 Rx/Tx 流量，保存 SQLite 数据，并提供本地 Web 图表看板。

## 功能

- SNMPv2c 只读采集接口流量。
- 采集 IF-MIB 的 64 位计数器：`ifHCInOctets`、`ifHCOutOctets`。
- SQLite 保存原始采样和日、周、月、年汇总。
- 默认原始数据保留 400 天。
- 本地 Web 页面展示 Rx、Tx、总字节数、95th、统计表和采集事件。
- 支持模拟数据模式，安装后可立即查看页面。
- 安装包内置独立运行程序，目标机器不需要预装 Python。

## 源码方式启动

```bat
python monitor.py --data-dir data
```

默认地址：

```text
http://127.0.0.1:8088/
```

## 配置真实交换机

首次启动会生成：

```text
data\config.json
```

将 `mock_mode` 改为 `false`，并按现场交换机配置设备：

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8088,
  "sample_interval_seconds": 60,
  "retention_days": 400,
  "snmp_timeout_seconds": 2.0,
  "snmp_retries": 1,
  "mock_mode": false,
  "devices": [
    {
      "id": "s5700-core-01",
      "name": "S5700-Core-01",
      "host": "192.168.1.10",
      "snmp_version": "2c",
      "community": "public",
      "enabled": true,
      "mock": false
    }
  ]
}
```

## 安全说明

- 当前离线版内置 SNMPv2c 客户端。
- 生产环境建议在交换机侧限制 SNMP 访问源 IP。
- SNMPv3 可作为第二阶段接入成熟库后实现。
- 本程序只读 SNMP，不会修改交换机配置。
