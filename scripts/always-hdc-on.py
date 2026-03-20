#!/usr/bin/env python3
"""AlwaysHdcOn v2: minimal daemon for HDC keepalive and discovery.

Commands:
- start: run daemon in background
- stop: stop daemon
- status: show daemon status
- list: show configured devices

Core behaviors in daemon loop:
1) Keepalive devices in devices.json by periodic `hdc tconn <ip:port>`.
2) Scan LAN :5555 and try `hdc tconn`; on success+UDID, add/update devices.json.
3) For currently offline devices, scan same IP ports (common ports first, then full 1..65535)
   and try `hdc tconn`; on success+UDID match, update device_id.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network, ip_network
from pathlib import Path
from shutil import which
from typing import Iterable, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: object, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f"{path.name}.tmp-{os.getpid()}"
    if not tmp_name.startswith("."):
        tmp_name = f".{tmp_name}"
    tmp = path.with_name(tmp_name)
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def require_hdc() -> None:
    if which("hdc") is None:
        raise RuntimeError("hdc not found in PATH. Please install/configure HDC first.")


def run_cmd(cmd: list[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def parse_ip_port(target: str) -> tuple[Optional[str], Optional[int]]:
    if not target or ":" not in target:
        return None, None
    ip_s, port_s = target.rsplit(":", 1)
    try:
        IPv4Address(ip_s)
    except Exception:
        return None, None
    try:
        port = int(port_s)
    except Exception:
        return ip_s, None
    if port <= 0 or port > 65535:
        return ip_s, None
    return ip_s, port


def parse_hdc_connected_targets() -> set[str]:
    cp = run_cmd(["hdc", "list", "targets", "-v"], timeout=8.0)
    if cp.returncode != 0:
        return set()
    connected: set[str] = set()
    for raw in cp.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        if parts[2] == "Connected":
            connected.add(parts[0])
    return connected


def try_hdc_tconn(target: str, timeout: float = 10.0) -> tuple[bool, str]:
    cp = run_cmd(["hdc", "tconn", target], timeout=timeout)
    return cp.returncode == 0, (cp.stdout or "").strip()


def get_udid(target: str, timeout: float = 6.0) -> Optional[str]:
    cp = run_cmd(["hdc", "-t", target, "shell", "bm", "get", "--udid"], timeout=timeout)
    if cp.returncode != 0:
        return None
    out = (cp.stdout or "").strip().upper()
    for token in out.split():
        if len(token) == 64 and all(ch in "0123456789ABCDEF" for ch in token):
            return token
    return None


def is_port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def find_open_hosts_for_port(
    ips: list[str],
    port: int,
    *,
    timeout: float,
    concurrency: int,
) -> list[str]:
    if not ips:
        return []
    open_ips: list[str] = []
    max_workers = max(1, min(concurrency, len(ips)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(is_port_open, ip, port, timeout) for ip in ips]
        for ip, fut in zip(ips, futures):
            try:
                if fut.result():
                    open_ips.append(ip)
            except Exception:
                pass
    return open_ips


def normalize_device(device: dict) -> dict:
    out = dict(device)
    out["device_id"] = str(out.get("device_id", "")).strip()
    udid = str(out.get("udid", "")).strip().upper()
    out["udid"] = udid
    return out


def read_devices(path: Path) -> list[dict]:
    if not path.exists():
        raise RuntimeError(f"Devices json not found: {path}")
    raw = read_json_file(path)
    if not isinstance(raw, list):
        raise RuntimeError(f"Devices json must be a list: {path}")
    return [normalize_device(d) for d in raw if isinstance(d, dict)]


def write_devices(path: Path, devices: list[dict]) -> None:
    write_json_atomic(path, devices, indent=4)


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_pid(path: Path) -> Optional[int]:
    try:
        txt = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not txt:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


def read_running_pid(path: Path) -> Optional[int]:
    pid = read_pid(path)
    if pid is None:
        return None
    return pid if is_pid_alive(pid) else None


def save_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class FileLogger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}][{level}] {message}\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)


class DaemonContext:
    def __init__(self, args: argparse.Namespace, logger: FileLogger):
        self.args = args
        self.logger = logger
        self.stop_requested = False
        self.started_at = now_iso()
        self.stats: dict[str, int] = {
            "loops_total": 0,
            "tconn_attempts": 0,
            "tconn_success": 0,
            "offline_ip_scans": 0,
            "offline_recovered": 0,
            "lan_scan_hosts": 0,
            "lan_discovered": 0,
            "last_loop_duration_ms": 0,
        }
        self.last_error = ""

    def write_status(self, *, last_loop_at: Optional[str] = None, running: bool = True) -> None:
        payload = {
            "pid": os.getpid(),
            "running": running,
            "started_at": self.started_at,
            "last_loop_at": last_loop_at,
            "last_error": self.last_error,
            "interval_seconds": self.args.interval,
            "devices_json": str(self.args.devices_json),
            "log_file": str(self.args.log_file),
            "stats": self.stats,
        }
        write_json_atomic(self.args.status_file, payload, indent=2)


def local_candidate_networks(devices: list[dict]) -> list[IPv4Network]:
    networks: list[IPv4Network] = []
    seen: set[str] = set()

    def add_network(ip_s: str) -> None:
        try:
            ip = IPv4Address(ip_s)
        except Exception:
            return
        if ip.is_loopback or ip.is_link_local:
            return
        net = ip_network(f"{ip}/24", strict=False)
        key = str(net)
        if key in seen:
            return
        seen.add(key)
        networks.append(net)

    for d in devices:
        ip, _ = parse_ip_port(str(d.get("device_id", "")))
        if ip:
            add_network(ip)

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            if not info or len(info) < 5:
                continue
            addr = info[4]
            if not addr:
                continue
            add_network(str(addr[0]))
    except Exception:
        pass

    return networks


def build_ip_pool(networks: list[IPv4Network], max_hosts: int) -> list[str]:
    pool: list[str] = []
    seen: set[str] = set()
    for net in sorted(networks, key=lambda n: str(n)):
        for host in net.hosts():
            ip_s = str(host)
            if ip_s in seen:
                continue
            seen.add(ip_s)
            pool.append(ip_s)
            if len(pool) >= max_hosts:
                return pool
    return pool


def ordered_unique(values: Iterable[int]) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for v in values:
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def chunked_range(start: int, end: int, size: int) -> Iterable[list[int]]:
    cur = start
    while cur <= end:
        tail = min(end, cur + size - 1)
        yield list(range(cur, tail + 1))
        cur = tail + 1


def scan_open_ports(ip: str, ports: list[int], *, timeout: float, concurrency: int) -> list[int]:
    if not ports:
        return []
    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(ports)))) as pool:
        in_flight: dict[object, int] = {}
        index = 0

        while index < len(ports) and len(in_flight) < concurrency:
            p = ports[index]
            in_flight[pool.submit(is_port_open, ip, p, timeout)] = p
            index += 1

        while in_flight:
            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                p = in_flight.pop(fut)
                ok = False
                try:
                    ok = bool(fut.result())
                except Exception:
                    ok = False
                if ok:
                    open_ports.append(p)

                if index < len(ports):
                    next_p = ports[index]
                    in_flight[pool.submit(is_port_open, ip, next_p, timeout)] = next_p
                    index += 1

    return open_ports


def try_recover_offline_device(
    device: dict,
    *,
    timeout: float,
    concurrency: int,
    common_ports: list[int],
    logger: FileLogger,
    stats: dict[str, int],
) -> Optional[str]:
    device_id = str(device.get("device_id", ""))
    expected_udid = str(device.get("udid", "")).strip().upper()
    ip, current_port = parse_ip_port(device_id)
    if not ip:
        return None

    ports_first = ordered_unique([*(common_ports or []), current_port or 0])
    ports_first = [p for p in ports_first if 1 <= p <= 65535]

    tried: set[int] = set()

    def try_ports(candidate_ports: list[int], reason: str) -> Optional[str]:
        if not candidate_ports:
            return None
        open_ports = scan_open_ports(ip, candidate_ports, timeout=timeout, concurrency=concurrency)
        for port in open_ports:
            target = f"{ip}:{port}"
            stats["tconn_attempts"] += 1
            ok, _ = try_hdc_tconn(target)
            if ok:
                stats["tconn_success"] += 1
            udid = get_udid(target)
            if not udid:
                continue
            if expected_udid and udid != expected_udid:
                continue
            if not expected_udid:
                device["udid"] = udid
            logger.log("INFO", f"Recovered offline device by {reason}: {device_id} -> {target}")
            return target
        return None

    candidate_first = [p for p in ports_first if p not in tried]
    for p in candidate_first:
        tried.add(p)
    recovered = try_ports(candidate_first, "common-port scan")
    if recovered:
        return recovered

    stats["offline_ip_scans"] += 1
    logger.log("INFO", f"Full port scan on offline device IP: {ip}")
    for batch in chunked_range(1, 65535, 4096):
        batch_ports = [p for p in batch if p not in tried]
        for p in batch_ports:
            tried.add(p)
        recovered = try_ports(batch_ports, "full-port scan")
        if recovered:
            return recovered

    return None


def upsert_by_udid(devices: list[dict], udid: str, target: str, now_at: str) -> bool:
    changed = False
    for d in devices:
        if str(d.get("udid", "")).strip().upper() == udid:
            if str(d.get("device_id", "")) != target:
                d["device_id"] = target
                changed = True
            if d.get("online") is not True:
                d["online"] = True
                changed = True
            if str(d.get("last_seen_at", "")) != now_at:
                d["last_seen_at"] = now_at
                changed = True
            if str(d.get("last_refresh_at", "")) != now_at:
                d["last_refresh_at"] = now_at
                changed = True
            return changed

    devices.append(
        {
            "device_id": target,
            "udid": udid,
            "online": True,
            "last_seen_at": now_at,
            "last_refresh_at": now_at,
        }
    )
    return True


def daemon_loop(ctx: DaemonContext) -> int:
    args = ctx.args
    logger = ctx.logger

    def _signal_handler(_signum: int, _frame: object) -> None:
        ctx.stop_requested = True

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    save_pid(args.pid_file, os.getpid())
    logger.log("INFO", f"Daemon started. pid={os.getpid()} interval={args.interval}s")

    try:
        while not ctx.stop_requested:
            loop_begin = time.time()
            loop_now = now_iso()
            ctx.stats["loops_total"] += 1

            try:
                devices = read_devices(args.devices_json)
                device_changed = False

                # Keepalive pass: periodic tconn on configured device_id.
                for d in devices:
                    if str(d.get("last_refresh_at", "")) != loop_now:
                        d["last_refresh_at"] = loop_now
                        device_changed = True
                    target = str(d.get("device_id", "")).strip()
                    if not target:
                        continue
                    ctx.stats["tconn_attempts"] += 1
                    ok, out = try_hdc_tconn(target)
                    if ok:
                        ctx.stats["tconn_success"] += 1
                    elif out:
                        logger.log("WARN", f"keepalive tconn failed: {target} ({out})")

                connected = parse_hdc_connected_targets()

                offline_devices: list[dict] = []
                for d in devices:
                    target = str(d.get("device_id", "")).strip()
                    if target and target in connected:
                        if d.get("online") is not True:
                            d["online"] = True
                            device_changed = True
                        if str(d.get("last_seen_at", "")) != loop_now:
                            d["last_seen_at"] = loop_now
                            device_changed = True
                    else:
                        if d.get("online") is not False:
                            d["online"] = False
                            device_changed = True
                        offline_devices.append(d)

                # Recover offline devices by scanning all ports on same IP.
                for d in offline_devices:
                    recovered = try_recover_offline_device(
                        d,
                        timeout=args.scan_timeout,
                        concurrency=args.scan_concurrency,
                        common_ports=args.common_ports,
                        logger=logger,
                        stats=ctx.stats,
                    )
                    if not recovered:
                        continue
                    d["device_id"] = recovered
                    d["online"] = True
                    d["last_seen_at"] = loop_now
                    device_changed = True
                    ctx.stats["offline_recovered"] += 1

                # LAN discovery: scan local networks on :5555.
                networks = local_candidate_networks(devices)
                ip_pool = build_ip_pool(networks, args.max_scan_hosts)
                open_ips = find_open_hosts_for_port(
                    ip_pool,
                    5555,
                    timeout=args.scan_timeout,
                    concurrency=args.scan_concurrency,
                )
                ctx.stats["lan_scan_hosts"] += len(ip_pool)

                for ip in open_ips:
                    target = f"{ip}:5555"
                    ctx.stats["tconn_attempts"] += 1
                    ok, _ = try_hdc_tconn(target)
                    if ok:
                        ctx.stats["tconn_success"] += 1
                    udid = get_udid(target)
                    if not udid:
                        continue
                    changed = upsert_by_udid(devices, udid, target, loop_now)
                    if changed:
                        device_changed = True
                        ctx.stats["lan_discovered"] += 1
                        logger.log("INFO", f"Discovered/updated device: {udid} @ {target}")

                if device_changed:
                    write_devices(args.devices_json, devices)

                ctx.last_error = ""

            except Exception as exc:
                ctx.last_error = str(exc)
                logger.log("ERROR", ctx.last_error)

            ctx.stats["last_loop_duration_ms"] = int((time.time() - loop_begin) * 1000)
            ctx.write_status(last_loop_at=loop_now, running=True)

            sleep_s = max(1, int(args.interval))
            for _ in range(sleep_s):
                if ctx.stop_requested:
                    break
                time.sleep(1)
            if args.once:
                break

        return 0
    finally:
        try:
            ctx.write_status(last_loop_at=now_iso(), running=False)
        except Exception:
            pass
        remove_pid(args.pid_file)
        logger.log("INFO", "Daemon stopped.")


def print_device_list(devices: list[dict]) -> None:
    if not devices:
        print("No devices in config.")
        return

    rows: list[tuple[str, str, str, str]] = []
    for d in devices:
        device_id = str(d.get("device_id", "")).strip()
        udid = str(d.get("udid", "")).strip().upper()
        online_v = d.get("online")
        online = "online" if online_v is True else ("offline" if online_v is False else "unknown")
        last_seen = str(d.get("last_seen_at", "") or "-")
        rows.append((online, device_id, udid, last_seen))

    widths = [
        max(len("STATUS"), max(len(r[0]) for r in rows)),
        max(len("DEVICE_ID"), max(len(r[1]) for r in rows)),
        max(len("UDID"), max(len(r[2]) for r in rows)),
        max(len("LAST_SEEN_AT"), max(len(r[3]) for r in rows)),
    ]

    header = (
        f"{'STATUS':<{widths[0]}}  "
        f"{'DEVICE_ID':<{widths[1]}}  "
        f"{'UDID':<{widths[2]}}  "
        f"{'LAST_SEEN_AT':<{widths[3]}}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row[0]:<{widths[0]}}  "
            f"{row[1]:<{widths[1]}}  "
            f"{row[2]:<{widths[2]}}  "
            f"{row[3]:<{widths[3]}}"
        )


def cmd_start(args: argparse.Namespace) -> int:
    require_hdc()
    running = read_running_pid(args.pid_file)
    if running:
        print(f"already running (pid={running})")
        return 0

    script = Path(__file__).resolve()
    cmd = [
        sys.executable,
        str(script),
        "run",
        "--devices-json",
        str(args.devices_json),
        "--pid-file",
        str(args.pid_file),
        "--status-file",
        str(args.status_file),
        "--log-file",
        str(args.log_file),
        "--interval",
        str(args.interval),
        "--scan-timeout",
        str(args.scan_timeout),
        "--scan-concurrency",
        str(args.scan_concurrency),
        "--max-scan-hosts",
        str(args.max_scan_hosts),
        "--common-ports",
        ",".join(str(p) for p in args.common_ports),
    ]

    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)

    # Wait briefly for daemon to write pid/status.
    for _ in range(30):
        time.sleep(0.1)
        pid = read_running_pid(args.pid_file)
        if pid:
            print(f"started (pid={pid})")
            print(f"log: {args.log_file}")
            print(f"status: {args.status_file}")
            return 0
        if proc.poll() is not None:
            break

    status_msg = ""
    if args.status_file.exists():
        try:
            status = read_json_file(args.status_file)
            if isinstance(status, dict):
                status_msg = str(status.get("last_error", ""))
        except Exception:
            pass

    print("failed to start daemon")
    if status_msg:
        print(f"last_error: {status_msg}")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    pid = read_running_pid(args.pid_file)
    if not pid:
        remove_pid(args.pid_file)
        print("not running")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"failed to stop pid={pid}: {exc}")
        return 1

    deadline = time.time() + 10
    while time.time() < deadline:
        if not is_pid_alive(pid):
            remove_pid(args.pid_file)
            print(f"stopped (pid={pid})")
            return 0
        time.sleep(0.2)

    print(f"stop timeout, process still alive (pid={pid})")
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    pid = read_pid(args.pid_file)
    running = pid is not None and is_pid_alive(pid)

    print(f"running: {'yes' if running else 'no'}")
    print(f"pid_file: {args.pid_file}")
    if pid is not None:
        print(f"pid: {pid}")

    if args.status_file.exists():
        try:
            status = read_json_file(args.status_file)
            if isinstance(status, dict):
                print(f"started_at: {status.get('started_at', '-')}")
                print(f"last_loop_at: {status.get('last_loop_at', '-')}")
                print(f"last_error: {status.get('last_error', '') or '-'}")
                stats = status.get("stats")
                if isinstance(stats, dict):
                    print("stats:")
                    for key in sorted(stats.keys()):
                        print(f"  {key}: {stats[key]}")
        except Exception as exc:
            print(f"status parse error: {exc}")
    else:
        print(f"status_file: missing ({args.status_file})")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    devices = read_devices(args.devices_json)
    print_device_list(devices)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    devices = read_devices(args.devices_json)
    before = len(devices)

    if args.udid:
        target_udid = str(args.udid).strip().upper()
        kept = [d for d in devices if str(d.get("udid", "")).strip().upper() != target_udid]
        selector = f"udid={target_udid}"
    else:
        target_device_id = str(args.device_id).strip()
        kept = [d for d in devices if str(d.get("device_id", "")).strip() != target_device_id]
        selector = f"device_id={target_device_id}"

    removed = before - len(kept)
    if removed <= 0:
        print(f"no device matched ({selector})")
        return 0

    write_devices(args.devices_json, kept)
    print(f"removed {removed} device(s) from {args.devices_json}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    require_hdc()
    logger = FileLogger(args.log_file)
    ctx = DaemonContext(args, logger)
    return daemon_loop(ctx)


def parse_ports_csv(text: str) -> list[int]:
    ports: list[int] = []
    for part in (text or "").split(","):
        item = part.strip()
        if not item:
            continue
        try:
            p = int(item)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid port: {item!r}") from exc
        if p <= 0 or p > 65535:
            raise argparse.ArgumentTypeError(f"port out of range: {p}")
        ports.append(p)
    dedup = sorted(set(ports))
    if not dedup:
        dedup = [5555, 8710]
    return dedup


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="AlwaysHdcOn minimal daemon")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--devices-json",
        type=Path,
        default=root / "config" / "devices.json",
        help="Path to devices.json",
    )
    common.add_argument(
        "--pid-file",
        type=Path,
        default=root / ".always-hdc-on.pid",
        help="PID file path",
    )
    common.add_argument(
        "--status-file",
        type=Path,
        default=root / ".always-hdc-on.status.json",
        help="Runtime status file path",
    )
    common.add_argument(
        "--log-file",
        type=Path,
        default=root / "logs" / "always-hdc-on.log",
        help="Daemon log path",
    )
    common.add_argument("--interval", type=int, default=10, help="Loop interval seconds")
    common.add_argument("--scan-timeout", type=float, default=0.8, help="Port scan timeout seconds")
    common.add_argument("--scan-concurrency", type=int, default=256, help="Port scan concurrency")
    common.add_argument("--max-scan-hosts", type=int, default=1024, help="Max hosts for LAN :5555 scan")
    common.add_argument(
        "--common-ports",
        type=parse_ports_csv,
        default=parse_ports_csv("5555,8710"),
        help="Common ports to try before full scan, e.g. 5555,8710",
    )
    common.add_argument("--once", action="store_true", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", parents=[common], help="Start daemon in background")
    sub.add_parser("stop", parents=[common], help="Stop daemon")
    sub.add_parser("status", parents=[common], help="Show daemon status")
    sub.add_parser("list", parents=[common], help="List devices from config")
    p_remove = sub.add_parser("remove", parents=[common], help="Remove device(s) from config")
    group = p_remove.add_mutually_exclusive_group(required=True)
    group.add_argument("--device-id", type=str, help="Remove by exact device_id")
    group.add_argument("--udid", type=str, help="Remove by exact UDID")
    sub.add_parser("run", parents=[common], help="Run daemon in foreground")

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.interval < 1:
        raise RuntimeError("--interval must be >= 1")
    if args.scan_timeout <= 0:
        raise RuntimeError("--scan-timeout must be > 0")
    if args.scan_concurrency < 1:
        raise RuntimeError("--scan-concurrency must be >= 1")
    if args.max_scan_hosts < 1:
        raise RuntimeError("--max-scan-hosts must be >= 1")

    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "remove":
        return cmd_remove(args)
    if args.command == "run":
        return cmd_run(args)

    raise RuntimeError(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
