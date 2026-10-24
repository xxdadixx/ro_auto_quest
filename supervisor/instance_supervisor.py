import asyncio
import ctypes
from dataclasses import dataclass, field
import json
import logging
import os
import subprocess
import time
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import psutil
import win32con
import win32gui
import win32ui

# Configure logging for supervisor service
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("MuMuInstanceSupervisor")


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


class MuMuInstanceSupervisor:
    """
    Multi-instance process supervisor for MuMuPlayer 12.
    Monitors process tree resource usage (CPU, RAM, GPU VRAM), captures window frames 
    at 60+ FPS via Win32 GDI/BitBlt, and executes automatic instance restarts using MuMuManager CLI.
    """

    DEFAULT_MUMU_PATH: str = r"C:\Program Files\Nemu\MuMuPlayer-12.0\shell\MuMuManager.exe"
    REFERENCE_WIDTH: int = 1280
    REFERENCE_HEIGHT: int = 720

    def __init__(
        self,
        mumu_manager_path: str = DEFAULT_MUMU_PATH,
        adb_server_host: str = "127.0.0.1",
        adb_server_port: int = 5037,
        max_consecutive_failures: int = 3,
        vram_limit_mb: float = 4096.0
    ) -> None:
        self.mumu_manager_path: str = mumu_manager_path
        self.adb_server_host: str = adb_server_host
        self.adb_server_port: int = adb_server_port
        self.max_consecutive_failures: int = max_consecutive_failures
        self.vram_limit_mb: float = vram_limit_mb

        self.instances: Dict[int, InstanceMetrics] = {}
        self._is_running: bool = False

    def register_instance(self, instance_index: int, serial: str, hwnd_title: str) -> bool:
        """
        Locates target window HWND and parent PID, registering the instance for supervision.
        """
        hwnd = win32gui.FindWindow(None, hwnd_title)
        if not hwnd:
            logger.error(f"[Idx {instance_index}] Could not locate HWND for window title '{hwnd_title}'.")
            return False

        _, pid = win32process_get_window_thread_process_id(hwnd)
        if not pid:
            logger.error(f"[Idx {instance_index}] Could not resolve PID from HWND {hwnd}.")
            return False

        metrics = InstanceMetrics(
            instance_index=instance_index,
            serial=serial,
            hwnd=hwnd,
            pid=pid
        )
        self.instances[instance_index] = metrics
        logger.info(f"Registered Instance {instance_index} | HWND: {hwnd} | PID: {pid} | Serial: {serial}")
        return True

    def capture_window_frame(self, hwnd: int) -> Optional[np.ndarray]:
        """
        Captures window pixels directly from Win32 Device Context using BitBlt.
        Avoids window occlusion issues by targeting client device context handles.
        
        Returns:
            RGB NumPy array shape (720, 1280, 3) or None on capture error.
        """
        try:
            hwnd_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()

            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, self.REFERENCE_WIDTH, self.REFERENCE_HEIGHT)
            save_dc.SelectObject(bitmap)

            # Execute GDI BitBlt copy operation
            save_dc.BitBlt(
                (0, 0),
                (self.REFERENCE_WIDTH, self.REFERENCE_HEIGHT),
                mfc_dc,
                (0, 0),
                win32con.SRCCOPY
            )

            # Convert raw HBITMAP byte buffer to NumPy array
            bmp_info = bitmap.GetInfo()
            bmp_str = bitmap.GetBitmapBits(True)
            img = np.frombuffer(bmp_str, dtype=np.uint8)

            # Clean up Win32 Device Context handles to prevent GDI memory leaks
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)

            # Shape BGRA -> RGB
            img = img.reshape((bmp_info["bmHeight"], bmp_info["bmWidth"], 4))
            rgb_frame = img[:, :, :3][..., ::-1]  # Convert BGR to RGB
            return np.ascontiguousarray(rgb_frame)

        except Exception as err:
            logger.error(f"Win32 BitBlt frame capture failed for HWND {hwnd}: {err}")
            return None

    def get_instance_tree_resources(self, parent_pid: int) -> Tuple[float, float]:
        """
        Calculates aggregate CPU % and RAM (MB) usage across parent process 
        and all spawned child renderer processes (NemuPlayer/MuMuHeadless).
        """
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
    def get_gpu_vram_usage_mb() -> float:
        """
        Queries NVIDIA System Management Interface (nvidia-smi) for aggregate GPU VRAM usage.
        """
        try:
            cmd = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
            output = subprocess.check_output(cmd, timeout=1.0).decode("utf-8").strip()
            return float(output.split("\n")[0])
        except Exception:
            # Fallback if nvidia-smi is unavailable
            return 0.0

    async def restart_instance(self, instance_index: int) -> bool:
        """
        Executes automated hard restart of a failed MuMuPlayer instance via MuMuManager CLI.
        """
        logger.warning(f"[Idx {instance_index}] Initiating automated recovery restart...")

        # Step 1: Force stop instance via CLI
        stop_cmd = [self.mumu_manager_path, "api", "-v", str(instance_index), "shutdown"]
        # Step 2: Launch instance via CLI
        start_cmd = [self.mumu_manager_path, "api", "-v", str(instance_index), "launch"]

        try:
            # Shutdown phase
            proc_stop = await asyncio.create_subprocess_exec(*stop_cmd)
            await proc_stop.wait()
            await asyncio.sleep(3.0)

            # Launch phase
            proc_start = await asyncio.create_subprocess_exec(*start_cmd)
            await proc_start.wait()
            logger.info(f"[Idx {instance_index}] Restart launch command issued. Waiting 15s for boot...")
            await asyncio.sleep(15.0)

            # Reset health state tracker
            if instance_index in self.instances:
                self.instances[instance_index].consecutive_failures = 0
                self.instances[instance_index].is_responsive = True

            return True

        except Exception as err:
            logger.error(f"[Idx {instance_index}] Instance restart failed: {err}")
            return False

    async def check_adb_health(self, serial: str) -> bool:
        """
        Pings target instance over ADB server socket to verify responsiveness.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.adb_server_host, self.adb_server_port),
                timeout=2.0
            )
            
            # Request device transport
            req = f"{len(f'host:transport:{serial}'):04x}host:transport:{serial}".encode("utf-8")
            writer.write(req)
            await writer.drain()

            status = await reader.readexactly(4)
            writer.close()
            await writer.wait_closed()
            return status == b"OKAY"

        except Exception:
            return False

    async def run_supervisor_loop(self, check_interval_seconds: float = 2.0) -> None:
        """
        Main asynchronous watchdog loop monitoring hardware limits, 
        ADB ping responses, and frame capture health.
        """
        self._is_running = True
        logger.info(f"Starting MuMuInstanceSupervisor main loop across {len(self.instances)} instances...")

        while self._is_running:
            system_vram_mb = self.get_gpu_vram_usage_mb()

            for idx, metrics in list(self.instances.items()):
                # 1. Update CPU and RAM usage metrics
                cpu, ram_mb = self.get_instance_tree_resources(metrics.pid)
                metrics.cpu_percent = round(cpu, 1)
                metrics.ram_usage_mb = round(ram_mb, 1)
                metrics.vram_usage_mb = system_vram_mb

                # 2. Check ADB connection responsiveness
                adb_healthy = await self.check_adb_health(metrics.serial)

                # 3. Test frame capture ability
                frame = self.capture_window_frame(metrics.hwnd)
                frame_healthy = frame is not None and frame.size > 0

                if adb_healthy and frame_healthy:
                    metrics.consecutive_failures = 0
                    metrics.is_responsive = True
                    metrics.last_frame_timestamp = time.time()
                else:
                    metrics.consecutive_failures += 1
                    metrics.is_responsive = False
                    logger.warning(
                        f"[Idx {idx}] Health check failed ({metrics.consecutive_failures}/{self.max_consecutive_failures}). "
                        f"ADB Healthy: {adb_healthy} | Frame Healthy: {frame_healthy}"
                    )

                # 4. Trigger recovery restart if threshold exceeded
                if metrics.consecutive_failures >= self.max_consecutive_failures:
                    await self.restart_instance(idx)

                logger.info(
                    f"[Idx {idx}] State: {'OK' if metrics.is_responsive else 'STALLED'} | "
                    f"CPU: {metrics.cpu_percent}% | RAM: {metrics.ram_usage_mb}MB | "
                    f"VRAM Total: {metrics.vram_usage_mb}MB"
                )

            await asyncio.sleep(check_interval_seconds)


def win32process_get_window_thread_process_id(hwnd: int) -> Tuple[int, int]:
    """Helper wrapper for Win32 GetWindowThreadProcessId."""
    thread_id = ctypes.c_ulong()
    process_id = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    return thread_id.value, process_id.value


if __name__ == "__main__":
    supervisor = MuMuInstanceSupervisor()
    
    # Example registering MuMuPlayer instance 0 (title: "MuMuPlayer-1")
    supervisor.register_instance(
        instance_index=0,
        serial="127.0.0.1:16384",
        hwnd_title="MuMuPlayer-1"
    )

    try:
        asyncio.run(supervisor.run_supervisor_loop(check_interval_seconds=3.0))
    except KeyboardInterrupt:
        logger.info("MuMuInstanceSupervisor shut down manually.")