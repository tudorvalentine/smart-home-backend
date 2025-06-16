# backend/main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, status, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from typing import List
import json
import logging

app = FastAPI()

# --- Logging настроим базово ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pump-backend")


@app.middleware("http")
async def validate_and_handle_errors(request: Request, call_next):
    max_body = 2 * 1024
    body = await request.body()
    if len(body) > max_body:
        return JSONResponse(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            content={"error": "Payload too large"},
        )

    try:
        response = await call_next(request)
    except RequestValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "Validation error", "details": exc.errors()},
        )
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail},
        )
    except Exception as exc:
        logger.exception("Unexpected error:")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "Internal server error"},
        )
    return response


# --- CORS и сжатие ответов ---
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["https://your-react-app.domain"],  # разрешить только ваш фронт
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)


class ConnectionManager:
    def __init__(self):
        self.clients: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)
        logger.info(f"WebSocket connected: {ws.client}")

    def disconnect(self, ws: WebSocket):
        self.clients.remove(ws)
        logger.info(f"WebSocket disconnected: {ws.client}")

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        for ws in self.clients:
            try:
                await ws.send_text(data)
            except:
                logger.warning("Failed to send to ws client")


esp_manager = ConnectionManager()
client_manager = ConnectionManager()


class PumpStatus(BaseModel):
    physical_switch: bool
    motor_state: bool
    remaining_time: int = Field(..., ge=0, lt=24*3600, description="Remaining seconds, 0–86400")


class PumpTimerRequest(BaseModel):
    hours: int = Field(..., ge=0, le=23)
    minutes: int = Field(..., ge=0, le=59)


@app.websocket("/ws/esp")
async def esp_ws(ws: WebSocket):
    await esp_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        esp_manager.disconnect(ws)


@app.websocket("/ws/client")
async def client_ws(ws: WebSocket):
    await client_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        client_manager.disconnect(ws)


@app.post("/pump/toggle")
async def pump_toggle():
    await esp_manager.broadcast({"action": "TOGGLE"})
    return {"status": "ok"}


@app.post("/pump/off")
async def pump_off():
    await esp_manager.broadcast({"action": "OFF"})
    return {"status": "ok"}


@app.post("/pump/timer")
async def pump_timer(req: PumpTimerRequest):
    await esp_manager.broadcast({"action": "TIMER", "hours": req.hours, "minutes": req.minutes})
    return {"status": "ok"}


@app.post("/pump/status", status_code=200)
async def pump_status(status: PumpStatus):
    """
    ESP POSTs JSON со статусом:
      { physical_switch: bool, motor_state: bool, remaining_time: int }
    Сервер ретранслирует всем UI-клиентам.
    """
    await client_manager.broadcast(status.dict())
    return {"status": "ok"}


_last_status: PumpStatus = PumpStatus(physical_switch=False, motor_state=False, remaining_time=0)

@app.get("/pump/status", response_model=PumpStatus)
async def get_pump_status():
    return _last_status


@app.post("/pump/status", status_code=200)
async def pump_status(status: PumpStatus):
    global _last_status
    _last_status = status
    await client_manager.broadcast(status.dict())
    return {"status": "ok"}

