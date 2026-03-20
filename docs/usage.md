# 使用说明

## 1) 初始化

```bash
cp config/devices.example.json config/devices.json
```

`devices.json` 需要至少包含 `device_id` 和 `udid`。

## 2) 命令

```bash
# 后台启动
python scripts/always-hdc-on.py start

# 查看守护状态
python scripts/always-hdc-on.py status

# 查看设备清单状态
python scripts/always-hdc-on.py list

# 按 UDID 删除设备
python scripts/always-hdc-on.py remove --udid <UDID>

# 按 DEVICE_ID 删除设备
python scripts/always-hdc-on.py remove --device-id <IP:PORT>

# 停止守护
python scripts/always-hdc-on.py stop
```

## 3) 常用参数（可用于 start/run/status/list/remove）

- `--devices-json`：设备清单路径（默认 `config/devices.json`）
- `--pid-file`：PID 文件路径（默认 `.always-hdc-on.pid`）
- `--status-file`：状态文件路径（默认 `.always-hdc-on.status.json`）
- `--log-file`：日志路径（默认 `logs/always-hdc-on.log`）
- `--interval`：轮询间隔秒数（默认 `10`）
- `--scan-timeout`：端口探测超时秒数（默认 `0.8`）
- `--scan-concurrency`：端口探测并发（默认 `256`）
- `--max-scan-hosts`：单轮 LAN `:5555` 最多扫描主机数（默认 `1024`）
- `--common-ports`：离线设备优先尝试端口（默认 `5555,8710`）

## 4) 运行流程（守护循环）

1. 读取 `devices.json`
2. 对每个设备执行保活 `hdc tconn <device_id>`
3. 查询 `hdc list targets -v` 更新 online/offline
4. 对 offline 设备执行同 IP 端口恢复：
   - 先扫 `--common-ports`
   - 再扫 `1..65535`
5. 扫描 LAN `:5555`，连接成功且拿到 UDID 后按 UDID 入库/更新
6. 回写 `devices.json` 并更新状态文件

## 5) 常用排查

```bash
hdc list targets -v
hdc tconn <ip:port>
hdc -t <ip:port> shell bm get --udid
```

查看日志：`logs/always-hdc-on.log`
