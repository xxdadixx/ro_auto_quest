import asyncio
import json
import logging
import signal
import sys
from typing import Dict, List, Any, Optional

from supervisor.multi_instance_supervisor import MultiInstanceSupervisor
from vision.ro_controller import ROVisionController
from executor.adb_binary_input import ADBBinaryInputExecutor
from web.dashboard_server import DashboardServer

# ANSI Escape Sequences
C_RESET = "\033[0m"
C_GREY = "\033[90m"
C_CYAN = "\033[96m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_MAGENTA = "\033[95m"
C_BOLD = "\033[1m"


class ColoredConsoleFormatter(logging.Formatter):
    """Custom logging formatter rendering ANSI colors per severity level."""

    FORMATS = {
        logging.DEBUG: C_GREY
        + "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
        + C_RESET,
        logging.INFO: C_CYAN
        + "%(asctime)s [%(levelname)s] "
        + C_RESET
        + "(%(name)s) %(message)s",
        logging.WARNING: C_YELLOW
        + "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
        + C_RESET,
        logging.ERROR: C_RED
        + "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
        + C_RESET,
        logging.CRITICAL: C_RED
        + C_BOLD
        + "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
        + C_RESET,
    }

    def format(self, record: logging.LogRecord) -> str:
        log_fmt = self.FORMATS.get(
            record.levelno, "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
        )
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)


# Apply Colored Formatter
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(ColoredConsoleFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("ROAutoQuestMain")


async def discover_and_connect_mumu_adb_ports(
    host: str = "127.0.0.1", port: int = 5037
) -> List[str]:
    """Scans MuMuPlayer 12 ADB ports (16384, 16416, 16448, etc.) and connects active serials."""
    candidate_ports = [16384, 16416, 16448, 16480, 16512, 7555, 5555]

    for p in candidate_ports:
        serial = f"{host}:{p}"
        try:
            reader, writer = await asyncio.open_connection(host, port)
            cmd = f"host:connect:{serial}"
            req = f"{len(cmd):04x}{cmd}".encode("utf-8")
            writer.write(req)
            await writer.drain()
            await reader.read(1024)
            writer.close()
            await writer.wait_closed()
        except Exception:
            continue

    active_serials: List[str] = []
    try:
        reader, writer = await asyncio.open_connection(host, port)
        cmd = "host:devices"
        req = f"{len(cmd):04x}{cmd}".encode("utf-8")
        writer.write(req)
        await writer.drain()

        status = await reader.readexactly(4)
        if status == b"OKAY":
            len_bytes = await reader.readexactly(4)
            length = int(len_bytes.decode("utf-8"), 16)
            resp = await reader.readexactly(length)
            lines = resp.decode("utf-8", errors="ignore").strip().splitlines()

            for line in lines:
                if "\tdevice" in line:
                    ser = line.split("\t")[0]
                    active_serials.append(ser)

        writer.close()
        await writer.wait_closed()
    except Exception as err:
        logger.error(f"Failed querying ADB daemon devices: {err}")

    logger.info(
        f"{C_GREEN}Auto-discovered active ADB device serials: {active_serials}{C_RESET}"
    )
    return active_serials


class ROAutoQuestOrchestrator:
    """Master Application Entry Point with structured color-coded telemetry."""

    def __init__(self, host: str = "0.0.0.0", web_port: int = 8000) -> None:
        self.host: str = host
        self.web_port: int = web_port

        self.supervisor: MultiInstanceSupervisor = MultiInstanceSupervisor()
        self.vision_controllers: Dict[int, ROVisionController] = {}
        self.input_executors: Dict[int, ADBBinaryInputExecutor] = {}
        self.dashboard_server: DashboardServer = DashboardServer(
            supervisor=self.supervisor, host=self.host, port=self.web_port
        )

        self._is_running: bool = False
        self._last_logged_state: Dict[int, str] = {}

    async def initialize_system(self) -> None:
        """Initializes hardware monitors, ADB streams, and vision engines per instance."""
        logger.info(
            f"{C_CYAN}=================================================={C_RESET}"
        )
        logger.info(
            f"{C_CYAN}Initializing RO_AUTO_QUEST Master Automation Engine{C_RESET}"
        )
        logger.info(
            f"{C_CYAN}=================================================={C_RESET}"
        )

        active_serials = await discover_and_connect_mumu_adb_ports()

        for idx in range(3):
            serial = (
                active_serials[idx]
                if idx < len(active_serials)
                else f"127.0.0.1:{16384 + idx * 32}"
            )
            hwnd_title = f"RO:NW{idx+1}"

            registered = self.supervisor.register_instance(
                instance_index=idx, serial=serial, hwnd_title=hwnd_title
            )

            if not registered:
                logger.error(
                    f"{C_RED}[Idx {idx}] Failed to register instance '{hwnd_title}'. Skipping...{C_RESET}"
                )
                continue

            real_serial = self.supervisor.instances[idx].serial

            self.vision_controllers[idx] = ROVisionController(
                instance_id=f"mumu_instance_{idx:02d}", target_fps=15.0
            )

            self.input_executors[idx] = ADBBinaryInputExecutor(
                adb_server_host=self.supervisor.adb_server_host,
                adb_server_port=self.supervisor.adb_server_port,
                is_64bit_kernel=True,
            )

            touch_node = await self.input_executors[idx].discover_touch_node(
                real_serial
            )
            logger.info(
                f"{C_GREEN}[Idx {idx}] Pipeline online ({real_serial}). Target Touch Node: {touch_node}{C_RESET}"
            )

    async def run_instance_bot_loop(self, instance_index: int) -> None:
        """High-frequency decision loop with color-coded telemetry logs."""
        vision_engine = self.vision_controllers[instance_index]
        input_engine = self.input_executors[instance_index]
        metrics = self.supervisor.instances[instance_index]
        inst_tag = f"{C_MAGENTA}[Idx {instance_index}]{C_RESET}"

        logger.info(f"{inst_tag} {C_GREEN}Bot automation loop online.{C_RESET}")

        while self._is_running:
            if not metrics.is_responsive:
                await asyncio.sleep(1.0)
                continue

            frame = self.supervisor.capture_instance_frame(instance_index)
            if frame is None or frame.size == 0:
                await asyncio.sleep(0.05)
                continue

            raw_payload = vision_engine.process_frame(frame)
            payload = json.loads(raw_payload)

            action = payload.get("action", "WAIT")
            state = payload.get("state", "IDLE")
            coords = payload.get("target_coordinates", {"x": 0, "y": 0})
            diagnostic = payload.get("diagnostic", "")
            norm_x, norm_y = coords.get("x", 0), coords.get("y", 0)

            # Log State Transitions
            if self._last_logged_state.get(instance_index) != state:
                logger.info(f"{inst_tag} {C_CYAN}[STATE CHANGE] -> {state}{C_RESET}")
                self._last_logged_state[instance_index] = state

            if action == "CLICK" and (norm_x != 0 or norm_y != 0):
                pixel_x = int((norm_x / 1000.0) * vision_engine.REFERENCE_WIDTH)
                pixel_y = int((norm_y / 1000.0) * vision_engine.REFERENCE_HEIGHT)

                logger.info(
                    f"{inst_tag} {C_GREEN}[ACTION: {state}] Tap ({pixel_x}, {pixel_y}) | {diagnostic}{C_RESET}"
                )

                await input_engine.execute_kernel_tap(
                    serial=metrics.serial, pixel_x=pixel_x, pixel_y=pixel_y
                )
            else:
                # Periodic diagnostic log when searching for UI targets
                logger.debug(f"{inst_tag} {C_YELLOW}[SEARCHING] {diagnostic}{C_RESET}")

            await asyncio.sleep(vision_engine.frame_interval)

    async def start(self) -> None:
        """Launches all background services."""
        self._is_running = True
        await self.initialize_system()

        if not self.supervisor.instances:
            logger.critical(
                f"{C_RED}No valid emulator instances registered. Exiting.{C_RESET}"
            )
            return

        tasks = [
            asyncio.create_task(
                self.supervisor.run_supervisor_loop(check_interval_seconds=5.0)
            ),
            asyncio.create_task(self.dashboard_server.start()),
        ]

        for idx in self.supervisor.instances.keys():
            tasks.append(asyncio.create_task(self.run_instance_bot_loop(idx)))

        logger.info(
            f"{C_GREEN}RO_AUTO_QUEST fully operational. Web Dashboard: http://localhost:{self.web_port}{C_RESET}"
        )

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("System tasks canceled.")

    def shutdown(self) -> None:
        """Gracefully halts all async loops."""
        logger.info(f"{C_YELLOW}Initiating graceful system shutdown...{C_RESET}")
        self._is_running = False


async def main_entry() -> None:
    orchestrator = ROAutoQuestOrchestrator(host="0.0.0.0", web_port=8000)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orchestrator.shutdown)
        except NotImplementedError:
            pass

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        orchestrator.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main_entry())
    except KeyboardInterrupt:
        logger.info(f"{C_YELLOW}RO_AUTO_QUEST process terminated by user.{C_RESET}")
