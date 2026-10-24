import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Dict, Any, List, Optional

import cv2
from fastapi import (  # type: ignore
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    HTTPException,
    status,
    Depends,
    Security,
    Query,
)
from fastapi.responses import HTMLResponse, JSONResponse  # type: ignore
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials  # type: ignore
import jwt  # type: ignore
import numpy as np
import uvicorn  # type: ignore

# Import supervisor using multi-tier fallback module resolver with type checker suppression
try:
    from supervisor.multi_instance_supervisor import MultiInstanceSupervisor
except ImportError:
    try:
        from ..supervisor.multi_instance_supervisor import MultiInstanceSupervisor  # type: ignore
    except ImportError:
        try:
            from .multi_instance_supervisor import MultiInstanceSupervisor  # type: ignore
        except ImportError:
            from multi_instance_supervisor import MultiInstanceSupervisor  # type: ignore

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("DashboardServer")

# --- SECURITY CONFIGURATION ---
SECRET_KEY: str = "d8f3a1e9c2b4f7a6e5d8c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0"
ALGORITHM: str = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

VALID_API_KEYS: set = {"mumu-bot-admin-key-2026", "mumu-bot-node-key-8891"}
ADMIN_USER: str = "admin"
ADMIN_PASSWORD: str = "ragnarok2026"

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_security = HTTPBearer(auto_error=False)

app = FastAPI(title="Secured MuMuPlayer 12 Telemetry Dashboard")

supervisor_instance: Optional[MultiInstanceSupervisor] = None


# --- AUTHENTICATION & AUTHORIZATION HELPERS ---


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generates a signed JWT access token with an expiration claim."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (
        expires_delta
        if expires_delta
        else timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "iat": now})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_jwt_token(token: str) -> dict:
    """Decodes and validates JWT token signature and expiration claims."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="JWT token has expired."
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate JWT signature.",
        )


async def verify_auth(
    api_key: Optional[str] = Security(api_key_header),
    auth_credentials: Optional[HTTPAuthorizationCredentials] = Security(
        bearer_security
    ),
) -> Dict[str, Any]:
    """Middleware dependency enforcing dual API Key or Bearer JWT token verification for REST endpoints."""
    if api_key and api_key in VALID_API_KEYS:
        return {"identity": "API_KEY_CLIENT", "auth_type": "api_key"}

    if auth_credentials and auth_credentials.scheme.lower() == "bearer":
        payload = decode_jwt_token(auth_credentials.credentials)
        return {
            "identity": payload.get("sub", "authenticated_user"),
            "auth_type": "jwt",
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing authentication credentials (API Key or JWT Bearer Token required).",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_websocket_auth(
    token: Optional[str] = Query(None), api_key: Optional[str] = Query(None)
) -> bool:
    """Validates WebSocket connection handshakes via URL query parameters (?token= or ?api_key=)."""
    if api_key and api_key in VALID_API_KEYS:
        return True

    if token:
        try:
            decode_jwt_token(token)
            return True
        except HTTPException:
            return False

    return False


class ConnectionManager:
    """Manages authenticated active WebSocket streaming sessions."""

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)


manager = ConnectionManager()


def encode_frame_to_base64_jpg(frame: np.ndarray, quality: int = 45) -> str:
    """Compresses RGB frame buffer to Base64 JPEG string for network transport."""
    if frame is None or frame.size == 0:
        return ""
    bgr_frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    success, buffer = cv2.imencode(".jpg", bgr_frame, encode_param)
    return base64.b64encode(buffer).decode("utf-8") if success else ""


# --- REST ENDPOINTS ---


@app.post("/api/login")
async def login_endpoint(credentials: Dict[str, str]) -> Dict[str, str]:
    """Authenticates admin user credentials and issues a signed JWT access token."""
    username = credentials.get("username")
    password = credentials.get("password")

    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        access_token = create_access_token(data={"sub": username, "role": "admin"})
        return {"access_token": access_token, "token_type": "bearer"}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password.",
    )


@app.get("/", response_class=HTMLResponse)
async def get_dashboard_html() -> str:
    """Serves the secured single-page web dashboard application."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Secured Control Center - MuMuPlayer 12</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background-color: #0f172a; color: #f8fafc; font-family: monospace; }
        .card { background-color: #1e293b; border: 1px solid #334155; }
        .metric-bar { transition: width 0.3s ease; }
    </style>
</head>
<body class="p-6">
    <header class="mb-8 flex justify-between items-center border-b border-slate-700 pb-4">
        <div>
            <h1 class="text-2xl font-bold text-sky-400">MuMuPlayer 12 Control Center</h1>
            <p class="text-sm text-slate-400">Secured DXGI Telemetry & Instance Recovery System</p>
        </div>
        <div class="flex items-center gap-3">
            <input id="api-key-input" type="password" placeholder="Enter API Key or JWT" class="px-3 py-1 bg-slate-800 border border-slate-600 rounded text-xs text-slate-200 focus:outline-none focus:border-sky-500" value="mumu-bot-admin-key-2026"/>
            <button onclick="reconnect()" class="px-3 py-1 bg-sky-600 hover:bg-sky-500 rounded text-xs font-semibold">Connect</button>
            <div id="status-indicator" class="px-3 py-1 rounded bg-rose-900/50 text-rose-300 text-xs font-semibold border border-rose-700">
                DISCONNECTED
            </div>
        </div>
    </header>

    <div id="instances-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"></div>

    <script>
        const grid = document.getElementById('instances-grid');
        const statusIndicator = document.getElementById('status-indicator');
        const apiKeyInput = document.getElementById('api-key-input');
        let socket;

        function reconnect() {
            if (socket) socket.close();
            connectWebSocket();
        }

        function connectWebSocket() {
            const authSecret = apiKeyInput.value.trim();
            if (!authSecret) {
                alert("Please provide an API Key or JWT token.");
                return;
            }

            const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            const paramKey = authSecret.startsWith('eyJ') ? 'token' : 'api_key';
            const wsUrl = `${wsProtocol}//${window.location.host}/ws/telemetry?${paramKey}=${encodeURIComponent(authSecret)}`;
            
            socket = new WebSocket(wsUrl);

            socket.onopen = () => {
                statusIndicator.textContent = "SYSTEM LIVE";
                statusIndicator.className = "px-3 py-1 rounded bg-emerald-900/50 text-emerald-300 text-xs font-semibold border border-emerald-700";
            };

            socket.onmessage = (event) => {
                const data = JSON.parse(event.data);
                renderInstances(data);
            };

            socket.onclose = (event) => {
                statusIndicator.textContent = event.code === 1008 ? "AUTH FAILED" : "DISCONNECTED";
                statusIndicator.className = "px-3 py-1 rounded bg-rose-900/50 text-rose-300 text-xs font-semibold border border-rose-700";
            };
        }

        async function triggerRestart(idx) {
            const authSecret = apiKeyInput.value.trim();
            if (!confirm(`Confirm restart order for Instance ${idx}?`)) return;

            const headers = {};
            if (authSecret.startsWith('eyJ')) {
                headers['Authorization'] = `Bearer ${authSecret}`;
            } else {
                headers['X-API-Key'] = authSecret;
            }

            try {
                const res = await fetch(`/api/instances/${idx}/restart`, { method: 'POST', headers });
                const json = await res.json();
                if (res.ok) alert(json.message);
                else alert(`Error: ${json.detail}`);
            } catch (err) {
                alert(`Request failed: ${err}`);
            }
        }

        function renderInstances(payload) {
            payload.instances.forEach(inst => {
                let card = document.getElementById(`card-inst-${inst.index}`);
                if (!card) {
                    card = document.createElement('div');
                    card.id = `card-inst-${inst.index}`;
                    card.className = "card rounded-lg p-4 flex flex-col justify-between shadow-xl";
                    grid.appendChild(card);
                }

                const statusColor = inst.responsive ? "text-emerald-400" : "text-rose-400";
                const frameSrc = inst.frame_base64 ? `data:image/jpeg;base64,${inst.frame_base64}` : '';

                card.innerHTML = `
                    <div>
                        <div class="flex justify-between items-center mb-3">
                            <span class="font-bold text-lg text-slate-200">Instance #${inst.index}</span>
                            <span class="text-xs font-bold ${statusColor}">● ${inst.responsive ? 'RUNNING' : 'STALLED'}</span>
                        </div>
                        <div class="aspect-video bg-black rounded mb-4 overflow-hidden border border-slate-700 relative">
                            ${frameSrc ? `<img src="${frameSrc}" class="w-full h-full object-cover"/>` : `<div class="flex items-center justify-center h-full text-slate-600 text-xs">NO DXGI FRAME</div>`}
                        </div>
                        <div class="space-y-2 text-xs mb-4">
                            <div>
                                <div class="flex justify-between text-slate-400 mb-1"><span>CPU Usage</span><span>${inst.cpu}%</span></div>
                                <div class="w-full bg-slate-700 h-1.5 rounded overflow-hidden"><div class="bg-sky-500 h-full metric-bar" style="width: ${Math.min(inst.cpu, 100)}%"></div></div>
                            </div>
                            <div>
                                <div class="flex justify-between text-slate-400 mb-1"><span>RAM Memory</span><span>${inst.ram_mb} MB</span></div>
                                <div class="w-full bg-slate-700 h-1.5 rounded overflow-hidden"><div class="bg-indigo-500 h-full metric-bar" style="width: ${Math.min((inst.ram_mb / 4096) * 100, 100)}%"></div></div>
                            </div>
                            <div class="flex justify-between text-slate-400 pt-1 border-t border-slate-700/50">
                                <span>Serial: <code class="text-slate-300">${inst.serial}</code></span>
                                <span>System VRAM: <code class="text-slate-300">${inst.vram_mb} MB</code></span>
                            </div>
                        </div>
                    </div>
                    <button onclick="triggerRestart(${inst.index})" class="w-full py-2 bg-rose-600/20 hover:bg-rose-600/40 text-rose-300 border border-rose-500/40 rounded text-xs font-semibold transition">
                        RESTART INSTANCE
                    </button>
                `;
            });
        }

        connectWebSocket();
    </script>
</body>
</html>
    """


@app.websocket("/ws/telemetry")
async def websocket_telemetry_stream(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    api_key: Optional[str] = Query(None),
) -> None:
    """Authenticates WebSocket connection during handshake and streams live DXGI telemetry."""
    is_authorized = await verify_websocket_auth(token=token, api_key=api_key)
    if not is_authorized:
        logger.warning(
            f"Unauthorized WebSocket connection attempt from {websocket.client}"
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)

    try:
        while True:
            if supervisor_instance is None:
                await asyncio.sleep(1.0)
                continue

            instance_payloads = []
            for idx, metrics in list(supervisor_instance.instances.items()):
                raw_frame = supervisor_instance.capture_instance_frame(idx)
                base64_jpg = (
                    encode_frame_to_base64_jpg(raw_frame, quality=45)
                    if raw_frame is not None
                    else ""
                )

                instance_payloads.append(
                    {
                        "index": idx,
                        "serial": metrics.serial,
                        "responsive": metrics.is_responsive,
                        "cpu": metrics.cpu_percent,
                        "ram_mb": metrics.ram_usage_mb,
                        "vram_mb": metrics.vram_usage_mb,
                        "frame_base64": base64_jpg,
                    }
                )

            telemetry_data = {
                "timestamp": datetime.now(timezone.utc).timestamp(),
                "instances": instance_payloads,
            }

            await websocket.send_text(json.dumps(telemetry_data))
            await asyncio.sleep(0.10)  # Stream rate limit ~10 FPS

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as err:
        logger.error(f"WebSocket telemetry error: {err}")
        manager.disconnect(websocket)


@app.post("/api/instances/{instance_index}/restart")
async def restart_instance_endpoint(
    instance_index: int, auth_context: Dict[str, Any] = Depends(verify_auth)
) -> Dict[str, Any]:
    """Secured REST endpoint for manual instance recovery actions."""
    if supervisor_instance is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supervisor service is uninitialized.",
        )

    if instance_index not in supervisor_instance.instances:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Instance index {instance_index} not found.",
        )

    logger.info(
        f"Authenticated restart order for Instance {instance_index} "
        f"issued by [{auth_context['identity']}] via {auth_context['auth_type']}."
    )
    asyncio.create_task(supervisor_instance.restart_instance(instance_index))

    return {
        "status": "ACCEPTED",
        "message": f"Instance {instance_index} restart sequence initiated.",
        "authenticated_as": auth_context["identity"],
    }


class DashboardServer:
    """Service controller running the secured FastAPI web server via Uvicorn."""

    def __init__(
        self,
        supervisor: MultiInstanceSupervisor,
        host: str = "0.0.0.0",
        port: int = 8000,
    ) -> None:
        global supervisor_instance
        supervisor_instance = supervisor
        self.host: str = host
        self.port: int = port
        self.config: uvicorn.Config = uvicorn.Config(
            app=app, host=self.host, port=self.port, log_level="warning"
        )
        self.server: uvicorn.Server = uvicorn.Server(self.config)

    async def start(self) -> None:
        """Starts Uvicorn server non-blockingly inside the active event loop."""
        logger.info(
            f"Starting Secured Telemetry Dashboard on http://{self.host}:{self.port}"
        )
        await self.server.serve()


if __name__ == "__main__":
    mock_supervisor = MultiInstanceSupervisor()
    dashboard = DashboardServer(supervisor=mock_supervisor, host="127.0.0.1", port=8000)
    asyncio.run(dashboard.start())
