import asyncio
import json
import logging
from typing import Dict, Any, Tuple, Optional

# Configure execution logger
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("ADBSocketExecutor")


class ADBSocketExecutor:
    """
    High-performance, low-latency ADB action executor for MuMuPlayer 12 instances.
    Bypasses OS process-spawning overhead by maintaining persistent TCP socket streams
    directly to the ADB daemon server (127.0.0.1:5037) and piping commands to /system/bin/sh.
    """

    DEFAULT_HOST: str = "127.0.0.1"
    DEFAULT_PORT: int = 9999
    ADB_SERVER_HOST: str = "127.0.0.1"
    ADB_SERVER_PORT: int = 5037

    # Default Target Screen Dimensions for MuMuPlayer Instances
    DEFAULT_INSTANCE_WIDTH: int = 1280
    DEFAULT_INSTANCE_HEIGHT: int = 720

    # Instance Port/Serial Mapping (MuMuPlayer 12 base ports)
    INSTANCE_ADB_MAP: Dict[str, str] = {
        "mumu_instance_01": "127.0.0.1:16384",
        "mumu_instance_02": "127.0.0.1:16385",
        "mumu_instance_03": "127.0.0.1:16386",
    }

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        adb_server_host: str = ADB_SERVER_HOST,
        adb_server_port: int = ADB_SERVER_PORT,
    ) -> None:
        self.host: str = host
        self.port: int = port
        self.adb_server_host: str = adb_server_host
        self.adb_server_port: int = adb_server_port
        self._server: Optional[asyncio.AbstractServer] = None

        # Persistent Shell Socket Pool: {serial: (StreamReader, StreamWriter)}
        self._persistent_shells: Dict[
            str, Tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] = {}

    @staticmethod
    def _format_adb_request(command: str) -> bytes:
        """
        Formats a raw string command into the low-level ADB wire protocol format.
        Header consists of a 4-character hex-encoded string length prefix.

        Example: "shell:" -> b"0006shell:"
        """
        cmd_bytes = command.encode("utf-8")
        length_prefix = f"{len(cmd_bytes):04x}".encode("utf-8")
        return length_prefix + cmd_bytes

    async def _get_or_create_persistent_shell(
        self, serial: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """
        Retrieves or initializes a persistent interactive shell socket connection
        to an emulator instance via the local ADB server daemon.
        """
        if serial in self._persistent_shells:
            reader, writer = self._persistent_shells[serial]
            if not writer.is_closing():
                return reader, writer
            else:
                logger.warning(
                    f"Persistent shell connection for {serial} closed. Rebuilding connection."
                )
                self._persistent_shells.pop(serial, None)

        # Step 1: Open direct socket connection to local ADB Daemon Server
        reader, writer = await asyncio.open_connection(
            self.adb_server_host, self.adb_server_port
        )

        try:
            # Step 2: Request device transport channel (host:transport:<serial>)
            transport_req = self._format_adb_request(f"host:transport:{serial}")
            writer.write(transport_req)
            await writer.drain()

            status = await reader.readexactly(4)
            if status != b"OKAY":
                error_msg = await reader.read(1024)
                raise ConnectionError(
                    f"ADB Transport selection failed for {serial}: {error_msg.decode('utf-8', 'ignore')}"
                )

            # Step 3: Upgrade transport connection to an interactive raw shell stream
            shell_req = self._format_adb_request("shell:")
            writer.write(shell_req)
            await writer.drain()

            status = await reader.readexactly(4)
            if status != b"OKAY":
                error_msg = await reader.read(1024)
                raise ConnectionError(
                    f"ADB Shell service request failed for {serial}: {error_msg.decode('utf-8', 'ignore')}"
                )

            logger.info(
                f"Successfully established persistent low-latency ADB shell stream -> {serial}"
            )
            self._persistent_shells[serial] = (reader, writer)
            return reader, writer

        except Exception as err:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError(
                f"Failed to initialize ADB socket stream for {serial}: {err}"
            )

    def _denormalize_coordinates(
        self, norm_x: int, norm_y: int, instance_width: int, instance_height: int
    ) -> Tuple[int, int]:
        """Converts normalized (0-1000) coordinates to absolute pixel values."""
        pixel_x = int((norm_x / 1000.0) * instance_width)
        pixel_y = int((norm_y / 1000.0) * instance_height)
        return pixel_x, pixel_y

    async def _execute_persistent_adb_tap(
        self, adb_target: str, pixel_x: int, pixel_y: int
    ) -> bool:
        """
        Executes a sub-5ms tap command by writing directly to the persistent ADB shell TCP stream.
        """
        tap_cmd = f"input tap {pixel_x} {pixel_y}\n".encode("utf-8")

        for attempt in range(2):
            try:
                _, writer = await self._get_or_create_persistent_shell(adb_target)
                writer.write(tap_cmd)
                await writer.drain()
                logger.info(
                    f"Sub-5ms Persistent Tap -> Target: {adb_target} | Pixel Coords: ({pixel_x}, {pixel_y})"
                )
                return True

            except (ConnectionError, OSError, asyncio.IncompleteReadError) as err:
                logger.warning(
                    f"Attempt {attempt + 1}: Socket send failed for {adb_target}: {err}. "
                    f"Clearing cache and retrying..."
                )
                self._persistent_shells.pop(adb_target, None)

        logger.error(
            f"Critical failure executing persistent tap for {adb_target} after retry."
        )
        return False

    async def _process_payload(self, raw_payload: str) -> None:
        """Parses JSON action payload and routes sub-5ms input commands."""
        try:
            payload: Dict[str, Any] = json.loads(raw_payload)
        except json.JSONDecodeError as err:
            logger.error(f"Received malformed JSON payload: {err}")
            return

        instance_id = payload.get("instance_id", "")
        action = payload.get("action", "WAIT")
        target_coords = payload.get("target_coordinates", {})
        norm_x = target_coords.get("x", 0)
        norm_y = target_coords.get("y", 0)

        if action != "CLICK":
            return

        if norm_x == 0 and norm_y == 0:
            logger.warning(
                f"[{instance_id}] Ignored CLICK action on unmapped (0,0) target."
            )
            return

        adb_target = self.INSTANCE_ADB_MAP.get(instance_id, instance_id)
        pixel_x, pixel_y = self._denormalize_coordinates(
            norm_x, norm_y, self.DEFAULT_INSTANCE_WIDTH, self.DEFAULT_INSTANCE_HEIGHT
        )

        # Dispatch low-latency direct TCP stream tap
        await self._execute_persistent_adb_tap(adb_target, pixel_x, pixel_y)

    async def handle_client_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handles incoming stream connections from vision producer nodes."""
        client_address = writer.get_extra_info("peername")
        logger.info(f"Connected to vision producer at {client_address}")

        try:
            while not reader.at_eof():
                data = await reader.readline()
                if not data:
                    break

                raw_line = data.decode("utf-8").strip()
                if raw_line:
                    await self._process_payload(raw_line)

        except asyncio.CancelledError:
            pass
        except Exception as err:
            logger.error(f"Error handling socket client {client_address}: {err}")
        finally:
            logger.info(f"Closing producer connection {client_address}")
            writer.close()
            await writer.wait_closed()

    async def close_all_persistent_shells(self) -> None:
        """Gracefully closes all open persistent ADB stream sockets."""
        for serial, (_, writer) in list(self._persistent_shells.items()):
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
        self._persistent_shells.clear()
        logger.info("All persistent ADB shell stream sockets closed.")

    async def start_server(self) -> None:
        """Initializes non-blocking TCP socket listener."""
        self._server = await asyncio.start_server(
            self.handle_client_connection, self.host, self.port
        )
        server_address = self._server.sockets[0].getsockname()
        logger.info(
            f"ADBSocketExecutor listening on {server_address[0]}:{server_address[1]}"
        )

        async with self._server:
            await self._server.serve_forever()


if __name__ == "__main__":
    executor = ADBSocketExecutor(host="127.0.0.1", port=9999)
    try:
        asyncio.run(executor.start_server())
    except KeyboardInterrupt:
        logger.info("Shutting down ADBSocketExecutor...")
