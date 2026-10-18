"""Host resource sampling (CPU, RAM, disk, GPU) and admission checks for new workers."""
from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import psutil

log = logging.getLogger("agentloop")

SAMPLE_S = 2  # CPU/RAM/disk sample period, which is also how often RAM and VRAM grants are policed
# Each nvidia-smi call is a round trip to the Windows GPU driver under WSL; polled every second, the calls piled
# up as stalls while CUDA jobs ran on the GPU that also drives the display, so the GPU is read less often
GPU_POLL_S = 10
GPU_STALE_S = 2 * GPU_POLL_S + 5  # a GPU reading older than this is shown as stale and stops no job


def gpus() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.used,memory.total,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    rows = []
    for line in out.stdout.strip().splitlines():
        idx, uuid, name, used, total, util = [x.strip() for x in line.split(",")]
        rows.append({"index": int(idx), "uuid": uuid, "name": name, "mem_used_gb": round(float(used) / 1024, 1),
                     "mem_total_gb": round(float(total) / 1024, 1), "util": float(util), "procs": {}})
    # VRAM per process tells which `loop run` job holds what; WSL lists no processes, so `procs` stays empty there
    apps = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_memory",
                           "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
    by_uuid = {g["uuid"]: g for g in rows}
    for line in apps.stdout.strip().splitlines():
        try:
            uuid, pid, used = [x.strip() for x in line.split(",")]
            by_uuid[uuid]["procs"][int(pid)] = round(float(used) / 1024, 2)
        except (KeyError, ValueError):  # an unknown GPU or "[N/A]" memory
            continue
    return rows


def tree_rss(pid: int) -> int:
    try:
        proc = psutil.Process(pid)
        return sum(p.memory_info().rss for p in [proc, *proc.children(recursive=True)])
    except psutil.Error:
        return 0


def cgroup_dir(pid: int) -> Path | None:
    """The cgroup v2 directory a process runs in (its turn's scope), if the memory files are readable there."""
    try:
        rel = Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
    except (OSError, IndexError):
        return None
    path = Path("/sys/fs/cgroup" + rel)
    return path if (path / "memory.stat").exists() else None


def mark_oom_first(path: Path, keep: set[int]) -> None:
    """Put every process of a turn's scope but `keep` (the CLI and its `loop run` helpers) first in line for the
    OOM killer.

    The CLI, the helpers and their jobs (which run in scopes of their own) keep the default score, so when the
    pool runs out the kernel kills a tool process that holds no RAM grant, not an agent or a job that keeps to its
    grant."""
    try:
        pids = (path / "cgroup.procs").read_text().split()
    except OSError:
        return
    for pid in pids:
        if int(pid) in keep:
            continue
        adj = Path(f"/proc/{pid}/oom_score_adj")
        try:
            if adj.read_text().strip() != "1000":
                adj.write_text("1000")
        except OSError:  # the process has exited
            pass


def scope_memory(path: Path) -> tuple[int, int] | None:
    """Anonymous memory and OOM kills of one turn's scope.

    Anonymous memory is what the processes really hold (page cache is reclaimable and not counted), and it
    includes jobs the agent detached from the CLI's process tree."""
    try:
        anon = next(int(line.split()[1]) for line in (path / "memory.stat").read_text().splitlines()
                    if line.startswith("anon "))
        events = dict(line.split() for line in (path / "memory.events").read_text().splitlines())
        return anon, int(events.get("oom_kill", 0))
    except (OSError, StopIteration, ValueError):
        return None


def wsl_host_mount() -> str | None:
    """The Windows drive (as its /mnt path) that holds this WSL distro's virtual disk, or None outside WSL.

    The distro's ext4.vhdx reports its maximum size (1 TB by default), but it can only grow as far as the free
    space of the Windows drive it lives on, so that drive bounds the real free space."""
    if "microsoft" not in os.uname().release.lower():
        return None
    # A server started as a service or from a stripped environment may lack WSL_DISTRO_NAME, and Windows' dirs in
    # PATH (also when WSL's appendWindowsPath is off); it then takes the default distro, the usual one to run in
    distro = os.environ.get("WSL_DISTRO_NAME")
    reg = shutil.which("reg.exe") or shutil.which("reg.exe", path="/mnt/c/Windows/System32")
    if not reg:
        return None
    try:
        out = subprocess.run([reg, "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss", "/s"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    default = re.search(r"DefaultDistribution\s+REG_SZ\s+(\{[^}]+\})", out)
    # One registry key per distro; pick the BasePath of the key whose DistributionName is ours.
    for block in re.split(r"\r?\n(?=HKEY_)", out):
        values = dict(re.findall(r"^\s+(\w+)\s+REG_\w+\s+(.*?)\s*$", block, re.M))
        drive = re.match(r"(?:\\\\\?\\)?([A-Za-z]):", values.get("BasePath", ""))
        ours = values.get("DistributionName") == distro if distro else \
            bool(default) and block.split("\n", 1)[0].rstrip().endswith(default.group(1))
        if ours and drive:
            mount = f"/mnt/{drive.group(1).lower()}"
            return mount if os.path.ismount(mount) else None
    return None


class Infra:
    def __init__(self, disk_path: str):
        self.disk_path = disk_path
        self.host_mount = wsl_host_mount()
        self.history: deque = deque(maxlen=600 // SAMPLE_S)  # the last 10 minutes
        psutil.cpu_percent()
        # nvidia-smi can hang for a minute when WSL's GPU channel stalls; it is polled on its own thread so the
        # 2 s CPU/RAM/disk sample never waits on it and carries the last GPU reading with its time instead
        self.gpu_reading: tuple[float, list[dict]] = (0.0, [])
        self.poll_gpus()
        if shutil.which("nvidia-smi"):
            threading.Thread(target=self.gpu_loop, name="gpu-poll", daemon=True).start()

    def poll_gpus(self) -> None:
        try:
            self.gpu_reading = (time.time(), gpus())
        except (OSError, subprocess.SubprocessError, ValueError):
            log.warning("nvidia-smi failed; keeping the GPU reading from %.0f s ago",
                        time.time() - self.gpu_reading[0], exc_info=True)

    def gpu_loop(self) -> None:
        while True:
            self.poll_gpus()
            time.sleep(GPU_POLL_S)

    def sample(self) -> dict:
        vm, disk = psutil.virtual_memory(), psutil.disk_usage(self.disk_path)
        free, total = disk.free, disk.total
        if self.host_mount:  # WSL: the virtual disk can grow only into the host drive's free space
            free = min(free, psutil.disk_usage(self.host_mount).free)
            total = min(total, disk.used + free)
        snap = {
            "ts": time.time(), "cpu_pct": psutil.cpu_percent(), "cpus": psutil.cpu_count(),
            "ram_total_gb": round(vm.total / 2**30, 1), "ram_free_gb": round(vm.available / 2**30, 1),
            "ram_pct": vm.percent, "swap_pct": psutil.swap_memory().percent,
            "disk_total_gb": round(total / 2**30), "disk_free_gb": round(free / 2**30),
            # Each sample gets its own copy: the engine adds per-sample fields (jobs_gb) to these dicts
            "gpus": copy.deepcopy(self.gpu_reading[1]), "gpus_ts": self.gpu_reading[0],
        }
        self.history.append(snap)
        return snap

    def latest(self) -> dict:
        return self.history[-1] if self.history else self.sample()

    def summary(self) -> str:
        s = self.latest()
        gpu = "; ".join(f"GPU{g['index']} {g['name']} {g['mem_used_gb']}/{g['mem_total_gb']} GB, {g['util']:.0f}%"
                        for g in s["gpus"]) or "none"
        return (f"CPU {s['cpu_pct']:.0f}% of {s['cpus']} cores · "
                f"RAM {s['ram_free_gb']}/{s['ram_total_gb']} GB free · disk {s['disk_free_gb']} GB free · GPU {gpu}")
