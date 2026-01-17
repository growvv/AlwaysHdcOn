# 使用文档：设备在线监控与自动重连

本仓库提供两个脚本：

- `scripts/watch-hdc-devices.py`：Python（推荐，跨平台）
- `scripts/watch-hdc-devices.ps1`：PowerShell（Windows/PS7 体验更好）

它们都会：

1. 读取 `config/devices.json` 中的设备清单（JSON list，脚本会每轮重新加载，便于实时维护）
2. 定时检查 `hdc list targets -v` 的 `Connected` 状态
3. 发现离线后先尝试对清单里的 `device_id` 执行 `hdc tconn <ip:port>`
4. 若仍离线：优先对该设备“已知 IP”扫描其它候选端口（默认包含 `5555/8710`），尝试 `tconn` 并通过 `UDID` 确认，从而在你执行了 `tmode port <port>` 后自动找回
5. 若仍离线：扫描局域网候选端口，对扫描到的 `ip:port` 进行 `tconn`，再通过 `bm get --udid` 匹配 `UDID`，从而在设备 IP/端口变化时自动重新映射

> `docs/devices.md` 仅作示例文档，脚本不会修改它。

## 设备清单（config/devices.json）

`config/devices.json` 是一个 JSON 数组，格式类似：

```json
[
  {
    "device_id": "192.168.31.204:5555",
    "type": "phone",
    "model": "HUAWEI Mate 60",
    "udid": "AE95...79E9"
  }
]
```

新环境初始化：

- 复制 `config/devices.example.json` → `config/devices.json`，并填写真实 `device_id` / `udid`

字段含义：

- `device_id`：当前已知的连接地址（`ip:port`）
- `udid`：用于识别设备（IP 变化时靠它重新匹配）
- `type` / `model`：用于日志展示（可选）
- `online`：脚本维护的在线状态（bool）
- `last_online_at`：最后一次确认在线的时间（ISO8601 字符串）
- `last_refresh_at`：最后一次刷新/检测的时间（ISO8601 字符串）
- `changes`：变更历史（数组，最多保留最近 10 条），会记录 IP/端口变化与上下线

常用命令：

```bash
# 查看所有 targets 状态（建议用 -v）
hdc list targets -v

# 主动连接某台设备（TCP）
hdc tconn 192.168.31.204:5555

# 查询设备 UDID（用于填到 config/devices.json）
hdc -t 192.168.31.204:5555 shell bm get --udid
```

## 状态文件（UDID -> device_id 缓存）

脚本会维护一个状态文件（默认：`.hdc-devices.state.json`），用来缓存每个 `UDID` 最近一次确认可用的 `device_id`（可用于排错/加速候选连接）。

- 你可以删除该文件来“清空记忆”，脚本会自动重建。
- 设备清单的 `device_id` 默认会直接写回 `config/devices.json`（可用 `--no-write-config` / `-NoWriteConfig` 关闭写入）。

## Python 脚本（推荐，跨平台）

### 运行方式

```bash
# 默认：每 10s 检测一次，持续运行
python scripts/watch-hdc-devices.py

# 只跑一次（用于验证）
python scripts/watch-hdc-devices.py --once
```

> Linux/macOS 一般使用 `python3`：`python3 scripts/watch-hdc-devices.py`
>
> Python 版本建议 `3.7+`（脚本仅使用标准库）。

### 参数说明

- `--devices-json`：设备清单路径（默认：`config/devices.json`）
- `--state`：状态文件路径（默认：`.hdc-devices.state.json`）
- `--interval`：检测间隔秒数（默认：`10`）
- `--once`：只执行一轮就退出（用于测试）
- `--no-write-config`：不把最新映射写回 `config/devices.json`
- `--no-lan-scan`：关闭局域网扫描（只会对清单里的 `device_id` 做 `tconn`）
- `--no-ip-port-scan`：关闭“同 IP 扫描其它端口”（默认开启）
- `--extra-ports`：额外端口候选（逗号分隔，默认：`5555,8710`；会用于同 IP 扫描与局域网扫描）
- `--scan-timeout`：端口探测超时时间（秒，默认：`0.8`）
- `--scan-concurrency`：端口探测并发数（默认：`256`）
- `--max-scan-hosts`：最多扫描多少个 IP（默认：`1024`）

### 典型配置示例

```bash
# 每 5 秒检测一次
python scripts/watch-hdc-devices.py --interval 5

# 不扫描局域网（只按清单 IP 重连）
python scripts/watch-hdc-devices.py --no-lan-scan

# 不写回 config（只做监控/重连）
python scripts/watch-hdc-devices.py --no-write-config

# 使用 30s 守护间隔
python scripts/watch-hdc-devices.py --interval 30

# 增加/覆盖候选端口（例如你把设备改到了 8710 或其它端口）
python scripts/watch-hdc-devices.py --extra-ports 5555,8710,12345

# 网络较慢/较大时：降低并发、增大超时、限制扫描 IP 数量
python scripts/watch-hdc-devices.py --scan-concurrency 64 --scan-timeout 1.2 --max-scan-hosts 256
```

## PowerShell 脚本（可选）

### 运行方式

```powershell
# 默认：每 10s 检测一次（默认），持续运行
pwsh .\scripts\watch-hdc-devices.ps1

# 只跑一次
pwsh .\scripts\watch-hdc-devices.ps1 -Once
```

### 参数说明

- `-DevicesJsonPath`：设备清单路径（默认：`config/devices.json`）
- `-StatePath`：状态文件路径（默认：`.hdc-devices.state.json`）
- `-IntervalSeconds`：检测间隔秒数（默认：`10`）
- `-Once`：只执行一轮就退出
- `-EnableLanScan:$false`：关闭局域网扫描（默认开启）
- `-EnableIpPortScan:$false`：关闭“同 IP 扫描其它端口”（默认开启）
- `-ExtraPorts`：额外端口候选（默认：`5555,8710`；会用于同 IP 扫描与局域网扫描）
- `-ScanTimeoutMs`：端口探测超时（毫秒，默认：`800`）
- `-ScanThrottle`：端口探测并发数（默认：`128`）
- `-MaxScanHosts`：最大扫描 IP 数量（默认：`1024`）
- `-NoWriteConfig`：不把最新映射写回 `config/devices.json`

### 典型配置示例

```powershell
# 更快检测
pwsh .\scripts\watch-hdc-devices.ps1 -IntervalSeconds 5

# 关闭局域网扫描
pwsh .\scripts\watch-hdc-devices.ps1 -EnableLanScan:$false

# 不写回 config（只做监控/重连）
pwsh .\scripts\watch-hdc-devices.ps1 -NoWriteConfig

# 使用 30s 守护间隔
pwsh .\scripts\watch-hdc-devices.ps1 -IntervalSeconds 30

# 增加/覆盖候选端口
pwsh .\scripts\watch-hdc-devices.ps1 -ExtraPorts 5555,8710,12345

# 调整扫描性能
pwsh .\scripts\watch-hdc-devices.ps1 -ScanThrottle 64 -ScanTimeoutMs 1200 -MaxScanHosts 256
```

## 注意事项 / 排错

- 局域网扫描会对大量 IP 做端口探测与（少量）`tconn` 尝试；在网段很大时建议降低并发或关闭扫描。
- 若 `tconn` 一直失败：检查设备是否开启 TCP 监听、端口是否被防火墙拦截、以及设备/电脑是否在同一局域网。
- `tmode port <port>` 会触发设备侧 daemon 重启并切换 TCP 端口：旧的 `ip:oldport` 会“消失”，需要 `hdc tconn ip:newport` 或让脚本通过端口扫描找回。
- 重新映射依赖 `UDID`，请确保 `config/devices.json` 中 `udid` 正确。
