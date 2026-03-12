# 使用说明

## 1) 初始化

```bash
cp config/devices.example.json config/devices.json
```

将示例中的 `device_id`、`udid` 替换为真实值。

## 2) 运行

```bash
# 单轮验证
python scripts/watch-hdc-devices.py --once

# 持续守护（默认 10 秒轮询）
python scripts/watch-hdc-devices.py
```

PowerShell（可选）：

```powershell
pwsh .\scripts\watch-hdc-devices.ps1 -Once
pwsh .\scripts\watch-hdc-devices.ps1
```

## 3) 主要参数（Python）

- `--devices-json`：设备清单路径（默认 `config/devices.json`）
- `--state`：状态文件路径（默认 `.hdc-devices.state.json`）
- `--interval`：轮询间隔秒数（默认 `10`）
- `--once`：只执行一轮
- `--no-write-config`：不回写 `config/devices.json`
- `--no-ip-port-scan`：关闭同 IP 端口扫描
- `--no-lan-scan`：关闭局域网扫描
- `--extra-ports`：额外端口（默认 `5555,8710`）
- `--scan-timeout`：端口探测超时秒数（默认 `0.8`）
- `--scan-concurrency`：端口探测并发（默认 `256`）
- `--max-scan-hosts`：单次最大扫描 IP 数（默认 `1024`）

## 4) 流程

1. 读取 `devices.json`
2. 查询 `hdc list targets -v`
3. 对离线设备尝试 `hdc tconn`
4. 可选：同 IP 的候选端口扫描
5. 可选：局域网候选端口扫描
6. 回写 `devices.json` 与状态文件

## 5) 常用排查

```bash
hdc list targets -v
hdc tconn <ip:port>
hdc -t <ip:port> shell bm get --udid
```

如果状态缓存异常，删除 `.hdc-devices.state.json` 后重试。
