import asyncio
import logging
import struct
import time
from typing import Optional, Tuple

logger = logging.getLogger("ADBBinaryInputExecutor")

# Linux Input Event Protocol Constants (input_event struct)
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

SYN_REPORT = 0x00
BTN_TOUCH = 0x140  # 330
ABS_MT_SLOT = 0x2F  # 47
ABS_MT_TOUCH_MAJOR = 0x30  # 48
ABS_MT_POSITION_X = 0x35  # 53
ABS_MT_POSITION_Y = 0x36  # 54
ABS_MT_TRACKING_ID = 0x39  # 57


class ADBBinaryInputExecutor:
    """
    Sub-2ms direct kernel touch injection engine for Android emulators.
    Bypasses high-overhead 'input tap' subprocesses by piping raw binary
    Linux input_event structs directly into target /dev/input/eventX device nodes.
    """

    def __init__(
        self,
        adb_server_host: str = "127.0.0.1",
        adb_server_port: int = 5037,
        is_64bit_kernel: bool = True,
    ) -> None:
        self.adb_host: str = adb_server_host
        self.adb_port: int = adb_server_port
        self.is_64bit_kernel: bool = is_64bit_kernel

        # Struct format: timeval (tv_sec, tv_usec), type (uint16), code (uint16), value (int32)
        # 64-bit Linux kernel struct size = 24 bytes (q, q, H, H, i)
        # 32-bit Linux kernel struct size = 16 bytes (l, l, H, H, i)
        self.struct_fmt: str = "qqHHi" if self.is_64bit_kernel else "llHHi"
        self.touch_nodes: dict[str, str] = {}
        self.persistent_writers: dict[str, asyncio.StreamWriter] = {}

    async def ensure_device_connected(self, serial: str) -> bool:
        """Sends host:connect:<serial> over ADB socket to register TCP device with daemon."""
        try:
            reader, writer = await asyncio.open_connection(self.adb_host, self.adb_port)
            cmd = f"host:connect:{serial}"
            req = f"{len(cmd):04x}{cmd}".encode("utf-8")
            writer.write(req)
            await writer.drain()

            status = await reader.readexactly(4)
            if status == b"OKAY":
                len_bytes = await reader.readexactly(4)
                resp_len = int(len_bytes.decode("utf-8"), 16)
                resp_msg = await reader.readexactly(resp_len)
                logger.info(
                    f"ADB device connect status for {serial}: {resp_msg.decode().strip()}"
                )

            writer.close()
            await writer.wait_closed()
            return True
        except Exception as err:
            logger.warning(f"Could not issue connect command for {serial}: {err}")
            return False

    async def _open_transport_stream(
        self, serial: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Ensures device TCP registration and opens host:transport socket stream."""
        await self.ensure_device_connected(serial)

        reader, writer = await asyncio.open_connection(self.adb_host, self.adb_port)
        cmd = f"host:transport:{serial}"
        req = f"{len(cmd):04x}{cmd}".encode("utf-8")
        writer.write(req)
        await writer.drain()

        status = await reader.readexactly(4)
        if status != b"OKAY":
            err_len_bytes = await reader.readexactly(4)
            err_len = int(err_len_bytes.decode("utf-8"), 16)
            err_msg = await reader.readexactly(err_len)
            writer.close()
            await writer.wait_closed()
            raise ConnectionError(
                f"Transport selection failed for {serial}: {err_msg.decode('utf-8', errors='ignore')}"
            )

        return reader, writer

    async def discover_touch_node(self, serial: str) -> str:
        """
        Scans /proc/bus/input/devices to automatically identify the touch screen
        device node (e.g., /dev/input/event1 or /dev/input/event2).
        """
        try:
            reader, writer = await self._open_transport_stream(serial)

            cmd = "shell:cat /proc/bus/input/devices"
            req = f"{len(cmd):04x}{cmd}".encode("utf-8")
            writer.write(req)
            await writer.drain()

            status = await reader.readexactly(4)
            if status != b"OKAY":
                writer.close()
                await writer.wait_closed()
                raise ConnectionError("Failed to initiate device scan shell command.")

            # Read stream until EOF
            raw_devices = await reader.read()
            devices_text = raw_devices.decode("utf-8", errors="ignore")

            writer.close()
            await writer.wait_closed()

            target_event = ""
            current_handlers = ""
            is_touch_device = False

            for line in devices_text.splitlines():
                if line.startswith("N: Name="):
                    if any(
                        kw in line.lower()
                        for kw in ["touch", "multi", "screen", "position"]
                    ):
                        is_touch_device = True
                    else:
                        is_touch_device = False
                elif line.startswith("H: Handlers=") and is_touch_device:
                    current_handlers = line
                    for part in current_handlers.split():
                        if part.startswith("event"):
                            target_event = f"/dev/input/{part}"
                            break

            if not target_event:
                target_event = "/dev/input/event2"  # MuMuPlayer 12 default touch node
                logger.warning(
                    f"Using default fallback node '{target_event}' for instance {serial}"
                )
            else:
                logger.info(
                    f"Auto-discovered touch node '{target_event}' for instance {serial}"
                )

            self.touch_nodes[serial] = target_event
            return target_event

        except Exception as err:
            logger.error(f"Error during touch node auto-discovery on {serial}: {err}")
            fallback = "/dev/input/event2"
            self.touch_nodes[serial] = fallback
            return fallback

    def encode_event(self, ev_type: int, code: int, value: int) -> bytes:
        """Packs event data into native Linux C input_event struct byte array."""
        now = time.time()
        tv_sec = int(now)
        tv_usec = int((now - tv_sec) * 1_000_000)
        return struct.pack(self.struct_fmt, tv_sec, tv_usec, ev_type, code, value)

    async def _get_persistent_pipe(
        self, serial: str, touch_node: str
    ) -> asyncio.StreamWriter:
        """Maintains persistent cat stream connected directly to /dev/input/eventX."""
        if serial in self.persistent_writers:
            writer = self.persistent_writers[serial]
            if not writer.is_closing():
                return writer

        reader, writer = await self._open_transport_stream(serial)

        cmd = f"shell:cat > {touch_node}"
        req = f"{len(cmd):04x}{cmd}".encode("utf-8")
        writer.write(req)
        await writer.drain()

        status = await reader.readexactly(4)
        if status != b"OKAY":
            writer.close()
            await writer.wait_closed()
            raise ConnectionError(f"Failed establishing shell pipe to {touch_node}")

        self.persistent_writers[serial] = writer
        return writer

    async def execute_kernel_tap(
        self, serial: str, pixel_x: int, pixel_y: int, tracking_id: int = 100
    ) -> bool:
        """
        Injects multi-touch protocol sequence B binary payload into kernel event queue:
        ABS_MT_SLOT -> ABS_MT_TRACKING_ID -> ABS_MT_POSITION_X/Y -> BTN_TOUCH -> EV_SYN
        """
        touch_node = self.touch_nodes.get(serial)
        if not touch_node:
            touch_node = await self.discover_touch_node(serial)

        # 1. DOWN Event Sequence
        down_payload = bytearray()
        down_payload.extend(self.encode_event(EV_ABS, ABS_MT_SLOT, 0))
        down_payload.extend(self.encode_event(EV_ABS, ABS_MT_TRACKING_ID, tracking_id))
        down_payload.extend(self.encode_event(EV_ABS, ABS_MT_POSITION_X, pixel_x))
        down_payload.extend(self.encode_event(EV_ABS, ABS_MT_POSITION_Y, pixel_y))
        down_payload.extend(self.encode_event(EV_KEY, BTN_TOUCH, 1))
        down_payload.extend(self.encode_event(EV_SYN, SYN_REPORT, 0))

        # 2. UP Event Sequence
        up_payload = bytearray()
        up_payload.extend(self.encode_event(EV_ABS, ABS_MT_SLOT, 0))
        up_payload.extend(self.encode_event(EV_ABS, ABS_MT_TRACKING_ID, -1))
        up_payload.extend(self.encode_event(EV_KEY, BTN_TOUCH, 0))
        up_payload.extend(self.encode_event(EV_SYN, SYN_REPORT, 0))

        for attempt in range(2):
            try:
                pipe_writer = await self._get_persistent_pipe(serial, touch_node)

                # Send DOWN, hold 25ms, send UP
                pipe_writer.write(down_payload)
                await pipe_writer.drain()

                await asyncio.sleep(0.025)

                pipe_writer.write(up_payload)
                await pipe_writer.drain()

                return True

            except Exception as err:
                logger.warning(
                    f"Attempt {attempt + 1}: Injection error on {serial}: {err}"
                )
                if serial in self.persistent_writers:
                    try:
                        self.persistent_writers[serial].close()
                    except Exception:
                        pass
                    del self.persistent_writers[serial]

        logger.error(f"Critical failure executing kernel touch injection on {serial}.")
        return False
