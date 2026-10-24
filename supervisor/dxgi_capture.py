import ctypes
from ctypes import wintypes
import logging
import time
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

logger = logging.getLogger("DXGIDesktopDuplication")

# Direct3D 11 & DXGI Constants
D3D_DRIVER_TYPE_UNKNOWN = 0
D3D_DRIVER_TYPE_HARDWARE = 1
D3D11_SDK_VERSION = 7
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1

MONITOR_DEFAULTTONEAREST = 2


# Win32 Structures
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


# Direct3D / DXGI Structures
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_byte * 8),
    ]


IID_IDXGIFactory1 = GUID(
    0x770AAE78, 0xF26F, 0x4DBA, (0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87)
)
IID_IDXGIOutput1 = GUID(
    0x00CD19FC, 0x10B0, 0x4778, (0xBF, 0x31, 0x86, 0x05, 0x8A, 0xE2, 0xD0, 0x04)
)
IID_ID3D11Texture2D = GUID(
    0x6F15856C, 0x4806, 0x4158, (0xA0, 0x31, 0x50, 0x3E, 0x01, 0x85, 0xAA, 0x21)
)


class DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", ctypes.c_uint),
        ("Monitor", wintypes.HMONITOR),
    ]


class D3D11_BOX(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_uint),
        ("top", ctypes.c_uint),
        ("front", ctypes.c_uint),
        ("right", ctypes.c_uint),
        ("bottom", ctypes.c_uint),
        ("back", ctypes.c_uint),
    ]


class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", ctypes.c_int64),
        ("LastMouseUpdateTime", ctypes.c_int64),
        ("AccumulatedFrames", ctypes.c_uint),
        ("RectsCoalesced", ctypes.c_bool),
        ("ProtectedOutputConsumed", ctypes.c_bool),
        ("ZeroedFrame", ctypes.c_bool),
        ("PointerPositionValid", ctypes.c_bool),
        ("PointerShapeBufferSize", ctypes.c_uint),
        ("LastMouseUpdate", ctypes.c_byte * 16),
    ]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", ctypes.c_uint),
        ("Height", ctypes.c_uint),
        ("MipLevels", ctypes.c_uint),
        ("ArraySize", ctypes.c_uint),
        ("Format", ctypes.c_uint),
        ("SampleDesc_Count", ctypes.c_uint),
        ("SampleDesc_Quality", ctypes.c_uint),
        ("Usage", ctypes.c_uint),
        ("BindFlags", ctypes.c_uint),
        ("CPUAccessFlags", ctypes.c_uint),
        ("MiscFlags", ctypes.c_uint),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData", ctypes.c_void_p),
        ("RowPitch", ctypes.c_uint),
        ("DepthPitch", ctypes.c_uint),
    ]


class DXGIDesktopDuplication:
    """
    Sub-millisecond multi-monitor DXGI Desktop Duplication engine with dynamic
    display auto-detection, GPU ROI cropping, and high-speed GDI fallback.
    """

    def __init__(self) -> None:
        self.d3d11 = ctypes.windll.d3d11
        self.dxgi = ctypes.windll.dxgi
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32

        # Selected Display State
        self.adapter_index: int = 0
        self.output_index: int = 0
        self.device_name: str = ""
        self.desktop_rect: Tuple[int, int, int, int] = (0, 0, 0, 0)

        # Active COM Interfaces
        self.device = ctypes.c_void_p()
        self.context = ctypes.c_void_p()
        self.duplication = ctypes.c_void_p()
        self.staging_texture = ctypes.c_void_p()

        self.roi_width: int = 0
        self.roi_height: int = 0
        self._is_initialized: bool = False
        self._use_gdi_fallback: bool = False

    def enumerate_monitors(self) -> List[Dict[str, Any]]:
        """Scans all physical GPUs and attached outputs using DXGIFactory1."""
        factory = ctypes.c_void_p()
        res = self.dxgi.CreateDXGIFactory1(
            ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)
        )
        if res != 0 or not factory.value:
            return []

        monitors = []
        adapter_idx = 0

        while True:
            adapter = ctypes.c_void_p()
            if (
                self._call_vtbl(factory, 7, adapter_idx, ctypes.byref(adapter)) != 0
                or not adapter.value
            ):
                break

            output_idx = 0
            while True:
                output = ctypes.c_void_p()
                if (
                    self._call_vtbl(adapter, 7, output_idx, ctypes.byref(output)) != 0
                    or not output.value
                ):
                    break

                desc = DXGI_OUTPUT_DESC()
                if self._call_vtbl(output, 7, ctypes.byref(desc)) == 0:
                    rect = (
                        desc.DesktopCoordinates.left,
                        desc.DesktopCoordinates.top,
                        desc.DesktopCoordinates.right,
                        desc.DesktopCoordinates.bottom,
                    )
                    monitors.append(
                        {
                            "adapter_index": adapter_idx,
                            "output_index": output_idx,
                            "device_name": desc.DeviceName,
                            "hmonitor": desc.Monitor,
                            "desktop_rect": rect,
                            "attached": bool(desc.AttachedToDesktop),
                        }
                    )

                self._call_vtbl(output, 2)
                output_idx += 1

            self._call_vtbl(adapter, 2)
            adapter_idx += 1

        self._call_vtbl(factory, 2)
        return monitors

    def auto_select_by_hwnd(self, hwnd: int) -> bool:
        """Auto-detects physical monitor containing target window HWND."""
        hmonitor = self.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        available_monitors = self.enumerate_monitors()

        if hmonitor:
            for mon in available_monitors:
                if mon["hmonitor"] == hmonitor:
                    self.adapter_index = mon["adapter_index"]
                    self.output_index = mon["output_index"]
                    self.device_name = mon["device_name"]
                    self.desktop_rect = mon["desktop_rect"]
                    logger.info(
                        f"Auto-selected monitor for HWND {hwnd} -> "
                        f"Adapter: {self.adapter_index} | Output: {self.output_index} | "
                        f"Device: '{self.device_name}' | Bounds: {self.desktop_rect}"
                    )
                    return True

        if available_monitors:
            mon = available_monitors[0]
            self.adapter_index = mon["adapter_index"]
            self.output_index = mon["output_index"]
            self.device_name = mon["device_name"]
            self.desktop_rect = mon["desktop_rect"]
            return True

        return True

    def initialize_dxgi(self) -> bool:
        """Instantiates D3D11 device and DuplicateOutput interface with GDI fallback."""
        try:
            factory = ctypes.c_void_p()
            res = self.dxgi.CreateDXGIFactory1(
                ctypes.byref(IID_IDXGIFactory1), ctypes.byref(factory)
            )
            if res != 0 or not factory.value:
                return self._enable_gdi_fallback("CreateDXGIFactory1 failed")

            adapter = ctypes.c_void_p()
            res = self._call_vtbl(factory, 7, self.adapter_index, ctypes.byref(adapter))
            if res != 0 or not adapter.value:
                self._call_vtbl(factory, 2)
                return self._enable_gdi_fallback(
                    f"EnumAdapters failed on index {self.adapter_index}"
                )

            output = ctypes.c_void_p()
            res = self._call_vtbl(adapter, 7, self.output_index, ctypes.byref(output))
            if res != 0 or not output.value:
                self._call_vtbl(adapter, 2)
                self._call_vtbl(factory, 2)
                return self._enable_gdi_fallback(
                    f"EnumOutputs failed on index {self.output_index}"
                )

            output1 = ctypes.c_void_p()
            try:
                res = self._call_vtbl(
                    output, 0, ctypes.byref(IID_IDXGIOutput1), ctypes.byref(output1)
                )
            except Exception as e:
                res = -1

            if res != 0 or not output1.value:
                self._call_vtbl(output, 2)
                self._call_vtbl(adapter, 2)
                self._call_vtbl(factory, 2)
                return self._enable_gdi_fallback(
                    f"Output {self.output_index} does not support IDXGIOutput1 (E_NOINTERFACE)"
                )

            feature_level = ctypes.c_uint()
            res = self.d3d11.D3D11CreateDevice(
                adapter,
                D3D_DRIVER_TYPE_UNKNOWN,
                None,
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None,
                0,
                D3D11_SDK_VERSION,
                ctypes.byref(self.device),
                ctypes.byref(feature_level),
                ctypes.byref(self.context),
            )
            if res != 0 or not self.device.value:
                self._call_vtbl(output1, 2)
                self._call_vtbl(output, 2)
                self._call_vtbl(adapter, 2)
                self._call_vtbl(factory, 2)
                return self._enable_gdi_fallback("D3D11CreateDevice failed")

            res = self._call_vtbl(
                output1, 22, self.device, ctypes.byref(self.duplication)
            )
            if res != 0 or not self.duplication.value:
                self._call_vtbl(self.context, 2)
                self._call_vtbl(self.device, 2)
                self._call_vtbl(output1, 2)
                self._call_vtbl(output, 2)
                self._call_vtbl(adapter, 2)
                self._call_vtbl(factory, 2)
                return self._enable_gdi_fallback(
                    f"DuplicateOutput failed on output {self.output_index}"
                )

            self._call_vtbl(factory, 2)
            self._call_vtbl(adapter, 2)
            self._call_vtbl(output, 2)
            self._call_vtbl(output1, 2)

            self._is_initialized = True
            logger.info("DXGI Desktop Duplication API fully initialized.")
            return True

        except Exception as err:
            return self._enable_gdi_fallback(f"DXGI Exception: {err}")

    def _enable_gdi_fallback(self, reason: str) -> bool:
        """Enables high-performance GDI window capture fallback engine."""
        logger.warning(
            f"DXGI duplication bypass ({reason}). Switching to GDI Window Capture Engine."
        )
        self._use_gdi_fallback = True
        self._is_initialized = True
        return True

    def capture_window_vram(
        self, hwnd: int, timeout_ms: int = 16
    ) -> Optional[np.ndarray]:
        """Captures window frame buffer using DXGI GPU duplication or GDI window render."""
        if not self._is_initialized:
            if not self.auto_select_by_hwnd(hwnd) or not self.initialize_dxgi():
                return None

        if self._use_gdi_fallback:
            return self._capture_gdi_window(hwnd)

        win_rect = RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            return self._capture_gdi_window(hwnd)

        display_left, display_top, display_right, display_bottom = self.desktop_rect
        display_w = display_right - display_left
        display_h = display_bottom - display_top

        local_left = win_rect.left - display_left
        local_top = win_rect.top - display_top
        local_right = win_rect.right - display_left
        local_bottom = win_rect.bottom - display_top

        local_left = max(0, min(display_w, local_left))
        local_top = max(0, min(display_h, local_top))
        local_right = max(local_left + 1, min(display_w, local_right))
        local_bottom = max(local_top + 1, min(display_h, local_bottom))

        frame = self.capture_roi_vram(
            (local_left, local_top, local_right, local_bottom), timeout_ms
        )
        return frame if frame is not None else self._capture_gdi_window(hwnd)

    def _capture_gdi_window(self, hwnd: int) -> Optional[np.ndarray]:
        """High-speed GDI fallback window capture using PrintWindow and GetDIBits."""
        rect = RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = self.user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None

        m_dc = self.gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = self.gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
        self.gdi32.SelectObject(m_dc, bmp)

        # PW_RENDERFULLCONTENT = 2
        res = self.user32.PrintWindow(hwnd, m_dc, 2)
        if not res:
            self.gdi32.BitBlt(m_dc, 0, 0, width, height, hwnd_dc, 0, 0, 0x00CC0020)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        buffer = ctypes.create_string_buffer(width * height * 4)
        self.gdi32.GetDIBits(m_dc, bmp, 0, height, buffer, ctypes.byref(bmi), 0)

        self.gdi32.DeleteObject(bmp)
        self.gdi32.DeleteDC(m_dc)
        self.user32.ReleaseDC(hwnd, hwnd_dc)

        frame_bgra = np.frombuffer(buffer, dtype=np.uint8).reshape((height, width, 4))
        return np.ascontiguousarray(frame_bgra[:, :, :3][..., ::-1])

    def _create_staging_texture(
        self, width: int, height: int, source_format: int
    ) -> None:
        """Allocates CPU-readable staging texture matching target ROI dimensions."""
        if self.staging_texture:
            self._call_vtbl(self.staging_texture, 2)

        desc = D3D11_TEXTURE2D_DESC()
        desc.Width = width
        desc.Height = height
        desc.MipLevels = 1
        desc.ArraySize = 1
        desc.Format = source_format
        desc.SampleDesc_Count = 1
        desc.SampleDesc_Quality = 0
        desc.Usage = D3D11_USAGE_STAGING
        desc.BindFlags = 0
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
        desc.MiscFlags = 0

        self.roi_width = width
        self.roi_height = height

        res = self._call_vtbl(
            self.device,
            5,
            ctypes.byref(desc),
            None,
            ctypes.byref(self.staging_texture),
        )
        if res != 0:
            raise RuntimeError(f"Failed to create CPU Staging Texture: {hex(res)}")

    def capture_roi_vram(
        self, local_bounds: Tuple[int, int, int, int], timeout_ms: int = 16
    ) -> Optional[np.ndarray]:
        """Performs GPU hardware sub-resource box cropping via Direct3D 11."""
        left, top, right, bottom = local_bounds
        target_width = right - left
        target_height = bottom - top

        if target_width <= 0 or target_height <= 0:
            return None

        frame_info = DXGI_OUTDUPL_FRAME_INFO()
        desktop_resource = ctypes.c_void_p()

        res = self._call_vtbl(
            self.duplication,
            8,
            timeout_ms,
            ctypes.byref(frame_info),
            ctypes.byref(desktop_resource),
        )

        if res != 0 or not desktop_resource.value:
            return None

        try:
            tex_surface = ctypes.c_void_p()
            self._call_vtbl(
                desktop_resource,
                0,
                ctypes.byref(IID_ID3D11Texture2D),
                ctypes.byref(tex_surface),
            )

            src_desc = D3D11_TEXTURE2D_DESC()
            self._call_vtbl(tex_surface, 10, ctypes.byref(src_desc))

            if (
                not self.staging_texture
                or self.roi_width != target_width
                or self.roi_height != target_height
            ):
                self._create_staging_texture(
                    target_width, target_height, src_desc.Format
                )

            box = D3D11_BOX()
            box.left = left
            box.top = top
            box.front = 0
            box.right = right
            box.bottom = bottom
            box.back = 1

            self._call_vtbl(
                self.context,
                46,
                self.staging_texture,
                0,
                0,
                0,
                0,
                tex_surface,
                0,
                ctypes.byref(box),
            )

            mapped_res = D3D11_MAPPED_SUBRESOURCE()
            self._call_vtbl(
                self.context,
                14,
                self.staging_texture,
                0,
                D3D11_MAP_READ,
                0,
                ctypes.byref(mapped_res),
            )

            buffer_size = mapped_res.RowPitch * target_height
            raw_ptr = ctypes.cast(
                mapped_res.pData, ctypes.POINTER(ctypes.c_uint8 * buffer_size)
            )

            frame_bgra = np.frombuffer(raw_ptr.contents, dtype=np.uint8).reshape(
                (target_height, mapped_res.RowPitch // 4, 4)
            )
            frame_rgb = frame_bgra[:target_height, :target_width, :3][..., ::-1]

            self._call_vtbl(self.context, 15, self.staging_texture, 0)
            return np.ascontiguousarray(frame_rgb)

        finally:
            self._call_vtbl(self.duplication, 14)
            self._call_vtbl(desktop_resource, 2)

    @staticmethod
    def _call_vtbl(interface_ptr: ctypes.c_void_p, index: int, *args) -> int:
        """Executes a COM interface method via its virtual method table (vtable) pointer."""
        if not interface_ptr or not interface_ptr.value:
            raise ValueError("Attempted COM vtable call on NULL interface pointer.")

        vtable_ptr = ctypes.cast(
            interface_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        )
        method_ptr = vtable_ptr.contents[index]

        arg_types = [ctypes.c_void_p]
        for arg in args:
            if isinstance(arg, int):
                arg_types.append(ctypes.c_uint64)
            elif hasattr(type(arg), "from_param"):
                arg_types.append(type(arg))
            else:
                arg_types.append(ctypes.c_void_p)

        prototype = ctypes.WINFUNCTYPE(ctypes.c_int32, *arg_types)
        func = prototype(method_ptr)
        return func(interface_ptr, *args)
