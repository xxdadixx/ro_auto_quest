import asyncio
import logging
import sys
from executor.adb_binary_input import ADBBinaryInputExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TestTapInput")

# Configuration for test instance
TARGET_SERIAL = "127.0.0.1:16384"  # Instance 0 ADB Serial
TAP_X = 640                        # Target X pixel coordinate
TAP_Y = 360                        # Target Y pixel coordinate

async def run_tap_test() -> None:
    executor = ADBBinaryInputExecutor(
        adb_server_host="127.0.0.1",
        adb_server_port=5037,
        is_64bit_kernel=True
    )

    logger.info(f"Connecting to ADB Instance {TARGET_SERIAL}...")

    # 1. Discover device touch node (e.g., /dev/input/event1 or event3)
    touch_node = await executor.discover_touch_node(TARGET_SERIAL)
    if not touch_node:
        logger.error(f"Failed to resolve touch input node for {TARGET_SERIAL}")
        return

    logger.info(f"Target Touch Device Node: {touch_node}")

    # 2. Inject binary touch tap event
    logger.info(f"Executing kernel tap at pixel coordinates ({TAP_X}, {TAP_Y})...")
    await executor.execute_kernel_tap(
        serial=TARGET_SERIAL,
        pixel_x=TAP_X,
        pixel_y=TAP_Y
    )
    logger.info("Tap command executed successfully.")

if __name__ == "__main__":
    asyncio.run(run_tap_test())