from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
import json
from typing import Dict

app = FastAPI(title="CCTS Realtime Tracker")
templates = Jinja2Templates(directory="templates")

# Lưu trữ các kết nối từ trình duyệt đang xem bản đồ (Dashboard)
class ConnectionManager:
    def __init__(self):
        self.viewers: list[WebSocket] = []

    async def connect_viewer(self, websocket: WebSocket):
        await websocket.accept()
        self.viewers.append(websocket)

    def disconnect_viewer(self, websocket: WebSocket):
        self.viewers.remove(websocket)

    async def broadcast_location(self, location_data: str):
        for viewer in self.viewers:
            try:
                await viewer.send_text(location_data)
            except:
                pass

manager = ConnectionManager()

# 1. Endpoint phục vụ giao diện bản đồ cho người quản lý
@app.get("/map", response_class=HTMLResponse)
async def get_map(request: Request):
    return templates.TemplateResponse("map.html", {"request": request})

# 2. Endpoint WebSocket cho người xem bản đồ (Quản lý)
@app.websocket("/ws/viewer")
async def websocket_viewer(websocket: WebSocket):
    await manager.connect_viewer(websocket)
    try:
        while True:
            await websocket.receive_text() # Giữ kết nối
    except WebSocketDisconnect:
        manager.disconnect_viewer(websocket)

# 3. Endpoint WebSocket nhận dữ liệu từ nhân viên (từ app.py)
@app.websocket("/ws/tracker")
async def websocket_tracker(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            # Ngay khi nhận được tọa độ, phát sóng luôn cho các viewer
            await manager.broadcast_location(data)
    except WebSocketDisconnect:
        pass