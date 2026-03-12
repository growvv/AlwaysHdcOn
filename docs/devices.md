# 设备清单说明（`config/devices.json`）

`config/devices.json` 必须是 JSON 数组，每个元素表示一台设备。

## 最小字段

```json
[
  {
    "device_id": "192.168.1.10:5555",
    "udid": "0123456789ABCDEF..."
  }
]
```

## 字段定义

- `device_id`：当前连接地址（`ip:port`）
- `udid`：设备唯一标识（用于 IP/端口变化后的重映射）
- `type` / `model`：可选，仅用于日志展示

## 运行时字段（脚本自动维护）

- `online`：是否在线
- `last_online_at`：最后在线时间（ISO8601）
- `last_refresh_at`：最后刷新时间（ISO8601）
- `changes`：变更记录（最多保留最近 3 条）

## 数据清理建议

- `config/devices.json` 只保留仍在使用的设备。
- 若调试历史过多，可清理各设备的 `changes` 字段。
- 若缓存映射异常，可删除 `.hdc-devices.state.json` 后重跑脚本。
