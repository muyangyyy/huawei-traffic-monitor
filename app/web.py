import json
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .collector import discover_device_interfaces
from .config import DeviceConfig
from .config import read_config_json, save_config_json
from .database import MonitorDatabase


UTC = timezone.utc


class MonitorHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        db: MonitorDatabase,
        data_dir: Path,
        on_config_saved: Callable[[], None] | None = None,
    ) -> None:
        self.db = db
        self.data_dir = data_dir
        self.on_config_saved = on_config_saved
        super().__init__(server_address, MonitorRequestHandler)


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorHttpServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.send_html(DASHBOARD_HTML)
            elif parsed.path == "/settings":
                self.send_html(SETTINGS_HTML)
            elif parsed.path == "/api/overview":
                self.send_json(self.server.db.overview())
            elif parsed.path == "/api/config":
                self.send_json(safe_config_json(read_config_json(self.server.data_dir)))
            elif parsed.path == "/api/devices":
                self.send_json({"devices": self.server.db.list_devices()})
            elif parsed.path == "/api/interfaces":
                query = parse_qs(parsed.query)
                device_id = query.get("device_id", [""])[0]
                self.send_json({"interfaces": self.server.db.list_interfaces(device_id)})
            elif parsed.path == "/api/traffic":
                self.send_json(self.api_traffic(parsed.query))
            elif parsed.path == "/api/daily_totals":
                self.send_json(self.api_daily_totals(parsed.query))
            elif parsed.path == "/api/range_summary":
                self.send_json(self.api_range_summary(parsed.query))
            elif parsed.path == "/api/events":
                self.send_json({"events": self.server.db.recent_events(severities=("warn", "error"))})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/config":
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                value = json.loads(raw)
                value = merge_saved_communities(read_config_json(self.server.data_dir), value)
                save_config_json(self.server.data_dir, value)
                if self.server.on_config_saved:
                    self.server.on_config_saved()
                self.send_json({"ok": True, "message": "配置已保存，已联动看板并触发采集"})
                return
            if parsed.path == "/api/discover":
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                value = json.loads(raw)
                value = merge_saved_communities(read_config_json(self.server.data_dir), {"devices": [value]})["devices"][0]
                self.send_json(discover_interfaces(value))
                return
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
                return
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)

    def api_traffic(self, query_text: str) -> dict:
        query = parse_qs(query_text)
        device_id = query.get("device_id", [""])[0]
        interface_id = int(query.get("interface_id", ["0"])[0] or "0")
        start, end = traffic_window(query, datetime.now(UTC))
        rows = self.server.db.traffic_series(device_id, interface_id, start, end)
        points = []
        for row in rows:
            in_bytes = int(row["in_delta_bytes"] or 0)
            out_bytes = int(row["out_delta_bytes"] or 0)
            points.append(
                {
                    "time": row["sampled_at"],
                    "rx": in_bytes,
                    "tx": out_bytes,
                    "total": in_bytes + out_bytes,
                    "in_bps": float(row["in_bps"] or 0),
                    "out_bps": float(row["out_bps"] or 0),
                    "status": row["sample_status"],
                }
            )
        return {"points": points, "stats": build_stats(points)}

    def api_daily_totals(self, query_text: str) -> dict:
        query = parse_qs(query_text)
        device_id = query.get("device_id", [""])[0]
        interface_id = int(query.get("interface_id", ["0"])[0] or "0")
        start, end = traffic_window(query, datetime.now(UTC))
        rows = self.server.db.daily_traffic_totals(device_id, interface_id, start, end)
        days = []
        for row in rows:
            rx = int(row["in_total_bytes"] or 0)
            tx = int(row["out_total_bytes"] or 0)
            days.append(
                {
                    "day": row["day"],
                    "rx": rx,
                    "tx": tx,
                    "total": rx + tx,
                    "sample_count": int(row["sample_count"] or 0),
                }
            )
        return {"days": days}

    def api_range_summary(self, query_text: str) -> dict:
        query = parse_qs(query_text)
        device_id = query.get("device_id", [""])[0]
        start, end = traffic_window(query, datetime.now(UTC))
        devices = []
        for row in self.server.db.device_traffic_totals(start, end):
            rx = int(row["in_total_bytes"] or 0)
            tx = int(row["out_total_bytes"] or 0)
            devices.append(
                {
                    "device_id": row["device_id"],
                    "name": row["name"],
                    "status": row["last_status"],
                    "rx": rx,
                    "tx": tx,
                    "total": rx + tx,
                    "sample_count": int(row["sample_count"] or 0),
                }
            )
        interfaces = []
        if device_id:
            for row in self.server.db.interface_traffic_totals(device_id, start, end):
                rx = int(row["in_total_bytes"] or 0)
                tx = int(row["out_total_bytes"] or 0)
                interfaces.append(
                    {
                        "interface_id": int(row["interface_id"]),
                        "if_index": int(row["if_index"]),
                        "if_name": row["if_name"],
                        "if_alias": row["if_alias"],
                        "oper_status": row["oper_status"],
                        "rx": rx,
                        "tx": tx,
                        "total": rx + tx,
                        "in_max_bps": float(row["in_max_bps"] or 0),
                        "out_max_bps": float(row["out_max_bps"] or 0),
                        "sample_count": int(row["sample_count"] or 0),
                    }
                )
        return {"devices": devices, "interfaces": interfaces}

    def send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, value: object, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def safe_config_json(value: dict) -> dict:
    safe = json.loads(json.dumps(value))
    for device in safe.get("devices", []):
        if device.get("community"):
            device["community"] = ""
            device["community_set"] = True
    return safe


def merge_saved_communities(existing: dict, incoming: dict) -> dict:
    saved_by_id = {
        str(device.get("id")): str(device.get("community", ""))
        for device in existing.get("devices", [])
        if isinstance(device, dict) and device.get("id")
    }
    for device in incoming.get("devices", []):
        if not isinstance(device, dict):
            continue
        device_id = str(device.get("id") or "")
        community = str(device.get("community", ""))
        if community:
            continue
        if device_id in saved_by_id and saved_by_id[device_id]:
            device["community"] = saved_by_id[device_id]
        else:
            device["community"] = "public"
    return incoming


def build_stats(points: list[dict]) -> dict:
    valid_points = [p for p in points if p.get("status") == "ok"]
    return {
        "total": series_stats([float(p["total"]) for p in valid_points]),
        "rx": series_stats([float(p["rx"]) for p in valid_points]),
        "tx": series_stats([float(p["tx"]) for p in valid_points]),
    }


def series_stats(values: list[float]) -> dict:
    if not values:
        return {"min": 0, "max": 0, "avg": 0, "p95_avg": 0, "p95": 0}
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, round_up(len(ordered) * 0.95) - 1))
    p95 = ordered[p95_index]
    upper = [v for v in ordered if v >= p95]
    return {
        "min": ordered[0],
        "max": ordered[-1],
        "avg": sum(ordered) / len(ordered),
        "p95_avg": sum(upper) / len(upper),
        "p95": p95,
    }


def round_up(value: float) -> int:
    as_int = int(value)
    return as_int if value == as_int else as_int + 1


def traffic_window(query: dict[str, list[str]], now: datetime) -> tuple[datetime, datetime]:
    end = parse_time_param(query.get("end", [""])[0]) or now
    start_param = parse_time_param(query.get("start", [""])[0])
    if start_param:
        return start_param, end
    period = query.get("period", ["24h"])[0]
    if period in ("hour", "1h", "realtime"):
        return end - timedelta(hours=1), end
    if period in ("24h", "day"):
        return end - timedelta(hours=24), end
    if period == "today":
        return end.replace(hour=0, minute=0, second=0, microsecond=0), end
    if period in ("7d", "week"):
        return end - timedelta(days=7), end
    if period in ("30d", "month"):
        return end - timedelta(days=30), end
    if period == "90d":
        return end - timedelta(days=90), end
    if period in ("365d", "year"):
        return end - timedelta(days=365), end
    return end - timedelta(hours=24), end


def parse_time_param(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def discover_interfaces(value: dict) -> dict:
    device = DeviceConfig(
        id=str(value.get("id") or "discover"),
        name=str(value.get("name") or value.get("host") or "discover"),
        host=str(value.get("host") or ""),
        snmp_version=str(value.get("snmp_version", "2c")).lower(),
        community=str(value.get("community", "public")),
        oid_profile=str(value.get("oid_profile", "if_mib_64")),
        custom_oid=str(value.get("custom_oid", "")),
        custom_direction=str(value.get("custom_direction", "in")).lower(),
        custom_in_oid=str(value.get("custom_in_oid", "")),
        custom_out_oid=str(value.get("custom_out_oid", "")),
        monitor_interfaces=(),
        port=int(value.get("port", 161)),
        enabled=True,
        mock=bool(value.get("demo_discover", False)),
    )
    if not device.host:
        raise ValueError("请填写设备管理 IP")
    config = SimpleNamespace(
        mock_mode=bool(value.get("demo_discover", False)),
        snmp_timeout_seconds=float(value.get("snmp_timeout_seconds", 2.0)),
        snmp_retries=int(value.get("snmp_retries", 1)),
    )
    samples = discover_device_interfaces(device, config, allow_mock=bool(value.get("demo_discover", False)))
    return {
        "interfaces": [
            {
                "if_index": item.if_index,
                "if_name": item.if_name,
                "if_alias": item.if_alias,
                "oper_status": item.oper_status,
                "if_speed_mbps": item.if_speed_mbps,
            }
            for item in samples
        ]
    }


SETTINGS_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>对接设置 - 华为交换机接口流量监控</title>
  <style>
    :root{--bg:#f5f7fa;--panel:#fff;--line:#d9e1ea;--text:#26323f;--muted:#667789;--total:#4d9bd2;--danger:#d94a4a;--ok:#1f9d63;--warn:#d99820;--shadow:0 10px 24px rgba(31,45,61,.08)}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif}.app{min-height:100vh;padding:18px}.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.title h1{margin:0;font-size:20px}.title small{display:block;color:var(--muted);font-size:12px;margin-top:4px}.actions{display:flex;gap:8px;flex-wrap:wrap}button,input,select,textarea{border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--text);font-size:13px}button{height:34px;padding:0 12px;cursor:pointer}button.primary{background:var(--total);border-color:var(--total);color:#fff}button.danger{color:var(--danger)}button:disabled{opacity:.62;cursor:not-allowed}input,select{height:34px;padding:0 10px;width:100%}textarea{min-height:64px;padding:8px 10px;width:100%;resize:vertical}.panel,.device{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);overflow:hidden}.panel{margin-bottom:14px}.panel-head,.device-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line)}.panel-head h2,.device-head h3{margin:0;font-size:16px}.body{padding:14px}.grid{display:grid;grid-template-columns:repeat(4,minmax(140px,1fr));gap:12px}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}.switches{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.switches label{font-size:13px;color:var(--text)}.device{margin-bottom:12px}.interface-box{margin-top:12px;border:1px solid var(--line);border-radius:8px;overflow:hidden}.interface-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;background:#fbfcfe;border-bottom:1px solid var(--line)}.interface-head strong{margin-right:auto}.select-all{display:inline-flex;align-items:center;gap:6px;color:var(--text);font-size:13px;white-space:nowrap}.select-all input{width:16px;height:16px;padding:0}.interfaces{max-height:240px;overflow:auto}.iface{display:grid;grid-template-columns:28px 90px 1fr 80px;gap:8px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line);font-size:13px}.iface:last-child{border-bottom:0}.hint{color:var(--muted);font-size:13px}.status{display:none;margin-top:10px;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--muted);font-size:13px}.status.ok,.status.warn,.status.error{display:block}.status.ok{border-color:rgba(31,157,99,.3);background:#f0fbf6;color:var(--ok)}.status.warn{border-color:rgba(217,152,32,.35);background:#fff8ea;color:var(--warn)}.status.error{border-color:rgba(217,74,74,.32);background:#fff1f1;color:var(--danger)}.ok{color:var(--ok)}.warn{color:var(--warn)}.error{color:var(--danger)}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.iface{grid-template-columns:28px 70px 1fr}}@media(max-width:620px){.toolbar,.panel-head,.device-head,.interface-head{align-items:flex-start;flex-direction:column}.app{padding:12px}.grid{grid-template-columns:1fr}.iface{grid-template-columns:28px 1fr}.iface span:nth-child(2),.iface span:nth-child(4){display:none}}
  </style>
</head>
<body>
<main class="app">
  <header class="toolbar">
    <div class="title"><h1>SNMP 对接设置</h1><small>添加设备、输入团体字、选择 OID、配置监控接口</small></div>
    <div class="actions"><button onclick="location.href='/'">返回看板</button><button id="addBtn">添加设备</button><button class="primary" id="saveBtn">保存配置</button></div>
  </header>
  <section class="panel">
    <div class="panel-head"><h2>全局设置</h2><span class="hint">保存后重启服务生效</span></div>
    <div class="body">
      <div class="grid">
        <div class="field"><label>采集周期（秒）</label><input id="sampleInterval" type="number" min="30"></div>
        <div class="field"><label>原始数据保留天数</label><input id="retentionDays" type="number" min="366"></div>
        <div class="field"><label>SNMP 超时（秒）</label><input id="snmpTimeout" type="number" min="1" step="0.5"></div>
        <div class="field"><label>SNMP 重试次数</label><input id="snmpRetries" type="number" min="0"></div>
      </div>
    </div>
  </section>
  <div id="devices"></div>
  <div id="status" class="status" role="status" aria-live="polite"></div>
</main>
<template id="deviceTemplate">
  <section class="device">
    <div class="device-head"><h3 class="device-title">设备</h3><div class="actions"><button class="discoverBtn">发现接口</button><button class="danger removeBtn">删除</button></div></div>
    <div class="body">
      <div class="grid">
        <div class="field"><label>设备 ID</label><input data-field="id" placeholder="s5700-core-01"></div>
        <div class="field"><label>设备名称</label><input data-field="name" placeholder="S5700-Core-01"></div>
        <div class="field"><label>管理 IP</label><input data-field="host" placeholder="192.168.1.10"></div>
        <div class="field"><label>SNMP 端口</label><input data-field="port" type="number" min="1" value="161"></div>
        <div class="field"><label>SNMP 版本</label><select data-field="snmp_version"><option value="2c">SNMPv2c</option></select></div>
        <div class="field"><label>团体字</label><input data-field="community" type="password" autocomplete="new-password" placeholder="留空表示不修改"></div>
        <div class="field"><label>OID 模板</label><select data-field="oid_profile"><option value="if_mib_64">IF-MIB 64位接口流量</option><option value="if_mib_32">IF-MIB 32位兼容接口流量</option><option value="custom_single">自定义单向 OID</option><option value="custom_dual">自定义双向 OID</option></select></div>
        <div class="field"><label>启用状态</label><select data-field="enabled"><option value="true">启用</option><option value="false">停用</option></select></div>
        <div class="field custom-single-field"><label>自定义 SNMP OID</label><input data-field="custom_oid" placeholder=".1.3.6.1.2.1.31.1.1.1.6.45"></div>
        <div class="field custom-single-field"><label>单向方向</label><select data-field="custom_direction"><option value="in">入方向 / Rx</option><option value="out">出方向 / Tx</option></select></div>
        <div class="field custom-dual-field"><label>入方向 OID / Rx</label><input data-field="custom_in_oid" placeholder=".1.3.6.1.2.1.31.1.1.1.6.45"></div>
        <div class="field custom-dual-field"><label>出方向 OID / Tx</label><input data-field="custom_out_oid" placeholder=".1.3.6.1.2.1.31.1.1.1.10.45"></div>
      </div>
      <div class="interface-box">
        <div class="interface-head"><strong>监控接口</strong><span class="hint">未勾选时默认采集全部接口</span><label class="select-all"><input class="selectAllInterfaces" type="checkbox"> 全选</label></div>
        <div class="interfaces"></div>
      </div>
      <textarea data-field="monitor_interfaces" placeholder="也可以手动填写 ifIndex 或接口名，每行一个。例如：24 或 GigabitEthernet0/0/24"></textarea>
    </div>
  </section>
</template>
<script>
let current={devices:[]};
const profiles={if_mib_64:"IF-MIB 64位接口流量",if_mib_32:"IF-MIB 32位兼容接口流量",custom_single:"自定义单向 OID",custom_dual:"自定义双向 OID"};
function q(root,sel){return root.querySelector(sel)}
function all(root,sel){return Array.from(root.querySelectorAll(sel))}
function setStatus(text,type=""){const el=q(document,"#status");el.className="status "+type;el.textContent=text}
function deviceDefaults(){return{id:"",name:"",host:"",snmp_version:"2c",community:"",port:161,oid_profile:"if_mib_64",custom_oid:"",custom_direction:"in",custom_in_oid:"",custom_out_oid:"",monitor_interfaces:[],enabled:true,mock:false}}
function normalizeDevice(device){return{...deviceDefaults(),...(device||{}),monitor_interfaces:Array.isArray(device?.monitor_interfaces)?device.monitor_interfaces:[]}}
async function loadConfig(){const data=await (await fetch("/api/config")).json();current=data;renderGlobal(data);renderDevices(data.devices||[])}
function renderGlobal(data){q(document,"#sampleInterval").value=data.sample_interval_seconds||30;q(document,"#retentionDays").value=data.retention_days||400;q(document,"#snmpTimeout").value=data.snmp_timeout_seconds||2;q(document,"#snmpRetries").value=data.snmp_retries||1}
function renderDevices(devices){const host=q(document,"#devices");host.innerHTML="";current.devices=(devices||[]).map(normalizeDevice);current.devices.forEach((device,index)=>host.appendChild(renderDevice(device,index)))}
function renderDevice(device,index){device=normalizeDevice(device);current.devices[index]=device;const node=q(document,"#deviceTemplate").content.firstElementChild.cloneNode(true);node.dataset.index=index;q(node,".device-title").textContent=(device.name||device.id||"新设备")+" / "+(profiles[device.oid_profile]||"OID模板");for(const input of all(node,"[data-field]")){const field=input.dataset.field;if(field==="monitor_interfaces"){input.value=(device.monitor_interfaces||[]).join("\n")}else if(input.type==="checkbox"){input.checked=!!device[field]}else{input.value=device[field]??""}}renderInterfaces(node,device.discovered_interfaces||[],device.monitor_interfaces||[]);q(node,".removeBtn").onclick=()=>{current.devices.splice(index,1);renderDevices(current.devices)};q(node,".discoverBtn").onclick=()=>discover(index,node);all(node,"input,select,textarea").forEach(el=>el.addEventListener("input",()=>syncDevice(index,node)));q(node,"[data-field=oid_profile]").addEventListener("change",()=>updateOidFields(node));updateOidFields(node);return node}
function updateOidFields(node){const profile=q(node,"[data-field=oid_profile]").value;all(node,".custom-single-field").forEach(el=>{el.style.display=profile==="custom_single"?"block":"none"});all(node,".custom-dual-field").forEach(el=>{el.style.display=profile==="custom_dual"?"block":"none"})}
function syncDevice(index,node){const d=normalizeDevice(current.devices[index]);for(const input of all(node,"[data-field]")){const field=input.dataset.field;if(field==="monitor_interfaces"){d.monitor_interfaces=input.value.split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean)}else if(input.type==="checkbox"){d[field]=input.checked}else if(field==="port"){d[field]=Number(input.value||161)}else if(field==="enabled"){d[field]=input.value==="true"}else{d[field]=input.value.trim()}}current.devices[index]=d}
function renderInterfaces(node,items,selected){const box=q(node,".interfaces"),selectAll=q(node,".selectAllInterfaces"),textarea=q(node,"[data-field=monitor_interfaces]");function syncChecked(){const checks=Array.from(box.querySelectorAll("input[type=checkbox]"));const values=checks.filter(x=>x.checked).map(x=>x.value);textarea.value=values.join("\n");if(selectAll){selectAll.checked=checks.length>0&&values.length===checks.length;selectAll.indeterminate=values.length>0&&values.length<checks.length}textarea.dispatchEvent(new Event("input",{bubbles:true}))}if(!items.length){box.innerHTML="<div class='iface'><span></span><span></span><span class='hint'>点击“发现接口”读取设备接口，或在下方手动填写。</span><span></span></div>";if(selectAll){selectAll.checked=false;selectAll.indeterminate=false;selectAll.disabled=true}return}if(selectAll)selectAll.disabled=false;const selectedSet=new Set((selected||[]).map(String));box.innerHTML=items.map(item=>{const key=String(item.if_index),checked=selectedSet.size===0||selectedSet.has(key)||selectedSet.has(item.if_name)?"checked":"";return `<label class="iface"><input type="checkbox" value="${key}" ${checked}><span>#${item.if_index}</span><span>${item.if_name}${item.if_alias?" / "+item.if_alias:""}</span><span>${item.oper_status||""}</span></label>`}).join("");box.querySelectorAll("input[type=checkbox]").forEach(cb=>cb.addEventListener("change",syncChecked));if(selectAll){selectAll.onchange=()=>{box.querySelectorAll("input[type=checkbox]").forEach(cb=>{cb.checked=selectAll.checked});syncChecked()}}syncChecked()}
async function discover(index,node){syncDevice(index,node);const device={...current.devices[index],mock:false,demo_discover:false,snmp_timeout_seconds:Number(q(document,"#snmpTimeout").value||2),snmp_retries:Number(q(document,"#snmpRetries").value||1)};setStatus("正在从设备真实发现接口...","warn");try{const r=await fetch("/api/discover",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(device)});const data=await r.json();if(!r.ok||data.ok===false)throw new Error(data.error||"发现失败");current.devices[index].mock=false;current.devices[index].discovered_interfaces=data.interfaces||[];renderInterfaces(node,current.devices[index].discovered_interfaces,current.devices[index].monitor_interfaces||[]);setStatus("真实接口发现完成，共 "+(data.interfaces||[]).length+" 个。保存后看板会联动实际设备。","ok")}catch(err){setStatus("真实接口发现失败："+err.message,"error")}}
function collectConfig(){current.listen_host=location.hostname||current.listen_host||"127.0.0.1";current.listen_port=Number(location.port||current.listen_port||8088);current.sample_interval_seconds=Number(q(document,"#sampleInterval").value||30);current.retention_days=Number(q(document,"#retentionDays").value||400);current.snmp_timeout_seconds=Number(q(document,"#snmpTimeout").value||2);current.snmp_retries=Number(q(document,"#snmpRetries").value||1);current.mock_mode=false;document.querySelectorAll(".device").forEach((node,index)=>syncDevice(index,node));current.devices=(current.devices||[]).map(d=>{const copy=normalizeDevice(d);copy.mock=false;delete copy.discovered_interfaces;delete copy.community_set;return copy});return current}
async function save(){const btn=q(document,"#saveBtn");setStatus("正在保存配置...","warn");btn.disabled=true;try{const payload=collectConfig();const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok||data.ok===false)throw new Error(data.error||"保存失败");const time=new Date().toLocaleTimeString("zh-CN",{hour12:false});setStatus(data.message+"。保存时间："+time+"，可返回看板查看设备状态和接口数据。","ok")}catch(err){setStatus("保存失败："+err.message,"error")}finally{btn.disabled=false}}
q(document,"#addBtn").onclick=()=>{current.devices=current.devices||[];current.devices.push(deviceDefaults());renderDevices(current.devices)};q(document,"#saveBtn").onclick=save;loadConfig().catch(err=>setStatus("加载失败："+err.message,"error"));
</script>
</body>
</html>"""


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>华为交换机接口流量监控</title>
  <style>
    :root{--bg:#f5f7fa;--panel:#fff;--line:#d9e1ea;--text:#26323f;--muted:#667789;--rx:#16b8c8;--tx:#f2a93b;--total:#4d9bd2;--danger:#d94a4a;--ok:#1f9d63;--warn:#d99820;--shadow:0 10px 24px rgba(31,45,61,.08)}
    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif}.app{min-height:100vh;padding:18px}.toolbar{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;margin-bottom:14px}.title{display:flex;align-items:center;gap:10px;min-width:0}.title h1{margin:0;font-size:20px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.title small{display:block;color:var(--muted);font-size:12px;margin-top:4px}.title-icon{width:36px;height:36px;border:1px solid var(--line);border-radius:6px;display:grid;place-items:center;background:#fff;box-shadow:var(--shadow);flex:0 0 auto}.icon{width:18px;height:18px}.actions{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}select,button{height:34px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--text);font-size:13px;padding:0 10px}button{width:36px;padding:0;display:inline-grid;place-items:center;cursor:pointer}button.active{border-color:var(--total);color:var(--total);background:#edf7fd}.summary{display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:10px;margin-bottom:14px}.metric{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);padding:12px;min-height:86px}.metric-head{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px;margin-bottom:10px;gap:8px}.metric strong{display:block;font-size:22px;line-height:1.1;margin-bottom:5px}.metric span{color:var(--muted);font-size:12px}.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px}.ok{color:var(--ok)}.warn{color:var(--warn)}.danger{color:var(--danger)}.layout{display:grid;grid-template-columns:minmax(0,1fr)270px;gap:14px;align-items:start}.chart-panel,.side-panel{background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow)}.chart-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 14px;border-bottom:1px solid var(--line)}.chart-head h2{margin:0;font-size:16px}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:13px;color:var(--muted)}.legend button{width:auto;height:28px;gap:6px;padding:0 8px;display:inline-flex}.legend button.off{opacity:.38}.chart-wrap{position:relative;height:380px;padding:14px}canvas{width:100%;height:100%;display:block}.tooltip{position:absolute;pointer-events:none;background:rgba(38,50,63,.94);color:#fff;padding:8px 10px;border-radius:6px;font-size:12px;line-height:1.6;transform:translate(12px,-50%);display:none;min-width:148px;z-index:2}.data-table{width:100%;border-collapse:collapse;font-size:13px}.data-table th,.data-table td{padding:11px 12px;border-top:1px solid var(--line);text-align:right;white-space:nowrap}.data-table th:first-child,.data-table td:first-child{text-align:left}.data-table th{color:var(--muted);font-weight:600;background:#fbfcfe}.side-panel{overflow:hidden}.side-panel h3{margin:0;padding:12px 14px;font-size:15px;border-bottom:1px solid var(--line)}.events{list-style:none;margin:0;padding:0}.events li{display:grid;grid-template-columns:18px 1fr;gap:8px;padding:11px 14px;border-bottom:1px solid var(--line);font-size:13px}.events small{display:block;color:var(--muted);margin-top:3px;font-size:12px}@media(max-width:980px){.toolbar,.layout{grid-template-columns:1fr}.actions{justify-content:flex-start}.summary{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:620px){.app{padding:12px}.summary{grid-template-columns:1fr}.chart-head{align-items:flex-start;flex-direction:column}.chart-wrap{height:310px}.data-table{display:block;overflow-x:auto}}
  </style>
  <style>
    .range-picker{position:relative;display:inline-block}.range-popup{position:absolute;right:0;top:42px;width:360px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 16px 40px rgba(31,45,61,.16);padding:14px;z-index:20;display:none}.range-popup.open{display:block}.range-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;font-size:14px}.range-head strong{font-size:15px}.range-close{width:28px;height:28px;border:0;color:var(--muted);font-size:18px;background:transparent}.range-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.range-grid button{width:100%;height:38px;border-radius:6px;background:#f3f6f9}.range-grid button.active{background:#35c27f;border-color:#35c27f;color:#fff}.custom-range{display:none;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.custom-range.open{display:grid}.custom-range input{width:100%;height:34px;border:1px solid var(--line);border-radius:6px;padding:0 8px;color:var(--text);font-family:inherit}.custom-range button{grid-column:1/-1;width:100%;height:34px;border-color:var(--total);color:var(--total);background:#f2f9fe}.live-pill{height:34px;display:inline-flex;align-items:center;gap:6px;padding:0 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--muted);font-size:13px}.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(31,157,99,.12)}@media(max-width:620px){.range-popup{left:0;right:auto;width:min(360px,calc(100vw - 24px))}.custom-range{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main class="app">
  <header class="toolbar">
    <div class="title"><div class="title-icon" title="交换机"><svg class="icon" viewBox="0 0 24 24" fill="none"><rect x="3" y="7" width="18" height="10" rx="2" stroke="currentColor" stroke-width="1.8"></rect><path d="M7 11h.01M10.5 11h.01M14 11h.01M17.5 11h.01M7 14h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"></path></svg></div><div><h1>华为交换机接口流量监控</h1><small id="subtitle">正在加载设备...</small></div></div>
    <div class="actions">
      <select id="deviceSelect" aria-label="设备"></select>
      <select id="interfaceSelect" aria-label="接口"></select>
      <span class="live-pill"><span class="pulse"></span><span id="liveText">30秒刷新</span></span>
      <div class="range-picker">
        <button id="rangeBtn" title="时间范围" aria-label="时间范围" style="width:auto;gap:6px;padding:0 10px"><svg class="icon" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8" stroke="currentColor" stroke-width="1.8"></circle><path d="M12 8v5l3 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg><span id="rangeLabel">24小时</span></button>
        <div id="rangePopup" class="range-popup">
          <div class="range-head"><strong>按时间排序</strong><button id="rangeClose" class="range-close" title="关闭" aria-label="关闭">×</button></div>
          <div class="range-grid">
            <button data-range="hour">1小时</button>
            <button data-range="24h" class="active">24小时</button>
            <button data-range="today">今天</button>
            <button data-range="7d">7天</button>
            <button data-range="30d">30天</button>
            <button data-range="90d">90天</button>
            <button data-range="custom">自定义</button>
          </div>
          <div id="customRange" class="custom-range">
            <input id="customStart" type="datetime-local" aria-label="开始时间">
            <input id="customEnd" type="datetime-local" aria-label="结束时间">
            <button id="applyCustom" class="active" style="width:100%">应用</button>
          </div>
        </div>
      </div>
      <button id="areaBtn" class="active" title="面积图" aria-label="面积图"><svg class="icon" viewBox="0 0 24 24" fill="none"><path d="M4 17l5-6 4 4 5-8 2 10H4z" fill="currentColor" opacity=".28"></path><path d="M4 17l5-6 4 4 5-8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg></button>
      <button id="lineBtn" title="折线图" aria-label="折线图"><svg class="icon" viewBox="0 0 24 24" fill="none"><path d="M4 16l5-5 4 3 6-8" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"></path><path d="M4 20h16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".45"></path></svg></button>
      <button id="barBtn" title="柱状图" aria-label="柱状图"><svg class="icon" viewBox="0 0 24 24" fill="none"><path d="M6 19V9M12 19V5M18 19v-7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"></path><path d="M4 19h16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".45"></path></svg></button>
      <button id="refreshBtn" title="刷新" aria-label="刷新"><svg class="icon" viewBox="0 0 24 24" fill="none"><path d="M20 12a8 8 0 1 1-2.34-5.66M20 5v6h-6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg></button>
      <button id="settingsBtn" title="对接设置" aria-label="对接设置"><svg class="icon" viewBox="0 0 24 24" fill="none"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-3-3 2.5-2.5z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path></svg></button>
    </div>
  </header>
  <section class="summary">
    <article class="metric"><div class="metric-head"><span>设备状态</span><span class="ok"><span class="dot" style="background:var(--ok)"></span>在线</span></div><strong id="deviceMetric">0 台</strong><span id="deviceSub">异常 0 台</span></article>
    <article class="metric"><div class="metric-head"><span>接口状态</span><span class="ok"><span class="dot" style="background:var(--ok)"></span>采集</span></div><strong id="ifaceMetric">0 个</strong><span id="ifaceSub">Down 0 个</span></article>
    <article class="metric"><div class="metric-head"><span>当前 Rx</span><span style="color:var(--rx)">入方向</span></div><strong id="rxMetric">0 bps</strong><span id="rxTotalMetric">范围汇总 0 B</span></article>
    <article class="metric"><div class="metric-head"><span>当前 Tx</span><span style="color:var(--tx)">出方向</span></div><strong id="txMetric">0 bps</strong><span id="txTotalMetric">范围汇总 0 B</span></article>
    <article class="metric"><div class="metric-head"><span>总流量</span><span class="warn"><span class="dot" style="background:var(--warn)"></span>95th</span></div><strong id="totalMetric">0 B</strong><span id="p95Metric">95th 0 B</span></article>
  </section>
  <section class="layout">
    <div class="chart-panel">
      <div class="chart-head"><h2>传输字节总数</h2><div class="legend"><button data-series="rx"><span class="dot" style="background:var(--rx)"></span>Rx 字节</button><button data-series="tx"><span class="dot" style="background:var(--tx)"></span>Tx 字节</button><button data-series="total"><span class="dot" style="background:var(--total)"></span>总字节数</button></div></div>
      <div class="chart-wrap"><canvas id="trafficChart"></canvas><div id="tooltip" class="tooltip"></div></div>
      <table class="data-table"><thead><tr><th>指标</th><th>最小</th><th>最大</th><th>平均</th><th>95th PercAvg</th><th>95th PercVal</th></tr></thead><tbody id="statsBody"></tbody></table>
      <div class="chart-head"><h2>每日流量汇总</h2><div class="legend"><span>按当前设备、接口和时间范围汇总</span></div></div>
      <table class="data-table"><thead><tr><th>日期</th><th>Rx 入方向</th><th>Tx 出方向</th><th>合计</th><th>有效样本</th></tr></thead><tbody id="dailyTotalsBody"></tbody></table>
      <div class="chart-head"><h2>范围流量汇总</h2><div class="legend"><span>按所选时间段统计设备和接口总量</span></div></div>
      <table class="data-table"><thead><tr><th>设备</th><th>状态</th><th>Rx 入方向</th><th>Tx 出方向</th><th>合计</th><th>有效样本</th></tr></thead><tbody id="deviceTotalsBody"></tbody></table>
      <table class="data-table"><thead><tr><th>接口</th><th>状态</th><th>Rx 入方向</th><th>Tx 出方向</th><th>合计</th><th>峰值速率</th></tr></thead><tbody id="interfaceTotalsBody"></tbody></table>
    </div>
    <aside class="side-panel"><h3>最近异常事件</h3><ul class="events" id="events"></ul></aside>
  </section>
</main>
<script>
const canvas=document.getElementById("trafficChart"),ctx=canvas.getContext("2d"),tooltip=document.getElementById("tooltip");
const visible={rx:true,tx:true,total:true};let chartMode="area",hoverIndex=-1,points=[],rangeMode="24h",customStart="",customEnd="",refreshTimer=null;
const css=getComputedStyle(document.documentElement),colors={rx:css.getPropertyValue("--rx"),tx:css.getPropertyValue("--tx"),total:css.getPropertyValue("--total")};
async function api(path){const r=await fetch(path);if(!r.ok)throw new Error(await r.text());return r.json()}
function fmtBytes(v){if(v>=1099511627776)return(v/1099511627776).toFixed(3)+" TB";if(v>=1073741824)return(v/1073741824).toFixed(3)+" GB";if(v>=1048576)return(v/1048576).toFixed(2)+" MB";if(v>=1024)return(v/1024).toFixed(2)+" KB";return Math.round(v)+" B"}
function fmtBps(v){if(v>=1e12)return(v/1e12).toFixed(2)+" Tbps";if(v>=1e9)return(v/1e9).toFixed(2)+" Gbps";if(v>=1e6)return(v/1e6).toFixed(2)+" Mbps";if(v>=1e3)return(v/1e3).toFixed(2)+" Kbps";return Math.round(v)+" bps"}
function shortTime(s){const d=new Date(s);return d.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})}
async function boot(){setupRangePicker();const devices=(await api("/api/devices")).devices||[];const ds=document.getElementById("deviceSelect");ds.innerHTML=devices.map(d=>`<option value="${d.id}">${d.name}</option>`).join("");if(!devices.length){document.getElementById("subtitle").textContent="暂无设备，请先进入对接设置添加设备";await refreshOverview();draw();return}await loadInterfaces();await refreshAll();startLiveRefresh()}
async function loadInterfaces(){const deviceId=document.getElementById("deviceSelect").value;if(!deviceId){document.getElementById("interfaceSelect").innerHTML="";document.getElementById("subtitle").textContent="暂无设备";return}const rows=(await api("/api/interfaces?device_id="+encodeURIComponent(deviceId))).interfaces||[];const is=document.getElementById("interfaceSelect");is.innerHTML=rows.map(i=>`<option value="${i.id}">${i.if_name}${i.if_alias?" / "+i.if_alias:""}</option>`).join("");const deviceName=document.getElementById("deviceSelect").selectedOptions[0]?.text||deviceId;document.getElementById("subtitle").textContent=rows[0]?deviceName+" / "+rows[0].if_name:"暂无接口数据"}
async function refreshAll(){await refreshOverview();await refreshTraffic();await refreshEvents()}
async function refreshOverview(){const o=await api("/api/overview");document.getElementById("deviceMetric").textContent=o.devices_online+" / "+o.devices_total+" 台";document.getElementById("deviceSub").textContent="异常 "+Math.max(0,o.devices_total-o.devices_online)+" 台";document.getElementById("ifaceMetric").textContent=o.interfaces_up+" / "+o.interfaces_total+" 个";document.getElementById("ifaceSub").textContent="Down "+Math.max(0,o.interfaces_total-o.interfaces_up)+" 个";document.getElementById("rxMetric").textContent=fmtBps(o.current_in_bps);document.getElementById("txMetric").textContent=fmtBps(o.current_out_bps)}
async function refreshTraffic(){const d=document.getElementById("deviceSelect").value,i=document.getElementById("interfaceSelect").value;if(!d||!i){points=[];renderStats({});renderDailyTotals([]);renderRangeSummary([],[]);draw();return}const query=`device_id=${encodeURIComponent(d)}&interface_id=${encodeURIComponent(i)}${rangeQuery()}`;const data=await api(`/api/traffic?${query}`);points=data.points||[];renderStats(data.stats||{});const totals=await api(`/api/daily_totals?${query}`);renderDailyTotals(totals.days||[]);const summary=await api(`/api/range_summary?device_id=${encodeURIComponent(d)}${rangeQuery()}`);renderRangeSummary(summary.devices||[],summary.interfaces||[]);draw()}
async function refreshEvents(){const rows=(await api("/api/events")).events;document.getElementById("events").innerHTML=rows.map(e=>`<li><span class="${e.severity==="error"?"danger":e.severity==="warn"?"warn":"ok"}">●</span><div>${e.message}<small>${new Date(e.event_time).toLocaleString("zh-CN")}</small></div></li>`).join("")||"<li><span class='ok'>●</span><div>暂无异常事件<small>设备没有新的失败、Down 或恢复提醒</small></div></li>"}
function rangeQuery(){if(rangeMode==="custom"&&customStart&&customEnd)return `&start=${encodeURIComponent(new Date(customStart).toISOString())}&end=${encodeURIComponent(new Date(customEnd).toISOString())}`;return `&period=${encodeURIComponent(rangeMode)}`}
function startLiveRefresh(){if(refreshTimer)clearInterval(refreshTimer);refreshTimer=setInterval(()=>{if(rangeMode!=="custom")refreshAll()},30000)}
function setupRangePicker(){const popup=document.getElementById("rangePopup"),custom=document.getElementById("customRange");document.getElementById("rangeBtn").onclick=()=>popup.classList.toggle("open");document.getElementById("rangeClose").onclick=()=>popup.classList.remove("open");document.querySelectorAll("[data-range]").forEach(btn=>btn.onclick=()=>{rangeMode=btn.dataset.range;document.querySelectorAll("[data-range]").forEach(x=>x.classList.toggle("active",x===btn));custom.classList.toggle("open",rangeMode==="custom");if(rangeMode!=="custom"){document.getElementById("rangeLabel").textContent=btn.textContent;popup.classList.remove("open");refreshAll()}});document.getElementById("applyCustom").onclick=()=>{customStart=document.getElementById("customStart").value;customEnd=document.getElementById("customEnd").value;if(!customStart||!customEnd){return}document.getElementById("rangeLabel").textContent="自定义";popup.classList.remove("open");refreshAll()}}
function renderStats(s){const rows=[["总字节数","total","var(--total)"],["Rx 字节","rx","var(--rx)"],["Tx 字节","tx","var(--tx)"]];const sum=key=>points.filter(p=>p.status==="ok").reduce((total,p)=>total+(Number(p[key])||0),0);const rxTotal=sum("rx"),txTotal=sum("tx"),total=rxTotal+txTotal,p95=(s.total||{}).p95||0;document.getElementById("rxTotalMetric").textContent="范围汇总 "+fmtBytes(rxTotal);document.getElementById("txTotalMetric").textContent="范围汇总 "+fmtBytes(txTotal);document.getElementById("totalMetric").textContent=fmtBytes(total);document.getElementById("p95Metric").textContent="95th "+fmtBytes(p95);document.getElementById("statsBody").innerHTML=rows.map(([label,key,color])=>{const v=s[key]||{};return `<tr><td style="color:${color}">${label}</td><td>${fmtBytes(v.min||0)}</td><td>${fmtBytes(v.max||0)}</td><td>${fmtBytes(v.avg||0)}</td><td>${fmtBytes(v.p95_avg||0)}</td><td>${fmtBytes(v.p95||0)}</td></tr>`}).join("")}
function renderDailyTotals(days){document.getElementById("dailyTotalsBody").innerHTML=(days||[]).map(d=>`<tr><td>${d.day}</td><td>${fmtBytes(d.rx||0)}</td><td>${fmtBytes(d.tx||0)}</td><td>${fmtBytes(d.total||0)}</td><td>${d.sample_count||0}</td></tr>`).join("")||"<tr><td colspan='5'>暂无每日汇总数据</td></tr>"}
function renderRangeSummary(devices,interfaces){document.getElementById("deviceTotalsBody").innerHTML=(devices||[]).map(d=>`<tr><td>${d.name||d.device_id}</td><td>${d.status||"unknown"}</td><td>${fmtBytes(d.rx||0)}</td><td>${fmtBytes(d.tx||0)}</td><td>${fmtBytes(d.total||0)}</td><td>${d.sample_count||0}</td></tr>`).join("")||"<tr><td colspan='6'>暂无设备汇总数据</td></tr>";document.getElementById("interfaceTotalsBody").innerHTML=(interfaces||[]).map(i=>{const peak=Math.max(Number(i.in_max_bps)||0,Number(i.out_max_bps)||0);return `<tr><td>#${i.if_index} ${i.if_name}${i.if_alias?" / "+i.if_alias:""}</td><td>${i.oper_status||"unknown"}</td><td>${fmtBytes(i.rx||0)}</td><td>${fmtBytes(i.tx||0)}</td><td>${fmtBytes(i.total||0)}</td><td>${fmtBps(peak)}</td></tr>`}).join("")||"<tr><td colspan='6'>暂无接口汇总数据</td></tr>"}
function resize(){const r=canvas.getBoundingClientRect(),scale=devicePixelRatio||1;canvas.width=Math.max(1,Math.floor(r.width*scale));canvas.height=Math.max(1,Math.floor(r.height*scale));ctx.setTransform(scale,0,0,scale,0,0);draw()}
function grid(plot,max){ctx.clearRect(0,0,canvas.clientWidth,canvas.clientHeight);ctx.fillStyle="#fff";ctx.fillRect(0,0,canvas.clientWidth,canvas.clientHeight);ctx.strokeStyle="#e5ebf1";ctx.fillStyle="#667789";ctx.font="12px Microsoft YaHei, Segoe UI, Arial";for(let n=0;n<=5;n++){const y=plot.bottom-plot.height*n/5;ctx.beginPath();ctx.moveTo(plot.left,y);ctx.lineTo(plot.right,y);ctx.stroke();ctx.fillText(fmtBytes(max*n/5),6,y+4)}const step=Math.max(1,Math.ceil(points.length/8));points.forEach((p,i)=>{if(i%step!==0&&i!==points.length-1)return;const x=plot.left+plot.width*i/Math.max(1,points.length-1);ctx.fillText(shortTime(p.time),x-16,plot.bottom+24)})}
function xy(plot,max,key){return points.map((p,i)=>({x:plot.left+plot.width*i/Math.max(1,points.length-1),y:plot.bottom-plot.height*(p[key]||0)/max,value:p[key]||0,time:p.time}))}
function drawSeries(plot,max,key,color,fill){if(!visible[key]||!points.length)return;const data=xy(plot,max,key);if(chartMode==="bar"){const bw=Math.max(3,plot.width/Math.max(1,points.length)/4),off=key==="rx"?-bw:key==="tx"?0:bw;ctx.fillStyle=color;ctx.globalAlpha=key==="total"?.32:.72;data.forEach(p=>ctx.fillRect(p.x+off,p.y,bw,plot.bottom-p.y));ctx.globalAlpha=1;return}ctx.beginPath();data.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));if(fill){ctx.lineTo(data[data.length-1].x,plot.bottom);ctx.lineTo(data[0].x,plot.bottom);ctx.closePath();ctx.fillStyle=color;ctx.globalAlpha=.28;ctx.fill();ctx.globalAlpha=1}ctx.beginPath();data.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle=color;ctx.lineWidth=key==="total"?2.2:1.9;ctx.stroke();ctx.fillStyle="#fff";ctx.strokeStyle=color;data.forEach((p,i)=>{if(i%2&&i!==hoverIndex)return;ctx.beginPath();ctx.arc(p.x,p.y,i===hoverIndex?4:2.6,0,Math.PI*2);ctx.fill();ctx.stroke()})}
function drawP95(plot,max){if(!points.length)return;const vals=points.map(p=>p.total||0).sort((a,b)=>a-b),p95=vals[Math.max(0,Math.ceil(vals.length*.95)-1)]||0,y=plot.bottom-plot.height*p95/max;ctx.save();ctx.strokeStyle="#d56b7d";ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(plot.left,y);ctx.lineTo(plot.right,y);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle="#d56b7d";ctx.fillText("95th",plot.left+8,y-6);ctx.restore()}
function draw(){const w=canvas.clientWidth,h=canvas.clientHeight,plot={left:64,right:w-18,top:16,bottom:h-38};plot.width=plot.right-plot.left;plot.height=plot.bottom-plot.top;const raw=Math.max(1,...points.map(p=>p.total||0)),max=Math.ceil(raw/1024/1024)*1024*1024;grid(plot,max);drawP95(plot,max);drawSeries(plot,max,"total",colors.total,chartMode==="area");drawSeries(plot,max,"rx",colors.rx,chartMode==="area");drawSeries(plot,max,"tx",colors.tx,chartMode==="area");if(hoverIndex>=0){const x=plot.left+plot.width*hoverIndex/Math.max(1,points.length-1);ctx.strokeStyle="#9fb4c8";ctx.beginPath();ctx.moveTo(x,plot.top);ctx.lineTo(x,plot.bottom);ctx.stroke()}}
function setMode(m){chartMode=m;["area","line","bar"].forEach(x=>document.getElementById(x+"Btn").classList.toggle("active",x===m));draw()}
document.getElementById("deviceSelect").addEventListener("change",async()=>{await loadInterfaces();await refreshAll()});document.getElementById("interfaceSelect").addEventListener("change",refreshAll);document.getElementById("refreshBtn").addEventListener("click",refreshAll);document.getElementById("settingsBtn").addEventListener("click",()=>{location.href="/settings"});document.getElementById("areaBtn").addEventListener("click",()=>setMode("area"));document.getElementById("lineBtn").addEventListener("click",()=>setMode("line"));document.getElementById("barBtn").addEventListener("click",()=>setMode("bar"));document.querySelectorAll(".legend button").forEach(b=>b.addEventListener("click",()=>{const k=b.dataset.series;visible[k]=!visible[k];b.classList.toggle("off",!visible[k]);draw()}));
canvas.addEventListener("mousemove",e=>{if(!points.length)return;const rect=canvas.getBoundingClientRect(),x=e.clientX-rect.left,plot={left:64,right:canvas.clientWidth-18};hoverIndex=Math.max(0,Math.min(points.length-1,Math.round((x-plot.left)/(plot.right-plot.left)*(points.length-1))));const p=points[hoverIndex];tooltip.style.display="block";tooltip.style.left=Math.min(rect.width-176,Math.max(12,x))+"px";tooltip.style.top=(e.clientY-rect.top)+"px";tooltip.innerHTML=`<strong>${shortTime(p.time)}</strong><br>Rx：${fmtBytes(p.rx||0)}<br>Tx：${fmtBytes(p.tx||0)}<br>总字节数：${fmtBytes(p.total||0)}`;draw()});canvas.addEventListener("mouseleave",()=>{hoverIndex=-1;tooltip.style.display="none";draw()});window.addEventListener("resize",resize);boot().then(resize).catch(err=>{document.getElementById("subtitle").textContent="加载失败："+err.message;resize()});
</script>
</body>
</html>"""
