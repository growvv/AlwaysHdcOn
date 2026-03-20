# AlwaysHdcOn

用于保持 OpenHarmony/HarmonyOS HDC(TCP) 设备在线的极简守护工具。

核心能力只保留两类：
- 保活：对 `config/devices.json` 中设备周期性执行 `hdc tconn <ip:port>`。
- 扫描发现：
  - 扫描局域网 `:5555`，连接成功且拿到 UDID 后写入 `devices.json`。
  - 对离线设备的 IP 扫端口（先常见端口，再全端口）并尝试恢复。

## 快速开始

```bash
cp config/devices.example.json config/devices.json
python scripts/always-hdc-on.py start
python scripts/always-hdc-on.py status
python scripts/always-hdc-on.py list
python scripts/always-hdc-on.py remove --udid <UDID>
python scripts/always-hdc-on.py stop
```

## 命令

- `start`：后台启动守护进程
- `stop`：停止守护进程
- `status`：查看守护状态（PID、最近轮询、统计）
- `list`：查看 `devices.json` 设备与 online/offline
- `remove`：按 `device_id` 或 `udid` 从 `devices.json` 删除设备

详细参数见 `docs/usage.md`。

## 兼容入口

`python scripts/watch-hdc-devices.py` 仍可运行，但已标记为废弃并转发到新脚本。
