# 华为交换机接口流量监控

一个面向 Windows 本机部署的轻量级华为交换机接口流量监控程序。程序通过 SNMPv2c 采集交换机接口进/出方向流量，使用 SQLite 保存历史数据，并提供本地 Web 看板展示实时趋势、时间范围查询和每日汇总。

## 当前功能

- 通过 SNMPv2c 只读采集华为交换机接口流量。
- 支持 IF-MIB 64 位接口计数器：`ifHCInOctets`、`ifHCOutOctets`。
- 支持 IF-MIB 32 位兼容计数器。
- 支持自定义单向 OID 监控。
- 支持自定义双向 OID 监控，例如分别配置入方向和出方向 OID。
- 支持从设备端真实发现接口，按接口勾选需要监控的 ifIndex。
- 监控接口区域支持全选、半选状态和手动填写 ifIndex 或接口名。
- 后端 SNMP 采集周期默认 30 秒，最小 30 秒。
- 前端看板趋势图默认 30 秒自动刷新。
- 支持 1 小时、24 小时、今天、7 天、30 天、90 天和自定义时间范围查询。
- 看板展示当前速率、范围汇总流量、95th、每日 Rx/Tx/合计流量汇总。
- SQLite 保存原始采样和统计数据，默认保留 400 天，满足一年以上查询。
- 提供对接设置页面，用于添加设备、填写团体字、选择 OID 模板和配置监控接口。
- 可打包为 Windows 安装程序，安装时支持选择安装路径。

## 项目结构

```text
huawei_traffic_monitor/
├─ app/
│  ├─ collector.py      # SNMP 采集和接口发现
│  ├─ config.py         # 配置读取、校验和默认值
│  ├─ database.py       # SQLite 存储和统计查询
│  ├─ snmp_v2c.py       # 内置 SNMPv2c 客户端
│  └─ web.py            # 本地 Web API 和前端页面
├─ installer/
│  ├─ build_setup.ps1   # 打包 Windows 安装程序
│  ├─ install.cmd       # 安装辅助脚本
│  └─ setup_installer.py
├─ tests/
│  └─ test_core.py
├─ monitor.py           # 程序入口
└─ README.md
```

## 源码运行

需要本机已安装 Python。

```bat
python monitor.py --data-dir data
```

默认访问地址：

```text
http://127.0.0.1:8088/
```

对接设置页面：

```text
http://127.0.0.1:8088/settings
```

## 配置设备

首次启动会自动生成：

```text
data\config.json
```

也可以直接在 Web 页面进入“对接设置”添加设备。建议通过页面配置，避免手动改 JSON 出错。

关键配置项示例：

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8088,
  "sample_interval_seconds": 30,
  "retention_days": 400,
  "snmp_timeout_seconds": 2.0,
  "snmp_retries": 1,
  "mock_mode": false,
  "devices": [
    {
      "id": "s5700-core-01",
      "name": "S5700-Core-01",
      "host": "192.168.1.10",
      "port": 161,
      "snmp_version": "2c",
      "community": "public",
      "oid_profile": "if_mib_64",
      "monitor_interfaces": ["45"],
      "enabled": true,
      "mock": false
    }
  ]
}
```

## OID 模板

可在“对接设置”中选择：

- `if_mib_64`：IF-MIB 64 位接口流量，优先推荐。
- `if_mib_32`：IF-MIB 32 位兼容接口流量。
- `custom_single`：自定义单向 OID，只采集入方向或出方向。
- `custom_dual`：自定义双向 OID，分别填写入方向和出方向 OID。

自定义双向 OID 示例：

```text
入方向 OID: .1.3.6.1.2.1.31.1.1.1.6.45
出方向 OID: .1.3.6.1.2.1.31.1.1.1.10.45
```

## 打包安装程序

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\build_setup.ps1
```

打包完成后安装程序输出到：

```text
..\dist\HuaweiTrafficMonitorSetup.exe
```

安装程序支持选择安装路径。安装完成后可通过本机浏览器打开看板和设置页面。

## 测试

```bat
python -m unittest discover -s tests
```

## 安全说明

- 本程序只读 SNMP，不会修改交换机配置。
- 不要把真实 `data\config.json`、`dev_data\config.json`、SQLite 数据库或安装包上传到公开仓库。
- 生产环境建议在交换机侧限制 SNMP 访问源 IP。
- SNMPv2c 团体字应按现场安全规范设置，避免使用默认值。
- 当前仓库的 `.gitignore` 已默认排除运行数据、数据库、缓存和打包产物。
