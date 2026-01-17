# AlwaysHdcOn

保持 OpenHarmony/HarmonyOS HDC（TCP）设备长期在线：定时检测设备是否 `Connected`，离线则自动 `tconn`，必要时扫描同 IP 其它端口与局域网候选端口，并按 `UDID` 重新映射 `ip:port`。

脚本会实时维护 `config/devices.json` 中的运行时字段：`online` / `last_online_at` / `last_refresh_at` / `changes`（最近 10 条变更）。

## 前置条件

- 已安装并可直接运行 `hdc`（在 PATH 中）
- 设备已开启 HDC TCP（可手动用 `hdc tconn <ip:port>` 验证）
- 初始化设备清单：复制 `config/devices.example.json` → `config/devices.json` 并填写真实 `device_id` / `udid`
- Python 3.7+（推荐，跨平台）

## 快速开始（Python）

```bash
# 每 10s 检测一次（默认），持续运行
python scripts/watch-hdc-devices.py

# 只跑一次（用于测试）
python scripts/watch-hdc-devices.py --once

# 30s 守护
python scripts/watch-hdc-devices.py --interval 30
```

## PowerShell 版本（可选）

```powershell
# 每 10s 检测一次（默认），持续运行
pwsh .\scripts\watch-hdc-devices.ps1

# 只跑一次
pwsh .\scripts\watch-hdc-devices.ps1 -Once
```

## 文档

- 详细使用说明与参数配置：`docs/usage.md`
