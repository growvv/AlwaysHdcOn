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
- `udid`：设备唯一标识（主去重键）
- `type` / `model`：可选，仅展示用途

## 运行时字段（脚本维护）

- `online`：当前轮询是否在线
- `last_seen_at`：最近确认在线时间（ISO8601）
- `last_refresh_at`：最近轮询刷新时间（ISO8601）

## 入库规则

- LAN 扫描仅在成功获取 `udid` 时写入 `devices.json`。
- 发现已存在 `udid` 时更新其 `device_id`；不存在时新增。
