# AlwaysHdcOn

保持 OpenHarmony/HarmonyOS HDC（TCP）设备长期在线：定时检测设备是否 `Connected`，离线则自动 `tconn`，必要时扫描同 IP 其它端口与局域网候选端口，并按 `UDID` 重新映射 `ip:port`。

本仓库包含脚本与使用文档；设备清单使用 `config/devices.json`（可从 `config/devices.example.json` 复制）。
