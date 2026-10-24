import json
import logging
import os
import shutil
import time
from typing import Dict, Any, Optional, Tuple

import cv2
import numpy as np
import pytesseract  # type: ignore

logger = logging.getLogger("ROVisionController")

# ANSI Color Codes for Terminal Output
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"
C_RED = "\033[91m"


def _auto_configure_tesseract_path() -> bool:
    """Scans system PATH and common Windows installation locations for tesseract.exe."""
    if shutil.which("tesseract"):
        logger.info(f"{C_GREEN}Tesseract OCR binary found in system PATH.{C_RESET}")
        return True

    candidate_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
        os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
        r"E:\Program Files\Tesseract-OCR\tesseract.exe",
    ]

    for path in candidate_paths:
        if os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            logger.info(
                f"{C_GREEN}Auto-configured Tesseract binary path: '{path}'{C_RESET}"
            )
            return True

    logger.warning(
        f"{C_YELLOW}Tesseract OCR executable not detected in standard locations.{C_RESET}"
    )
    return False


HAS_TESSERACT: bool = _auto_configure_tesseract_path()


class ROVisionController:
    """
    Ragnarok: The New World vision classification engine.
    Provides color-coded diagnostic logging for Thai OCR, HSV masking, and HUD quest tracking.
    """

    REFERENCE_WIDTH: int = 1280
    REFERENCE_HEIGHT: int = 720

    SKIP_KEYWORDS = ["แตะเพื่อข้าม", "ข้าม", "แตะ", "skip"]
    MAIN_QUEST_TOKENS = [
        "การเดินทาง",
        "[การเดินทาง]",
        "เดินทาง",
        "ทางการ",
        "[ทางการ]",
        "ทาง",
        "[ทาง]",
        "journey",
        "main",
    ]

    def __init__(self, instance_id: str = "mumu_00", target_fps: float = 15.0) -> None:
        self.instance_id: str = instance_id
        self.target_fps: float = target_fps
        self.frame_interval: float = 1.0 / target_fps

        self.current_state: str = "INITIALIZING"
        self.last_state_change: float = time.time()
        self.has_ocr: bool = HAS_TESSERACT

    def _safe_ocr(self, roi: np.ndarray, config: str = "--psm 6") -> str:
        """Performs multi-language (tha+eng) OCR reading with error handling."""
        if not self.has_ocr:
            self.has_ocr = _auto_configure_tesseract_path()
            if not self.has_ocr:
                return ""

        try:
            gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
            _, thresh = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
            text = pytesseract.image_to_string(thresh, lang="tha+eng", config=config)
            return text.strip()
        except Exception:
            try:
                text = pytesseract.image_to_string(thresh, config=config)
                return text.strip()
            except Exception:
                return ""

    def _detect_cutscene_skip(
        self, frame: np.ndarray
    ) -> Tuple[Optional[Tuple[int, int]], str]:
        """Scans top-right screen area for Thai 'แตะเพื่อข้าม' (Tap to Skip) button."""
        h, w, _ = frame.shape
        roi = frame[int(h * 0.02) : int(h * 0.12), int(w * 0.82) : int(w * 0.98)]

        # 1. OCR Keyword Match
        ocr_text = self._safe_ocr(roi, config="--psm 7")
        clean_text = ocr_text.replace("\n", " ").strip()

        if any(kw in clean_text.lower() for kw in self.SKIP_KEYWORDS):
            diag = f"OCR Match: '{clean_text}'"
            return (int(w * 0.92), int(h * 0.06)), diag

        # 2. Brightness/Contrast Fallback
        gray_roi = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        _, white_mask = cv2.threshold(gray_roi, 200, 255, cv2.THRESH_BINARY)
        white_pixel_ratio = cv2.countNonZero(white_mask) / (roi.shape[0] * roi.shape[1])

        if white_pixel_ratio > 0.04:
            diag = f"High-contrast text box detected (Ratio: {white_pixel_ratio:.3f} > 0.040)"
            return (int(w * 0.92), int(h * 0.06)), diag

        diag_reason = (
            f"No Skip button found. OCR text: '{clean_text if clean_text else '<EMPTY>'}' | "
            f"White Ratio: {white_pixel_ratio:.3f} (Threshold: 0.040)"
        )
        return None, diag_reason

    def _detect_main_quest_tag(
        self, frame: np.ndarray
    ) -> Tuple[Optional[Tuple[int, int]], str]:
        """Scans top slot of the left-side Quest Panel for '[การเดินทาง]' / '[ทางการ]'."""
        h, w, _ = frame.shape
        roi = frame[int(h * 0.22) : int(h * 0.35), int(w * 0.01) : int(w * 0.26)]

        # 1. OCR Token Match
        ocr_text = self._safe_ocr(roi, config="--psm 6")
        clean_text = ocr_text.replace("\n", " ").strip()

        if (
            any(token in clean_text for token in self.MAIN_QUEST_TOKENS)
            or "[" in clean_text
            or "]" in clean_text
        ):
            diag = f"OCR Token Match: '{clean_text}'"
            return (int(w * 0.12), int(h * 0.28)), diag

        # 2. HSV Yellow/Gold Header Masking
        hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
        lower_yellow = np.array([10, 80, 150])
        upper_yellow = np.array([40, 255, 255])
        yellow_mask = cv2.inRange(hsv, lower_yellow, upper_yellow)
        yellow_count = cv2.countNonZero(yellow_mask)

        if yellow_count > 60:
            diag = f"HSV Yellow Header Match (Count: {yellow_count} > 60)"
            return (int(w * 0.12), int(h * 0.28)), diag

        # 3. Positional Fallback
        auto_battle_roi = frame[
            int(h * 0.50) : int(h * 0.65), int(w * 0.90) : int(w * 0.98)
        ]
        hud_active = (
            cv2.countNonZero(cv2.cvtColor(auto_battle_roi, cv2.COLOR_RGB2GRAY) > 100)
            > 300
        )

        if hud_active:
            diag = "Positional Anchor: Active Gameplay HUD detected (Top Quest Slot)"
            return (int(w * 0.12), int(h * 0.28)), diag

        diag_reason = (
            f"No Quest found. OCR text: '{clean_text if clean_text else '<EMPTY>'}' | "
            f"Yellow HSV Count: {yellow_count} (Threshold: 60) | HUD Active: {hud_active}"
        )
        return None, diag_reason

    def process_frame(self, frame: np.ndarray) -> str:
        """Processes frame buffer and returns a rich JSON diagnostic payload."""
        if frame is None or frame.size == 0:
            return json.dumps(
                {
                    "action": "WAIT",
                    "state": "IDLE",
                    "target_coordinates": {"x": 0, "y": 0},
                    "diagnostic": "Empty or null frame buffer received",
                }
            )

        h, w, _ = frame.shape

        # 1. Check Cutscene / Dialogue Skip
        skip_coords, skip_diag = self._detect_cutscene_skip(frame)
        if skip_coords:
            self.current_state = "SKIP_DIALOGUE"
            norm_x = int((skip_coords[0] / w) * 1000)
            norm_y = int((skip_coords[1] / h) * 1000)
            return json.dumps(
                {
                    "action": "CLICK",
                    "state": "SKIP_DIALOGUE",
                    "target_coordinates": {"x": norm_x, "y": norm_y},
                    "diagnostic": f"Skip button identified via {skip_diag}",
                }
            )

        # 2. Check Main Quest Panel
        quest_coords, quest_diag = self._detect_main_quest_tag(frame)
        if quest_coords:
            self.current_state = "AUTO_QUESTING"
            norm_x = int((quest_coords[0] / w) * 1000)
            norm_y = int((quest_coords[1] / h) * 1000)
            return json.dumps(
                {
                    "action": "CLICK",
                    "state": "AUTO_QUESTING",
                    "target_coordinates": {"x": norm_x, "y": norm_y},
                    "diagnostic": f"Main Quest '[การเดินทาง]' identified via {quest_diag}",
                }
            )

        self.current_state = "SEARCHING"
        return json.dumps(
            {
                "action": "WAIT",
                "state": "SEARCHING",
                "target_coordinates": {"x": 0, "y": 0},
                "diagnostic": f"[{skip_diag}] AND [{quest_diag}]",
            }
        )
