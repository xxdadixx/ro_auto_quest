import json
import os
import time
from typing import Dict, Any


class JSONPayloadManager:
    """
    Handles serialization, disk persistence, and string formatting
    for ROVisionController action payloads.
    """

    @staticmethod
    def save_action_payload(
        payload_dict: Dict[str, Any], output_dir: str = "logs/payloads"
    ) -> str:
        """
        Persists a vision action dictionary to an atomic UTF-8 encoded .json file.

        Args:
            payload_dict: Unserialized dictionary output from ROVisionController.
            output_dir: Target directory path for payload logs.

        Returns:
            Absolute file path of written JSON file.
        """
        os.makedirs(output_dir, exist_ok=True)
        instance_id = payload_dict.get("instance_id", "instance_unknown")
        timestamp_ms = int(time.time() * 1000)
        file_name = f"{instance_id}_{timestamp_ms}.json"
        file_path = os.path.join(output_dir, file_name)

        # Write atomically using UTF-8 encoding to support Thai character sets
        with open(file_path, "w", encoding="utf-8") as json_file:
            json.dump(payload_dict, json_file, ensure_ascii=False, indent=2)

        return file_path

    @staticmethod
    def serialize_for_ipc(payload_dict: Dict[str, Any]) -> str:
        """
        Serializes dictionary into a compact string payload for IPC streaming.
        """
        return json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":"))
