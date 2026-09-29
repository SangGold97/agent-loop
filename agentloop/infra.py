"""Host resource sampling (CPU, RAM, disk, GPU) and admission checks for new workers."""
from __future__ import annotations

import shutil
import subprocess
import time
from collections import deque

import psutil


def gpus() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    rows = []
    for line in out.stdout.strip().splitlines():
        idx, name, used, total, util = [x.strip() for x in line.split(",")]
        rows.append({"index": int(idx), "name": name, "mem_used_gb": round(float(used) / 1024, 1),
                     "mem_total_gb": round(float(total) / 1024, 1), "util": float(util)})
    return rows


def tree_rss(pid: int) -> int:
    try:
        proc = psutil.Process(pid)
        return sum(p.memory_info().rss for p in [proc, *proc.children(recursive=True)])
    except psutil.Error:
        return 0


class Infra:
    def __init__(self, disk_path: str):
        self.disk_path = disk_path
        self.history: deque = deque(maxlen=600)
        psutil.cpu_percent()

    def sample(self) -> dict:
        vm, disk = psutil.virtual_memory(), psutil.disk_usage(self.disk_path)
        snap = {
            "ts": time.time(), "cpu_pct": psutil.cpu_percent(), "cpus": psutil.cpu_count(),
            "load": [round(x, 2) for x in psutil.getloadavg()],
            "ram_total_gb": round(vm.total / 2**30, 1), "ram_free_gb": round(vm.available / 2**30, 1),
            "ram_pct": vm.percent, "swap_pct": psutil.swap_memory().percent,
            "disk_total_gb": round(disk.total / 2**30), "disk_free_gb": round(disk.free / 2**30),
            "gpus": gpus(),
        }
        self.history.append(snap)
        return snap

    def latest(self) -> dict:
        return self.history[-1] if self.history else self.sample()

    def summary(self) -> str:
        s = self.latest()
        gpu = "; ".join(f"GPU{g['index']} {g['name']} {g['mem_used_gb']}/{g['mem_total_gb']} GB, {g['util']:.0f}%"
                        for g in s["gpus"]) or "none"
        return (f"CPU {s['cpu_pct']:.0f}% of {s['cpus']} cores (load {s['load'][0]}) · "
                f"RAM {s['ram_free_gb']}/{s['ram_total_gb']} GB free · disk {s['disk_free_gb']} GB free · GPU {gpu}")
