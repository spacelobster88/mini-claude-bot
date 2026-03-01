"""Collect macOS system metrics via shell commands."""
import re
import subprocess


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()


def _parse_size_gb(s: str) -> float:
    """Parse df -h size strings like '228Gi', '15Gi', '69Gi'."""
    s = s.strip()
    if s.endswith("Ti"):
        return round(float(s[:-2]) * 1024, 1)
    if s.endswith("Gi"):
        return round(float(s[:-2]), 1)
    if s.endswith("Mi"):
        return round(float(s[:-2]) / 1024, 1)
    # Fallback
    return 0.0


def collect() -> dict:
    metrics: dict = {}

    # CPU brand
    metrics["cpu"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"])

    # Total RAM
    mem_bytes = int(_run(["sysctl", "-n", "hw.memsize"]))
    metrics["memory_total_gb"] = round(mem_bytes / (1024**3), 1)

    # CPU + memory from top
    top_out = _run(["top", "-l", "1", "-n", "0", "-s", "0"])

    cpu_m = re.search(r"CPU usage:\s+([\d.]+)% user,\s+([\d.]+)% sys,\s+([\d.]+)% idle", top_out)
    if cpu_m:
        metrics["cpu_usage_percent"] = round(float(cpu_m[1]) + float(cpu_m[2]), 1)
        metrics["cpu_idle_percent"] = float(cpu_m[3])

    mem_m = re.search(r"PhysMem:\s+([\d.]+)([GM]) used.*?([\d.]+)([GM]) unused", top_out)
    if mem_m:
        used = float(mem_m[1]) if mem_m[2] == "G" else round(float(mem_m[1]) / 1024, 1)
        free = float(mem_m[3]) if mem_m[4] == "G" else round(float(mem_m[3]) / 1024, 1)
        metrics["memory_used_gb"] = used
        metrics["memory_free_gb"] = free

    load_m = re.search(r"Load Avg:\s+([\d.]+),\s+([\d.]+),\s+([\d.]+)", top_out)
    if load_m:
        metrics["load_avg"] = [float(load_m[i]) for i in range(1, 4)]

    # Disk
    df_out = _run(["df", "-h", "/"])
    df_line = df_out.strip().split("\n")[-1].split()
    metrics["disk_total_gb"] = _parse_size_gb(df_line[1])
    metrics["disk_used_gb"] = _parse_size_gb(df_line[2])
    metrics["disk_free_gb"] = _parse_size_gb(df_line[3])
    metrics["disk_used_percent"] = int(df_line[4].rstrip("%"))

    # Uptime
    uptime_out = _run(["uptime"])
    up_m = re.search(r"up\s+(.+?),\s+\d+ users?", uptime_out)
    if up_m:
        metrics["uptime"] = up_m[1].strip()

    # Hostname
    metrics["hostname"] = _run(["hostname"])

    return metrics
