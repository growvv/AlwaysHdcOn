# AlwaysHdcOn

保持 OpenHarmony/HarmonyOS HDC(TCP) 设备在线的轻量守护脚本。

## 项目结构

- `scripts/watch-hdc-devices.py`：主脚本（推荐，跨平台）
- `scripts/watch-hdc-devices.ps1`：PowerShell 版本（可选）
- `config/devices.example.json`：设备清单模板
- `docs/usage.md`：运行参数与排障
- `docs/devices.md`：设备清单字段说明

## 快速开始

```bash
cp config/devices.example.json config/devices.json
python scripts/watch-hdc-devices.py --once
python scripts/watch-hdc-devices.py
```

## 设计原则

- 明确输入：只依赖 `config/devices.json`、命令行参数与状态文件。
- 明确失败：配置或状态文件格式异常时直接报错，不静默兜底。
- 明确流程：按 `连接检查 -> tconn -> 同IP端口扫描 -> 局域网扫描` 执行。

更多细节见 `docs/usage.md`。
