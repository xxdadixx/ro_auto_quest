import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import json
import logging
import os
import subprocess
import sys
import time
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import psutil

try:
    from supervisor.dxgi_capture import DXGIDesktopDuplication
except ImportError:
    try:
        from .dxgi_capture import DXGIDesktopDuplication
    except ImportError:
        from dxgi_capture import DXGIDesktopDuplication  # type: ignore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("MultiInstanceSupervisor")


@dataclass
class InstanceMetrics:
    instance_index: int
    serial: str
    hwnd: int
    pid: int
    cpu_percent: float = 0.0
    ram_usage_mb: float = 0.0
    vram_usage_mb: float = 0.0
    is_responsive: bool = True
    consecutive_failures: int = 0
    last_frame_timestamp: float = field(default_factory=time.time)


class MultiInstanceSupervisor:
    """
    Asynchronous process supervisor for multi-instance MuMuPlayer 12 execution.
    Combines Direct3D 11 DXGI GPU frame capture, full process-tree CPU/RAM/VRAM
    telemetry, dynamic adb.exe auto-discovery, and circuit-breaker restarts.
    """

    SEARCH_DIRS: List[str] = [
        r"E:\Program Files\Netease\MuMuPlayer",
        r"C:\Program Files\Netease\MuMuPlayer",
        r"C:\Program Files\Nemu\MuMuPlayer-12.0",
        r"D:\Program Files\Netease\MuMuPlayer",
    ]

    DEFAULT_ADB_HOST: str = "127.0.0.1"
    DEFAULT_ADB_PORT: int = 5037

    TARGET_EXECUTABLES: List[str] = [
        "MuMuNxDevice.exe",
        "NemuPlayer.exe",
        "MuMuPlayer.exe",
    ]

    def __init__(
        self,
        mumu_manager_path: Optional[str] = None,
        adb_path: Optional[str] = None,
        adb_server_host: str = DEFAULT_ADB_HOST,
        adb_server_port: int = DEFAULT_ADB_PORT,
        max_consecutive_failures: int = 3,
        capture_timeout_ms: int = 16,
    ) -> None:
        self.adb_server_host: str = adb_server_host
        self.adb_server_port: int = adb_server_port
        self.max_consecutive_failures: int = max_consecutive_failures
        self.capture_timeout_ms: int = capture_timeout_ms

        self.adb_path: Optional[str] = adb_path or self._discover_adb_binary()
        if self.adb_path:
            logger.info(f"ADB Binary Auto-Discovered: '{self.adb_path}'")

        self.mumu_manager_path: Optional[str] = (
            mumu_manager_path or self._discover_mumu_manager()
        )

        self.instances: Dict[int, InstanceMetrics] = {}
        self.dxgi_capturers: Dict[int, DXGIDesktopDuplication] = {}
        self._is_running: bool = False

        self.user32 = ctypes.windll.user32
        self.ensure_adb_daemon_started()

    def _discover_adb_binary(self) -> Optional[str]:
        """Scans system PATH and common installation paths for adb.exe."""
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            candidate = os.path.join(path_dir, "adb.exe")
            if os.path.isfile(candidate):
                return candidate

        for search_root in self.SEARCH_DIRS:
            if os.path.exists(search_root):
                for root, _, files in os.walk(search_root):
                    if "adb.exe" in [f.lower() for f in files]:
                        return os.path.join(root, "adb.exe")
        return None

    def _discover_mumu_manager(self) -> Optional[str]:
        """Scans for MuMuManager.exe binary."""
        for search_root in self.SEARCH_DIRS:
            if os.path.exists(search_root):
                for root, _, files in os.walk(search_root):
                    if "mumumanager.exe" in [f.lower() for f in files]:
                        return os.path.join(root, "MuMuManager.exe")
        return None

    def ensure_adb_daemon_started(self) -> bool:
        """Verifies ADB daemon status and executes adb start-server if needed."""
        try:
            with psutil.socket.socket(psutil.AF_INET, psutil.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect((self.adb_server_host, self.adb_server_port))
                return True
        except Exception:
            logger.warning(
                "ADB Server daemon not responding on port 5037. Starting daemon..."
            )

        if not self.adb_path or not os.path.isfile(self.adb_path):
            logger.error("Cannot start ADB server daemon: 'adb.exe' binary not found.")
            return False

        try:
            cmd = [self.adb_path, "start-server"]
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10.0
            )
            if result.returncode == 0:
                logger.info("ADB Server daemon started successfully.")
                time.sleep(1.0)
                return True
            else:
                logger.error(
                    f"ADB start-server failed: {result.stderr.decode().strip()}"
                )
                return False
        except Exception as err:
            logger.error(f"Failed to launch ADB daemon process: {err}")
            return False

    def _find_hwnds_by_process_names(self) -> List[Tuple[int, int, str]]:
        """Scans process tree for target emulators and maps visible HWNDs."""
        target_pids: Dict[int, str] = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                pname = proc.info["name"]
                if pname and any(
                    target.lower() in pname.lower()
                    for target in self.TARGET_EXECUTABLES
                ):
                    target_pids[proc.info["pid"]] = pname
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not target_pids:
            return []

        found_windows: List[Tuple[int, int, str]] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_callback(hwnd: int, lparam: int) -> bool:
            if self.user32.IsWindowVisible(hwnd):
                pid = ctypes.c_ulong()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in target_pids:
                    rect = wintypes.RECT()
                    self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    width = rect.right - rect.left
                    height = rect.bottom - rect.top

                    if width > 300 and height > 200:
                        length = self.user32.GetWindowTextLengthW(hwnd)
                        title = ""
                        if length > 0:
                            buf = ctypes.create_unicode_buffer(length + 1)
                            self.user32.GetWindowTextW(hwnd, buf, length + 1)
                            title = buf.value
                        found_windows.append((pid.value, hwnd, title))
            return True

        callback = WNDENUMPROC(enum_callback)
        self.user32.EnumWindows(callback, 0)
        return found_windows

    def register_instance(
        self, instance_index: int, serial: str, hwnd_title: str
    ) -> bool:
        """Resolves HWND, maps PID, and initializes DXGI capture per instance."""
        hwnd: int = 0
        resolved_pid: int = 0

        registered_hwnds = {m.hwnd for m in self.instances.values()}
        proc_windows = self._find_hwnds_by_process_names()
        available_proc_windows = [
            item for item in proc_windows if item[1] not in registered_hwnds
        ]

        if instance_index < len(available_proc_windows):
            resolved_pid, hwnd, title = available_proc_windows[instance_index]
            logger.info(
                f"[Idx {instance_index}] Selected distinct HWND {hwnd} (PID {resolved_pid}, Title: '{title}')"
            )
        elif available_proc_windows:
            resolved_pid, hwnd, title = available_proc_windows[0]
            logger.info(
                f"[Idx {instance_index}] Selected fallback HWND {hwnd} (PID {resolved_pid}, Title: '{title}')"
            )

        if not hwnd:
            logger.error(
                f"[Idx {instance_index}] Could not locate available HWND for '{hwnd_title}'."
            )
            return False

        dxgi_engine = DXGIDesktopDuplication()
        if (
            not dxgi_engine.auto_select_by_hwnd(hwnd)
            or not dxgi_engine.initialize_dxgi()
        ):
            logger.error(
                f"[Idx {instance_index}] DXGI capture initialization failed for HWND {hwnd}."
            )
            return False

        metrics = InstanceMetrics(
            instance_index=instance_index, serial=serial, hwnd=hwnd, pid=resolved_pid
        )

        self.instances[instance_index] = metrics
        self.dxgi_capturers[instance_index] = dxgi_engine

        logger.info(
            f"Registered Instance {instance_index} | HWND: {hwnd} | PID: {resolved_pid} | "
            f"Serial: {serial} | Display: '{dxgi_engine.device_name}'"
        )
        return True

    def capture_instance_frame(self, instance_index: int) -> Optional[np.ndarray]:
        """Captures target instance frame buffer directly from GPU VRAM via DXGI."""
        capturer = self.dxgi_capturers.get(instance_index)
        metrics = self.instances.get(instance_index)

        if not capturer or not metrics:
            return None

        frame = capturer.capture_window_vram(
            metrics.hwnd, timeout_ms=self.capture_timeout_ms
        )
        if frame is not None and frame.size > 0:
            metrics.last_frame_timestamp = time.time()
            return frame

        return None

    def get_instance_tree_resources(self, parent_pid: int) -> Tuple[float, float]:
        """Calculates aggregate CPU % and RAM (MB) across process tree."""
        try:
            parent = psutil.Process(parent_pid)
            processes = [parent] + parent.children(recursive=True)

            total_cpu = 0.0
            total_ram_bytes = 0

            for proc in processes:
                try:
                    total_cpu += proc.cpu_percent(interval=None)
                    total_ram_bytes += proc.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            total_ram_mb = total_ram_bytes / (1024.0 * 1024.0)
            return total_cpu, total_ram_mb

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0.0, 0.0

    @staticmethod
    def get_system_vram_usage_mb() -> float:
        """Queries GPU VRAM utilization via nvidia-smi."""
        try:
            cmd = [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
            ]
            output = subprocess.check_output(cmd, timeout=1.0).decode("utf-8").strip()
            return float(output.split("\n")[0])
        except Exception:
            return 0.0

    async def check_adb_health(self, serial: str) -> bool:
        """Verifies ADB daemon responsiveness by attempting transport handshakes."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.adb_server_host, self.adb_server_port),
                timeout=2.0,
            )

            req_cmd = f"host:transport:{serial}"
            formatted_req = f"{len(req_cmd):04x}{req_cmd}".encode("utf-8")
            writer.write(formatted_req)
            await writer.drain()

            status = await reader.readexactly(4)
            writer.close()
            await writer.wait_closed()
            return status == b"OKAY"

        except (ConnectionRefusedError, OSError):
            self.ensure_adb_daemon_started()
            return False
        except Exception:
            return False

    async def restart_instance(self, instance_index: int) -> bool:
        """Executes automated hard restart of a stalled MuMuPlayer instance via CLI."""
        if not self.mumu_manager_path or not os.path.isfile(self.mumu_manager_path):
            logger.error(
                f"[Idx {instance_index}] Cannot restart instance: MuMuManager.exe path invalid."
            )
            return False

        logger.warning(
            f"[Idx {instance_index}] Watchdog threshold triggered. Initiating restart..."
        )

        # Updated MuMuPlayer 12 CLI control syntax
        stop_cmd = [
            self.mumu_manager_path,
            "api",
            "-v",
            str(instance_index),
            "player",
            "close",
        ]
        start_cmd = [
            self.mumu_manager_path,
            "api",
            "-v",
            str(instance_index),
            "player",
            "launch",
        ]

        try:
            proc_stop = await asyncio.create_subprocess_exec(*stop_cmd)
            await proc_stop.wait()
            await asyncio.sleep(3.0)

            proc_start = await asyncio.create_subprocess_exec(*start_cmd)
            await proc_start.wait()
            logger.info(
                f"[Idx {instance_index}] Restart launched. Waiting 15s for emulator boot..."
            )
            await asyncio.sleep(15.0)

            if instance_index in self.instances:
                self.instances[instance_index].consecutive_failures = 0
                self.instances[instance_index].is_responsive = True

            return True

        except Exception as err:
            logger.error(f"[Idx {instance_index}] Instance restart failed: {err}")
            return False

    async def run_supervisor_loop(self, check_interval_seconds: float = 2.0) -> None:
        """Main non-blocking watchdog loop evaluating ADB health and DXGI frame ingestion."""
        self._is_running = True
        logger.info(
            f"Starting MultiInstanceSupervisor loop across {len(self.instances)} registered instances..."
        )

        while self._is_running:
            vram_usage_mb = self.get_system_vram_usage_mb()

            for idx, metrics in list(self.instances.items()):
                cpu, ram_mb = self.get_instance_tree_resources(metrics.pid)
                metrics.cpu_percent = round(cpu, 1)
                metrics.ram_usage_mb = round(ram_mb, 1)
                metrics.vram_usage_mb = vram_usage_mb

                adb_healthy = await self.check_adb_health(metrics.serial)
                frame = self.capture_instance_frame(idx)
                frame_healthy = frame is not None and frame.size > 0

                if adb_healthy and frame_healthy:
                    metrics.consecutive_failures = 0
                    metrics.is_responsive = True
                else:
                    metrics.consecutive_failures += 1
                    metrics.is_responsive = False
                    logger.warning(
                        f"[Idx {idx}] Health check failed ({metrics.consecutive_failures}/{self.max_consecutive_failures}). "
                        f"ADB: {adb_healthy} | DXGI Capture: {frame_healthy}"
                    )

                if metrics.consecutive_failures >= self.max_consecutive_failures:
                    await self.restart_instance(idx)

                logger.info(
                    f"[Idx {idx}] Status: {'OK' if metrics.is_responsive else 'STALLED'} | "
                    f"CPU: {metrics.cpu_percent}% | RAM: {metrics.ram_usage_mb}MB | "
                    f"VRAM Total: {metrics.vram_usage_mb}MB"
                )

            await asyncio.sleep(check_interval_seconds)


if __name__ == "__main__":
    supervisor = MultiInstanceSupervisor()
    supervisor.register_instance(
        instance_index=0, serial="127.0.0.1:16384", hwnd_title="MuMu"
    )

    try:
        asyncio.run(supervisor.run_supervisor_loop(check_interval_seconds=3.0))
    except KeyboardInterrupt:
        logger.info("MultiInstanceSupervisor shut down manually.")
