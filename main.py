import socket
import qrcode
import webbrowser
import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import uvicorn
from database import init_db, save_message, get_history
import threading

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    
    yield
    # Shutdown
    pass

app = FastAPI(lifespan=lifespan)

# Create directories if they don't exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("templates", exist_ok=True)
os.makedirs("static/css", exist_ok=True)
os.makedirs("static/js", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")

# In-memory store for connected users
connected_users = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket, username: str):
        await websocket.accept()
        self.active_connections.append(websocket)
        connected_users[username] = True
        await self.broadcast_user_list()

    def disconnect(self, websocket: WebSocket, username: str):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if username in connected_users:
            del connected_users[username]

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

    async def broadcast_user_list(self):
        await self.broadcast({"type": "presence", "users": list(connected_users.keys())})

manager = ConnectionManager()

@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.websocket("/ws/{username}")
async def websocket_endpoint(websocket: WebSocket, username: str):
    await manager.connect(websocket, username)
    try:
        # Buffer: Send only the last 5 minutes of chat history to cover spotty disconnections
        history = await get_history()
        await websocket.send_json({"type": "history", "messages": history})
        
        while True:
            data = await websocket.receive_text()
            await save_message(username, data, "text")
            
            # Broadcast only the minimalist live message
            await manager.broadcast({
                "type": "message",
                "username": username,
                "content": data,
                "msg_type": "text"
            })
    except WebSocketDisconnect:
        manager.disconnect(websocket, username)
        await manager.broadcast_user_list()

@app.post("/upload")
async def upload_file(file: UploadFile = File(...), username: str = Form(...)):
    if file.content_type not in ["image/jpeg", "image/png", "application/pdf"]:
        raise HTTPException(status_code=400, detail="Invalid file type")
    
    file_size = 0
    file_path = f"uploads/{file.filename}"
    with open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024): # Read in chunks
            file_size += len(chunk)
            if file_size > 25 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="File too large")
            f.write(chunk)
            
    await save_message(username, file.filename, "file")
    await manager.broadcast({
        "type": "message",
        "username": username,
        "content": file.filename,
        "msg_type": "file"
    })
    return {"filename": file.filename}

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    import sys
    import os
    import subprocess
    
    # If not run with --run flag, spawn a dedicated, visible CMD window
    if "--run" not in sys.argv:
        script_path = os.path.abspath(__file__)
        # Use subprocess to open a new native console
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
        subprocess.Popen([sys.executable, script_path, "--run"], creationflags=flags)
        sys.exit(0)

    # Set console title
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW("AirChat Server")

    # --- SERVER STARTUP LOGIC ---
    local_ip = get_local_ip()
    
    # Highly unique, completely STATIC port! 
    # This guarantees your URL will NEVER randomly change, and it won't conflict with standard apps.
    port = 9999
        
    url = f"http://{local_ip}:{port}"
    
    # Standard ASCII console output (prevents Windows cp1252 crash)
    print("\n" + "="*65)
    print("=                                                               =")
    print("=                AIRCHAT LOCAL SERVER IS LIVE                   =")
    print("=                                                               =")
    print("="*65 + "\n")
    print("   Connect from ANY device on your Wi-Fi using this IP:\n")
    print(f"                 --->   {url}   <--- \n")
    print("-" * 65)
    print("   WARNING: DO NOT CLOSE THIS WINDOW!")
    print("   Closing this specific window will shut down the chat room.")
    print("-" * 65 + "\n")
    
    try:
        print("   Scan this QR code with your phone to connect:\n")
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        print("-" * 65 + "\n")
    except Exception as e:
        print(f"   Could not generate QR code: {e}")

    try:
        # Windows specific chrome launch
        chrome_path = 'C:/Program Files/Google/Chrome/Application/chrome.exe %s'
        webbrowser.get(chrome_path).open(url)
    except Exception as e:
        print(f"Could not launch Chrome automatically: {e}")
        webbrowser.open(url)

    # Keep the console open if there's a fatal server error
    try:
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
    except Exception as e:
        print(f"\nSERVER ERROR: {e}")
        input("Press Enter to close...")
