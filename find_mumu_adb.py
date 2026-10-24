import os
import subprocess
import socket

MUMU_BASE_DIR = r"E:\Program Files\Netease\MuMuPlayer"

def locate_adb() -> str | None:
    """Recursively scans the MuMuPlayer installation directory for adb.exe."""
    for root, _, files in os.walk(MUMU_BASE_DIR):
        if "adb.exe" in files:
            return os.path.join(root, "adb.exe")
    return None

def main():
    print(f"Scanning for adb.exe in '{MUMU_BASE_DIR}'...")
    adb_path = locate_adb()

    if not adb_path:
        print("❌ Could not locate adb.exe. Please verify your MuMuPlayer install path.")
        return

    print(f"✅ Found adb.exe: {adb_path}\n")

    # Start ADB daemon and list active devices
    try:
        output = subprocess.check_output([adb_path, "devices"]).decode()
        print("--- Active ADB Instances & Ports ---")
        print(output)
    except Exception as err:
        print(f"Failed to execute adb: {err}")

if __name__ == "__main__":
    main()