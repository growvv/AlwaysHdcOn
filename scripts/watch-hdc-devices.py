#!/usr/bin/env python3
"""
Watch HDC devices and auto reconnect.

- Reads devices from config/devices.json (JSON list).
- Every N seconds checks `hdc list targets -v`.
- If a device is offline, tries `hdc tconn <ip:port>`.
- If still offline, scans other candidate ports on the known IP (default includes 5555/8710), then scans LAN candidate ports and remaps by UDID.
- Updates config/devices.json runtime fields: online/last_online_at/last_refresh_at + capped changes history.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import IPv4Address, IPv4Network, ip_network
from pathlib import Path
from typing import Iterable, Optional


CHANGES_MAX_ITEMS = 3


def now_iso() -> str:
  return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def log(level: str, message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}][{level}] {message}", flush=True)


def require_hdc() -> None:
  from shutil import which

  if which("hdc") is None:
    raise RuntimeError("hdc not found in PATH. Please install/configure HDC first.")


def run(cmd: list[str], timeout: Optional[float] = None) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
        timeout=timeout,
        check=False,
    )


def read_devices_from_json(path: Path) -> list[dict]:
    if not path.exists():
        raise RuntimeError(f"Devices json not found: {path}")
    try:
        devices = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(devices, list):
        raise RuntimeError(f"Devices json must be a list in: {path}")
    return devices


def write_json_atomic(path: Path, data: object, *, indent: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def normalize_device_for_write(device: dict) -> dict:
    out: dict = {}
    raw_changes = device.get("changes")
    for k, v in device.items():
        if k == "changes":
            continue
        out[k] = v

    changes: list[dict] = []
    if isinstance(raw_changes, list):
        for ev in raw_changes:
            if isinstance(ev, dict):
                changes.append(ev)
    if len(changes) > CHANGES_MAX_ITEMS:
        changes = changes[-CHANGES_MAX_ITEMS:]
    out["changes"] = changes

    return out


def write_devices_json_atomic(path: Path, devices: list[dict]) -> None:
    normalized = [normalize_device_for_write(d) for d in devices]
    write_json_atomic(path, normalized, indent=4)


def ensure_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"online", "on", "true", "1", "yes", "y"}:
            return True
        if v in {"offline", "off", "false", "0", "no", "n"}:
            return False
    return None


def split_ip_port(connect_key: str) -> tuple[Optional[str], Optional[int]]:
    if not connect_key or ":" not in connect_key:
        return None, None
    ip_part, port_part = connect_key.rsplit(":", 1)
    try:
        IPv4Address(ip_part)
    except Exception:
        return None, None
    try:
        port = int(port_part)
    except Exception:
        return ip_part, None
    if port <= 0 or port > 65535:
        return ip_part, None
    return ip_part, port


def ensure_changes_list(device: dict, *, max_items: int = CHANGES_MAX_ITEMS) -> list[dict]:
    changes = device.get("changes")
    if isinstance(changes, list):
        out: list[dict] = []
        for item in changes:
            if isinstance(item, dict):
                out.append(item)
        if len(out) > max_items:
            out = out[-max_items:]
        device["changes"] = out
        return out
    device["changes"] = []
    return device["changes"]


def append_change(device: dict, event: dict, max_items: int = CHANGES_MAX_ITEMS) -> None:
    changes = ensure_changes_list(device, max_items=max_items)
    changes.append(event)
    if len(changes) > max_items:
        device["changes"] = changes[-max_items:]


def record_endpoint_change(device: dict, old: str, new: str, at: str, reason: str) -> None:
    if old == new:
        return
    old_ip, old_port = split_ip_port(old)
    new_ip, new_port = split_ip_port(new)
    what = "endpoint"
    if old_ip != new_ip and old_port != new_port:
        what = "ip+port"
    elif old_ip != new_ip:
        what = "ip"
    elif old_port != new_port:
        what = "port"

    append_change(
        device,
        {
            "at": at,
            "kind": "endpoint_change",
            "what": what,
            "from": old,
            "to": new,
            "reason": reason,
        },
    )


def record_status_change(device: dict, prev_online: Optional[bool], now_online: bool, at: str, reason: str) -> None:
    if prev_online is None or prev_online == now_online:
        return
    append_change(
        device,
        {
            "at": at,
            "kind": "online" if now_online else "offline",
            "reason": reason,
            "device_id": str(device.get("device_id", "")),
        },
    )


@dataclass(frozen=True)
class Target:
    connect_key: str
    transport: str
    status: str


def get_hdc_targets_verbose() -> list[Target]:
    cp = run(["hdc", "list", "targets", "-v"])
    if cp.returncode != 0:
        raise RuntimeError(f"Failed to run 'hdc list targets -v':\n{cp.stdout}")

    targets: list[Target] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        targets.append(Target(connect_key=parts[0], transport=parts[1], status=parts[2]))
    return targets


def connected_targets_set(targets: list[Target]) -> set[str]:
    return {t.connect_key for t in targets if t.status == "Connected"}


def try_hdc_connect(target: str, timeout: float = 15.0) -> tuple[bool, str]:
  cp = run(["hdc", "tconn", target], timeout=timeout)
  out = (cp.stdout or "").strip()
  return (cp.returncode == 0), out


_UDID_RE = re.compile(r"([A-Fa-f0-9]{64})")


def get_hdc_udid(target: str, timeout: float = 8.0) -> Optional[str]:
    cp = run(["hdc", "-t", target, "shell", "bm", "get", "--udid"], timeout=timeout)
    if cp.returncode != 0:
        return None
    m = _UDID_RE.search(cp.stdout or "")
    if not m:
        return None
    return m.group(1).upper()


def uniq_keep_order(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def extract_ipv4(s: str) -> Optional[str]:
    if not s:
        return None
    ip = s.split(":")[0]
    try:
        IPv4Address(ip)
        return ip
    except Exception:
        return None


def guess_local_ipv4() -> Optional[str]:
    # Cross-platform best-effort: UDP "connect" (no packets sent) to infer primary route.
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        if ip and not ip.startswith(("127.", "169.254.")):
            return ip
    except Exception:
        pass

    # Fallback: hostname resolve (often returns loopback on some setups).
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith(("127.", "169.254.")):
            return ip
    except Exception:
        pass
    return None


def candidate_networks(devices: list[dict], max_hosts: int) -> list[IPv4Network]:
    nets: list[IPv4Network] = []
    seen: set[str] = set()

    local_ip = guess_local_ipv4()
    if local_ip:
        net = ip_network(f"{local_ip}/24", strict=False)
        if str(net) not in seen:
            seen.add(str(net))
            nets.append(net)

    for d in devices:
        ip = extract_ipv4(str(d.get("device_id", "")))
        if not ip:
            continue
        net = ip_network(f"{ip}/24", strict=False)
        if str(net) in seen:
            continue
        if net.num_addresses - 2 > max_hosts:
            net = ip_network(f"{ip}/24", strict=False)
        seen.add(str(net))
        nets.append(net)

    return nets


def build_ip_pool(networks: list[IPv4Network], max_total_hosts: int) -> list[str]:
    pool: list[str] = []
    seen: set[str] = set()
    for net in sorted(networks, key=lambda n: str(n)):
        for host in net.hosts():
            ip = str(host)
            if ip in seen:
                continue
            seen.add(ip)
            pool.append(ip)
            if len(pool) >= max_total_hosts:
                return pool
    return pool


def is_port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def find_open_ports_on_host(
    ip: str,
    ports: list[int],
    timeout: float,
    concurrency: int,
) -> list[int]:
    if not ports:
        return []

    open_ports: list[int] = []
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(ports)))) as executor:
        futures = {executor.submit(is_port_open, ip, port, timeout): port for port in ports}
        for future in as_completed(futures):
            port = futures[future]
            try:
                if future.result():
                    open_ports.append(port)
            except Exception:
                continue
    return sorted(set(open_ports))


def find_open_port_hosts(
    ip_addresses: list[str],
    port: int,
    timeout: float,
    concurrency: int,
) -> list[str]:
    if not ip_addresses:
        return []

    open_ips: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {executor.submit(is_port_open, ip, port, timeout): ip for ip in ip_addresses}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                if future.result():
                    open_ips.append(ip)
            except Exception:
                continue
    return sorted(set(open_ips))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"updatedAt": now_iso(), "devices": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state is not an object")
        state.setdefault("devices", {})
        if not isinstance(state["devices"], dict):
            state["devices"] = {}
        return state
    except Exception:
        log("WARN", f"State file is not valid JSON, recreating: {path}")
        return {"updatedAt": now_iso(), "devices": {}}


def save_state(path: Path, state: dict) -> None:
    state["updatedAt"] = now_iso()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_ports(devices: list[dict]) -> list[int]:
    ports: set[int] = set()
    for d in devices:
        did = str(d.get("device_id", ""))
        m = re.search(r":(\d+)$", did)
        if m:
            try:
                ports.add(int(m.group(1)))
            except Exception:
                pass
    if not ports:
        ports.add(5555)
    return sorted(ports)


def collect_candidate_ports(
    devices: list[dict],
    state: dict,
    extra_ports: list[int],
) -> list[int]:
    ports: set[int] = set()
    for p in extra_ports:
        if 0 < p <= 65535:
            ports.add(p)

    for p in parse_ports(devices):
        ports.add(p)

    for entry in (state.get("devices") or {}).values():
        if not isinstance(entry, dict):
            continue
        ip, port = split_ip_port(str(entry.get("device_id", "")))
        if port and 0 < port <= 65535:
            ports.add(port)

    for d in devices:
        for ev in (d.get("changes") or []):
            if not isinstance(ev, dict):
                continue
            for k in ("from", "to", "device_id"):
                ip, port = split_ip_port(str(ev.get(k, "")))
                if port and 0 < port <= 65535:
                    ports.add(port)

    return sorted(ports)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Watch HDC devices and auto reconnect.")
    repo_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--devices-json",
        type=Path,
        default=repo_root / "config" / "devices.json",
        help="Path to config/devices.json",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=repo_root / ".hdc-devices.state.json",
        help="State JSON path (UDID->device_id mapping cache)",
    )
    parser.add_argument("--interval", type=int, default=10, help="Check interval seconds")
    parser.add_argument("--once", action="store_true", help="Run once then exit")
    parser.add_argument(
        "--no-write-config",
        action="store_true",
        help="Do not rewrite devices json when remapping device_id",
    )

    parser.add_argument("--no-lan-scan", action="store_true", help="Disable LAN scan remap")
    parser.add_argument(
        "--no-ip-port-scan",
        action="store_true",
        help="Disable scanning other ports on known device IP when offline",
    )
    parser.add_argument(
        "--extra-ports",
        type=str,
        default="5555,8710",
        help="Extra TCP ports to try/scan (comma separated), e.g. 5555,8710",
    )
    parser.add_argument("--scan-timeout", type=float, default=0.8, help="Port scan timeout seconds")
    parser.add_argument("--scan-concurrency", type=int, default=256, help="Port scan concurrency")
    parser.add_argument("--max-scan-hosts", type=int, default=1024, help="Max hosts to scan per run")

    args = parser.parse_args(argv)

    require_hdc()
    devices_path: Path = args.devices_json

    extra_ports: list[int] = []
    for part in str(args.extra_ports).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = int(part)
        except Exception:
            raise RuntimeError(f"Invalid --extra-ports item: {part!r}")
        if p <= 0 or p > 65535:
            raise RuntimeError(f"Invalid port in --extra-ports: {p}")
        extra_ports.append(p)
    extra_ports = sorted(set(extra_ports))

    state_path: Path = args.state
    state = load_state(state_path)

    devices_cache = read_devices_from_json(devices_path)
    if not devices_cache:
        raise RuntimeError(f"No devices found in {devices_path}")
    devices_mtime_ns_cache = devices_path.stat().st_mtime_ns

    log("INFO", f"Devices: {devices_path} (reloaded each loop)")
    log("INFO", f"State: {state_path}")
    log(
        "INFO",
        f"Interval: {args.interval}s (EnableLanScan={not args.no_lan_scan}, "
        f"EnableIpPortScan={not args.no_ip_port_scan}, Once={args.once})",
    )

    while True:
        loop_now = now_iso()

        try:
            devices_mtime_ns = devices_path.stat().st_mtime_ns
            devices = read_devices_from_json(devices_path)
            if not devices:
                raise RuntimeError(f"No devices found in {devices_path}")
            devices_cache = devices
            devices_mtime_ns_cache = devices_mtime_ns
        except Exception as exc:
            log("ERROR", f"Failed to load devices json: {exc}. Using last known devices.")
            devices = devices_cache
            devices_mtime_ns = devices_mtime_ns_cache

        device_by_udid: dict[str, dict] = {}
        expected_udids: set[str] = set()
        prev_online_by_udid: dict[str, Optional[bool]] = {}
        for d in devices:
            udid = str(d.get("udid", "")).upper().strip()
            if udid:
                device_by_udid[udid] = d
                expected_udids.add(udid)
                prev_online_by_udid[udid] = ensure_bool(d.get("online"))

        ports_all = collect_candidate_ports(devices, state, extra_ports)

        targets = get_hdc_targets_verbose()
        connected = connected_targets_set(targets)

        connected_udid_to_target: dict[str, str] = {}
        for t in sorted(connected):
            ip, port = split_ip_port(t)
            if not ip or not port:
                continue
            u = get_hdc_udid(t, timeout=2.0)
            if u:
                connected_udid_to_target[u] = t

        pending_offline: list[dict] = []
        state_changed = False
        devices_changed = False

        for d in devices:
            prev_online = ensure_bool(d.get("online"))
            d["last_refresh_at"] = loop_now
            devices_changed = True

            old_device_id = str(d.get("device_id", "") or "")
            udid = str(d.get("udid", "")).upper().strip()
            label = " | ".join([str(d.get("type", "")), str(d.get("model", "")), udid]).strip(" |")

            ensure_changes_list(d)

            if udid and udid in connected_udid_to_target:
                target = connected_udid_to_target[udid]
                if old_device_id != target:
                    d["device_id"] = target
                    record_endpoint_change(d, old_device_id, target, loop_now, "already_connected")
                d["online"] = True
                d["last_online_at"] = loop_now
                record_status_change(d, prev_online, True, loop_now, "already_connected")

                state["devices"].setdefault(udid, {})
                if state["devices"][udid].get("device_id") != target:
                    state["devices"][udid]["device_id"] = target
                    state_changed = True
                state["devices"][udid]["lastSeen"] = loop_now

                log("INFO", f"Connected: {label}")
                continue

            known_target: Optional[str] = None
            if udid and isinstance(state.get("devices", {}).get(udid), dict):
                known_target = str(state["devices"][udid].get("device_id") or "") or None

            candidates = uniq_keep_order([known_target or "", old_device_id])

            connected_candidate = next((c for c in candidates if c in connected), None)
            if connected_candidate:
                target = connected_candidate
                log("INFO", f"Connected: {label}")
                if old_device_id != target:
                    d["device_id"] = target
                    record_endpoint_change(d, old_device_id, target, loop_now, "already_connected")
                    old_device_id = target

                if not udid:
                    discovered_udid = get_hdc_udid(target)
                    if discovered_udid:
                        d["udid"] = discovered_udid
                        udid = discovered_udid
                        device_by_udid[udid] = d
                        expected_udids.add(udid)

                d["online"] = True
                d["last_online_at"] = loop_now
                record_status_change(d, prev_online, True, loop_now, "already_connected")

                if udid:
                    state["devices"].setdefault(udid, {})
                    if state["devices"][udid].get("device_id") != target:
                        state["devices"][udid]["device_id"] = target
                        state_changed = True
                    state["devices"][udid]["lastSeen"] = loop_now
                continue

            connected_now_target: Optional[str] = None
            for c in candidates:
                if not c:
                    continue
                log("INFO", f"Try connect: {c}")
                ok, out = try_hdc_connect(c)
                if not ok:
                    if out:
                        log("WARN", f"tconn failed: {c} ({out})")
                    continue
                time.sleep(0.3)
                targets = get_hdc_targets_verbose()
                connected = connected_targets_set(targets)
                if c in connected:
                    connected_now_target = c
                    break

            if connected_now_target:
                target = connected_now_target
                log("INFO", f"Reconnected: {label} -> {target}")
                if old_device_id != target:
                    d["device_id"] = target
                    record_endpoint_change(d, old_device_id, target, loop_now, "tconn")
                    old_device_id = target

                if not udid:
                    discovered_udid = get_hdc_udid(target)
                    if discovered_udid:
                        d["udid"] = discovered_udid
                        udid = discovered_udid
                        device_by_udid[udid] = d
                        expected_udids.add(udid)

                d["online"] = True
                d["last_online_at"] = loop_now
                record_status_change(d, prev_online, True, loop_now, "tconn")

                if udid:
                    state["devices"].setdefault(udid, {})
                    if state["devices"][udid].get("device_id") != target:
                        state["devices"][udid]["device_id"] = target
                        state_changed = True
                    state["devices"][udid]["lastSeen"] = loop_now
                continue

            recovered = False
            if (not args.no_ip_port_scan) and (udid or candidates):
                expected_udid = udid or None
                ips: list[str] = []
                tried_ports: set[int] = set()
                for c in candidates:
                    ip, port = split_ip_port(c)
                    if ip:
                        ips.append(ip)
                    if port:
                        tried_ports.add(port)
                ips = uniq_keep_order(ips)

                ports_to_try = [p for p in ports_all if p not in tried_ports]
                for ip in ips:
                    open_ports = find_open_ports_on_host(
                        ip,
                        ports_to_try,
                        timeout=args.scan_timeout,
                        concurrency=args.scan_concurrency,
                    )
                    for port in open_ports:
                        target = f"{ip}:{port}"
                        if target not in connected:
                            ok, _ = try_hdc_connect(target)
                            if not ok:
                                continue
                            time.sleep(0.3)

                        u = get_hdc_udid(target)
                        if not u:
                            continue
                        if expected_udid and u != expected_udid:
                            continue

                        if not udid:
                            d["udid"] = u
                            udid = u
                            device_by_udid[udid] = d
                            expected_udids.add(udid)

                        if old_device_id != target:
                            d["device_id"] = target
                            record_endpoint_change(d, old_device_id, target, loop_now, "ip_port_scan")
                            old_device_id = target

                        d["online"] = True
                        d["last_online_at"] = loop_now
                        record_status_change(d, prev_online, True, loop_now, "ip_port_scan")

                        state["devices"].setdefault(udid, {})
                        if state["devices"][udid].get("device_id") != target:
                            state["devices"][udid]["device_id"] = target
                            state_changed = True
                        state["devices"][udid]["lastSeen"] = loop_now

                        log("INFO", f"Recovered by ip-port scan: {label} -> {target}")
                        recovered = True
                        break
                    if recovered:
                        break

            if recovered:
                continue

            pending_offline.append(d)
            log("WARN", f"Offline:   {label}")

        if (not args.no_lan_scan) and pending_offline:
            offline_udids = {str(d.get("udid", "")).upper().strip() for d in pending_offline if d.get("udid")}
            offline_udids.discard("")
            if offline_udids:
                log("WARN", f"LAN scan: {len(pending_offline)} device(s) still offline. Scanning...")

                ports = ports_all
                nets = candidate_networks(devices, max_hosts=args.max_scan_hosts)
                ip_pool = build_ip_pool(nets, max_total_hosts=args.max_scan_hosts)

                for port in ports:
                    if not offline_udids:
                        break
                    log(
                        "INFO",
                        f"Scan port {port} on {len(ip_pool)} host(s) "
                        f"(timeout={args.scan_timeout}s, concurrency={args.scan_concurrency})",
                    )
                    open_ips = find_open_port_hosts(
                        ip_pool, port=port, timeout=args.scan_timeout, concurrency=args.scan_concurrency
                    )
                    log("INFO", f"Found {len(open_ips)} host(s) with port {port} open")

                    targets = get_hdc_targets_verbose()
                    connected = connected_targets_set(targets)

                    for ip in open_ips:
                        if not offline_udids:
                            break
                        target = f"{ip}:{port}"

                        if target not in connected:
                            ok, _ = try_hdc_connect(target)
                            if not ok:
                                continue
                            time.sleep(0.3)

                        u = get_hdc_udid(target)
                        if not u or u not in expected_udids or u not in offline_udids:
                            continue

                        cfg_device = device_by_udid.get(u)
                        if not isinstance(cfg_device, dict):
                            continue

                        prev_online = prev_online_by_udid.get(u)
                        old_device_id = str(cfg_device.get("device_id", "") or "")
                        if old_device_id != target:
                            cfg_device["device_id"] = target
                            record_endpoint_change(cfg_device, old_device_id, target, loop_now, "lan_scan")

                        cfg_device["online"] = True
                        cfg_device["last_online_at"] = loop_now
                        record_status_change(cfg_device, prev_online, True, loop_now, "lan_scan")

                        state["devices"].setdefault(u, {})
                        if state["devices"][u].get("device_id") != target:
                            state["devices"][u]["device_id"] = target
                            state_changed = True
                        state["devices"][u]["lastSeen"] = loop_now

                        log("INFO", f"Discovered device: {u} @ {target}")
                        offline_udids.discard(u)

        for d in pending_offline:
            udid = str(d.get("udid", "")).upper().strip()
            if str(d.get("last_online_at", "") or "") == loop_now:
                continue

            if udid:
                st = state.get("devices", {}).get(udid)
                if isinstance(st, dict) and st.get("lastSeen") == loop_now:
                    continue

            prev_online = ensure_bool(d.get("online"))
            d["online"] = False
            record_status_change(d, prev_online, False, loop_now, "unreachable")

        if state_changed:
            save_state(state_path, state)
            log("INFO", f"State updated: {state_path}")

        if devices_changed and not args.no_write_config:
            processed_by_udid: dict[str, dict] = {}
            for d in devices:
                u = str(d.get("udid", "")).upper().strip()
                if u:
                    processed_by_udid[u] = d

            devices_to_write: Optional[list[dict]] = devices
            try:
                current_mtime_ns = devices_path.stat().st_mtime_ns
                if current_mtime_ns != devices_mtime_ns:
                    try:
                        latest_devices = read_devices_from_json(devices_path)
                        for ld in latest_devices:
                            lu = str(ld.get("udid", "")).upper().strip()
                            if not lu or lu not in processed_by_udid:
                                continue
                            src = processed_by_udid[lu]
                            for field in ("device_id", "udid", "online", "last_online_at", "last_refresh_at", "changes"):
                                if field in src:
                                    ld[field] = src[field]
                        devices_to_write = latest_devices
                    except Exception as exc:
                        log(
                            "WARN",
                            f"Devices json changed and reload failed, skipping write this round: {exc}",
                        )
                        devices_to_write = None
            except FileNotFoundError:
                pass

            if devices_to_write is not None:
                write_devices_json_atomic(devices_path, devices_to_write)
                log("INFO", f"Config updated: {devices_path}")

        if args.once:
            return 0
        time.sleep(max(1, int(args.interval)))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        log("INFO", "Interrupted.")
        raise SystemExit(130)
    except Exception as exc:
        log("ERROR", str(exc))
        raise SystemExit(1)
