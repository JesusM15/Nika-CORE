"""
main.py — Servidor Central de Nika OS
======================================
FastAPI como backbone del sistema: sirve el dashboard, expone la API REST,
gestiona WebSockets para actualizaciones en tiempo real y coordina
los módulos MQTT, la base de datos y el orquestador de voz.

Arquitectura de arranque (lifespan):
  1. init_db()          → crea tablas SQLite si no existen
  2. MQTTManager        → conecta al broker Mosquitto
  3. Orchestrator       → instancia el cerebro central
  4. wake_word.py       → lanza como subprocess independiente
  5. _heartbeat_loop()  → tarea async periódica (escaneo de dispositivos)

Endpoints principales:
  GET  /              → Sirve dashboard/index.html
  WS   /ws            → WebSocket para actualizaciones en tiempo real
  GET  /api/modes     → Lista todos los modos
  POST /api/modes     → Crea un nuevo modo
  PUT  /api/modes/{id}→ Actualiza un modo
  DEL  /api/modes/{id}→ Elimina un modo
  POST /api/modes/{id}/activate → Activa un modo (lanza sus apps)
  GET  /api/devices   → Lista dispositivos descubiertos
  POST /api/devices/scan → Escanea la red vía MQTT
  GET  /api/settings  → Obtiene configuración
  PUT  /api/settings  → Actualiza configuración
  POST /api/voice/command → Recibe comando de voz del wake_word.py
  POST /api/voice/state   → Actualiza estado del micrófono
  GET  /api/status    → Health check
"""

import os
import sys
import json
import asyncio
import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Cargar .env desde la raíz del proyecto
ROOT_DIR = Path(__file__).parent
load_dotenv(dotenv_path=ROOT_DIR / ".env")

# ── Setup de logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nika.main")

# ── Imports de módulos propios ────────────────────────────────────────────────
from database import init_db, get_db, AsyncSessionLocal, Mode, ModeApp, Device, Setting
from scripts.mqtt_manager import MQTTManager
from scripts.orchestrator import Orchestrator
from sqlalchemy import select, delete as sa_delete


# ══════════════════════════════════════════════════════
#  GESTIÓN DE CONEXIONES WEBSOCKET
# ══════════════════════════════════════════════════════

class ConnectionManager:
    """
    Gestiona múltiples conexiones WebSocket concurrentes del dashboard.
    Thread-safe para uso con paho-mqtt (que opera en un hilo separado).
    """

    def __init__(self):
        self.active: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.active.append(ws)
        logger.info(f"[WS] + Conexión. Total activas: {len(self.active)}")

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            if ws in self.active:
                self.active.remove(ws)
        logger.info(f"[WS] - Desconexión. Total activas: {len(self.active)}")

    async def broadcast(self, data: dict):
        """
        Envía un mensaje JSON a TODOS los clientes WebSocket conectados.
        Elimina silenciosamente las conexiones rotas.
        """
        if not self.active:
            return

        msg  = json.dumps(data, ensure_ascii=False, default=str)
        dead = []

        for ws in list(self.active):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)

        # Limpiar conexiones muertas
        async with self._lock:
            for ws in dead:
                if ws in self.active:
                    self.active.remove(ws)


ws_manager = ConnectionManager()


# ══════════════════════════════════════════════════════
#  LIFESPAN: ARRANQUE Y APAGADO DEL SERVIDOR
# ══════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Contexto de vida de la aplicación.
    Todo lo que está ANTES del 'yield' se ejecuta al arrancar.
    Todo lo que está DESPUÉS se ejecuta al apagar.
    """
    logger.info("=" * 60)
    logger.info("  🎙️  Nika OS — Iniciando sistema")
    logger.info("=" * 60)

    # ── 1. Inicializar base de datos ──────────────────────────────────────────
    await init_db()
    logger.info("[Main] ✓ Base de datos lista.")

    # ── 2. Configurar y conectar MQTT ─────────────────────────────────────────
    mqtt = MQTTManager.get_instance()

    # Inyectar función de broadcast WebSocket en el MQTTManager.
    # NOTA: paho-mqtt llama a los callbacks desde un hilo separado.
    # Usamos call_soon_threadsafe para programar la corrutina en el event loop
    # del hilo principal de asyncio (donde corre FastAPI/uvicorn).
    loop = asyncio.get_event_loop()

    def thread_safe_broadcast(data: dict):
        """Wrapper thread-safe para llamar a ws_manager.broadcast desde hilo de paho."""
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(data), loop)

    mqtt.set_ws_broadcast(thread_safe_broadcast)
    mqtt.connect()
    logger.info("[Main] ✓ MQTT Manager configurado.")

    # ── 3. Inicializar orquestador ────────────────────────────────────────────
    app.state.orchestrator = Orchestrator(
        mqtt_manager=mqtt,
        db_session_factory=AsyncSessionLocal,
    )
    logger.info("[Main] ✓ Orquestador de voz listo.")

    # ── 4. Lanzar wake_word.py como proceso independiente ─────────────────────
    wake_script = ROOT_DIR / "scripts" / "wake_word.py"
    app.state.wake_proc = None

    if wake_script.exists():
        try:
            app.state.wake_proc = subprocess.Popen(
                [sys.executable, str(wake_script)],
                # En producción, podrías querer redirigir stdout a un archivo de log
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT_DIR),
            )
            logger.info(f"[Main] ✓ Wake word detector PID: {app.state.wake_proc.pid}")
        except Exception as e:
            logger.warning(f"[Main] ⚠ No se pudo iniciar wake_word.py: {e}")
            logger.warning("       El sistema funcionará sin detección de voz.")
    else:
        logger.warning(f"[Main] ⚠ wake_word.py no encontrado en: {wake_script}")

    # ── 5. Tareas de background ───────────────────────────────────────────────
    app.state.heartbeat_task = asyncio.create_task(
        _heartbeat_loop(mqtt),
        name="nika-heartbeat",
    )
    logger.info("[Main] ✓ Heartbeat de dispositivos iniciado.")
    logger.info(f"[Main] 🚀 Servidor listo en http://localhost:{os.getenv('API_PORT', '8000')}")

    yield   # ← El servidor corre aquí ←

    # ══════════════════════════════════════════════════
    #  APAGADO LIMPIO
    # ══════════════════════════════════════════════════
    logger.info("[Main] 🛑 Iniciando apagado...")

    # Cancelar heartbeat
    app.state.heartbeat_task.cancel()
    try:
        await app.state.heartbeat_task
    except asyncio.CancelledError:
        pass

    # Detener wake_word.py
    if app.state.wake_proc and app.state.wake_proc.poll() is None:
        app.state.wake_proc.terminate()
        try:
            app.state.wake_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.state.wake_proc.kill()
        logger.info("[Main] Wake word detector detenido.")

    # Desconectar MQTT
    mqtt.disconnect()
    logger.info("[Main] Sistema Nika apagado correctamente.")


async def _heartbeat_loop(mqtt: MQTTManager):
    """
    Tarea async periódica:
      - Escanea dispositivos vía MQTT ping cada 30 segundos
      - Broadcast del estado al dashboard
    Corre indefinidamente hasta ser cancelada.
    """
    await asyncio.sleep(5.0)    # Esperar un poco al arrancar
    while True:
        try:
            mqtt.scan_devices()
            devices = mqtt.get_devices()
            await ws_manager.broadcast({
                "event":   "heartbeat",
                "devices": devices,
                "mqtt_ok": mqtt.connected,
            })
        except Exception as e:
            logger.debug(f"[Heartbeat] Error: {e}")

        await asyncio.sleep(30)


# ══════════════════════════════════════════════════════
#  INICIALIZACIÓN DE LA APP FASTAPI
# ══════════════════════════════════════════════════════

app = FastAPI(
    title="Nika OS API",
    description="Backend centralizado del asistente inteligente Nika",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

# CORS: permite requests desde el dashboard (mismo origen en producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Servir archivos estáticos del dashboard ───────────────────────────────────
DASHBOARD_DIR = ROOT_DIR / "dashboard"

if (DASHBOARD_DIR / "css").exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR / "css")), name="static-css")

if (DASHBOARD_DIR / "js").exists():
    app.mount("/js", StaticFiles(directory=str(DASHBOARD_DIR / "js")), name="static-js")

if (ROOT_DIR / "static").exists():
    app.mount("/assets", StaticFiles(directory=str(ROOT_DIR / "static")), name="assets")


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    """Sirve el dashboard principal (SPA)."""
    index_path = DASHBOARD_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"message": "Nika OS API corriendo. Dashboard no encontrado."}, status_code=200)


# ══════════════════════════════════════════════════════
#  WEBSOCKET
# ══════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket para actualizaciones en tiempo real al dashboard.

    Eventos que el servidor envía al cliente:
      init              → Estado inicial al conectarse
      heartbeat         → Estado periódico de dispositivos (cada 30s)
      device_discovered → Nuevo dispositivo encontrado
      device_status     → Cambio de estado de dispositivo
      mqtt_connected    → MQTT reconectado
      mqtt_disconnected → MQTT perdió conexión
      mode_created      → Nuevo modo creado
      mode_updated      → Modo modificado
      mode_deleted      → Modo eliminado
      mode_activated    → Modo activado
      voice_command     → Comando de voz procesado
      voice_state       → Estado del micrófono (listening/recording/processing)
      settings_changed  → Configuración actualizada
      theme_changed     → Tema del dashboard cambiado
    """
    await ws_manager.connect(ws)
    try:
        # Enviar snapshot del estado actual al nuevo cliente
        mqtt    = MQTTManager.get_instance()
        devices = mqtt.get_devices()

        await ws.send_text(json.dumps({
            "event":          "init",
            "devices":        devices,
            "mqtt_connected": mqtt.connected,
            "broker":         mqtt.broker,
        }))

        # Loop de keep-alive: responder a pings del cliente
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                msg = json.loads(raw)

                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": asyncio.get_event_loop().time()}))
                elif msg.get("type") == "scan":
                    mqtt.scan_devices()

            except asyncio.TimeoutError:
                # Enviar ping proactivo para mantener la conexión viva
                await ws.send_text(json.dumps({"type": "ping"}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[WS] Error en conexión: {e}")
    finally:
        await ws_manager.disconnect(ws)


# ══════════════════════════════════════════════════════
#  API — MODOS
# ══════════════════════════════════════════════════════

class AppItem(BaseModel):
    app_name:  str
    app_path:  str
    device_id: str

class ModeCreate(BaseModel):
    name:  str = Field(..., min_length=1, max_length=100)
    icon:  str = Field(default="🚀",     max_length=10)
    color: str = Field(default="#7c3aed", max_length=20)
    apps:  List[AppItem] = []

class ModeUpdate(BaseModel):
    name:  Optional[str]        = None
    icon:  Optional[str]        = None
    color: Optional[str]        = None
    apps:  Optional[List[AppItem]] = None


@app.get("/api/modes", summary="Lista todos los modos")
async def list_modes(session=Depends(get_db)):
    result = await session.execute(select(Mode))
    modes  = result.scalars().all()
    return [m.to_dict() for m in modes]


@app.post("/api/modes", status_code=201, summary="Crea un nuevo modo")
async def create_mode(data: ModeCreate, session=Depends(get_db)):
    mode = Mode(name=data.name, icon=data.icon, color=data.color)
    session.add(mode)
    await session.flush()   # Obtener el ID antes del commit

    for app_item in data.apps:
        session.add(ModeApp(
            mode_id=mode.id,
            app_name=app_item.app_name,
            app_path=app_item.app_path,
            device_id=app_item.device_id,
        ))

    await session.commit()
    await session.refresh(mode)
    logger.info(f"[API] Modo creado: '{mode.name}' (id={mode.id})")
    await ws_manager.broadcast({"event": "mode_created", "mode": mode.to_dict()})
    return mode.to_dict()


@app.get("/api/modes/{mode_id}", summary="Obtiene un modo por ID")
async def get_mode(mode_id: int, session=Depends(get_db)):
    mode = await session.get(Mode, mode_id)
    if not mode:
        raise HTTPException(404, f"Modo {mode_id} no encontrado.")
    return mode.to_dict()


@app.put("/api/modes/{mode_id}", summary="Actualiza un modo")
async def update_mode(mode_id: int, data: ModeUpdate, session=Depends(get_db)):
    mode = await session.get(Mode, mode_id)
    if not mode:
        raise HTTPException(404, f"Modo {mode_id} no encontrado.")

    if data.name  is not None: mode.name  = data.name
    if data.icon  is not None: mode.icon  = data.icon
    if data.color is not None: mode.color = data.color

    if data.apps is not None:
        # Reemplazar las apps: borrar las actuales e insertar las nuevas
        await session.execute(sa_delete(ModeApp).where(ModeApp.mode_id == mode_id))
        for app_item in data.apps:
            session.add(ModeApp(
                mode_id=mode_id,
                app_name=app_item.app_name,
                app_path=app_item.app_path,
                device_id=app_item.device_id,
            ))

    await session.commit()
    await session.refresh(mode)
    logger.info(f"[API] Modo actualizado: '{mode.name}' (id={mode_id})")
    await ws_manager.broadcast({"event": "mode_updated", "mode": mode.to_dict()})
    return mode.to_dict()


@app.delete("/api/modes/{mode_id}", summary="Elimina un modo")
async def delete_mode(mode_id: int, session=Depends(get_db)):
    mode = await session.get(Mode, mode_id)
    if not mode:
        raise HTTPException(404, f"Modo {mode_id} no encontrado.")

    name = mode.name
    await session.delete(mode)
    await session.commit()
    logger.info(f"[API] Modo eliminado: '{name}' (id={mode_id})")
    await ws_manager.broadcast({"event": "mode_deleted", "mode_id": mode_id, "name": name})
    return {"message": f"Modo '{name}' eliminado correctamente."}


@app.post("/api/modes/{mode_id}/activate", summary="Activa un modo")
async def activate_mode(mode_id: int, session=Depends(get_db)):
    """Lanza todas las apps del modo en sus dispositivos asignados vía MQTT."""
    mode = await session.get(Mode, mode_id)
    if not mode:
        raise HTTPException(404, f"Modo {mode_id} no encontrado.")

    mqtt     = MQTTManager.get_instance()
    launched = []
    failed   = []

    for app in mode.apps:
        command = {
            "action":    "open_app",
            "app_name":  app.app_name,
            "app_path":  app.app_path,
            "mode_id":   mode.id,
            "mode_name": mode.name,
        }
        if mqtt.publish_command(app.device_id, command):
            launched.append({"app": app.app_name, "device": app.device_id})
        else:
            failed.append({"app": app.app_name, "device": app.device_id})

    logger.info(f"[API] Modo activado: '{mode.name}' | ✓{len(launched)} ✗{len(failed)}")
    await ws_manager.broadcast({
        "event":    "mode_activated",
        "mode_id":  mode_id,
        "name":     mode.name,
        "launched": launched,
        "failed":   failed,
    })

    return {
        "message":  f"Modo '{mode.name}' activado.",
        "launched": launched,
        "failed":   failed,
    }


# ══════════════════════════════════════════════════════
#  API — DISPOSITIVOS
# ══════════════════════════════════════════════════════

@app.get("/api/devices", summary="Lista dispositivos descubiertos")
async def list_devices():
    """Retorna todos los dispositivos en memoria (descubiertos vía MQTT)."""
    mqtt    = MQTTManager.get_instance()
    devices = mqtt.get_devices()
    return {
        "devices":      devices,
        "count":        len(devices),
        "online_count": sum(1 for d in devices.values() if d.get("status") == "online"),
    }


@app.post("/api/devices/scan", summary="Escanea dispositivos via MQTT")
async def scan_devices():
    """
    Envía un ping broadcast y espera 2 segundos para recolectar respuestas.
    Retorna el estado actualizado de dispositivos.
    """
    mqtt = MQTTManager.get_instance()
    mqtt.scan_devices()

    # Esperar brevemente para que lleguen las respuestas pong
    await asyncio.sleep(2.0)

    devices = mqtt.get_devices()
    await ws_manager.broadcast({"event": "scan_complete", "devices": devices})

    return {
        "message":      "Escaneo completado.",
        "devices":      devices,
        "online_count": sum(1 for d in devices.values() if d.get("status") == "online"),
    }


@app.post("/api/devices/{device_id}/command", summary="Envía comando a dispositivo")
async def send_device_command(device_id: str, request: Request):
    """Envía un comando JSON arbitrario a un dispositivo específico."""
    command = await request.json()
    mqtt    = MQTTManager.get_instance()
    success = mqtt.publish_command(device_id, command)

    return {
        "success":   success,
        "device_id": device_id,
        "command":   command,
    }


# ══════════════════════════════════════════════════════
#  API — CONFIGURACIÓN
# ══════════════════════════════════════════════════════

@app.get("/api/settings", summary="Obtiene toda la configuración")
async def get_settings(session=Depends(get_db)):
    result   = await session.execute(select(Setting))
    settings = result.scalars().all()
    return {s.key: s.value for s in settings}


@app.put("/api/settings", summary="Actualiza configuración")
async def update_settings(request: Request, session=Depends(get_db)):
    """Acepta un dict JSON con los pares clave-valor a actualizar."""
    data = await request.json()

    for key, value in data.items():
        existing = await session.get(Setting, key)
        if existing:
            existing.value = str(value)
        else:
            session.add(Setting(key=key, value=str(value)))

    await session.commit()
    logger.info(f"[API] Configuración actualizada: {list(data.keys())}")

    # Broadcast de cambios relevantes al dashboard
    if "nika_name" in data:
        await ws_manager.broadcast({"event": "settings_changed", "nika_name": data["nika_name"]})
    if "theme" in data:
        await ws_manager.broadcast({"event": "theme_changed", "theme": data["theme"]})

    return {"message": "Configuración actualizada.", "keys": list(data.keys())}


# ══════════════════════════════════════════════════════
#  API — VOZ
# ══════════════════════════════════════════════════════

class VoiceCommand(BaseModel):
    text:   str
    source: str = "wake_word"

class VoiceState(BaseModel):
    state: str   # listening | recording | processing | offline


@app.post("/api/voice/command", summary="Procesa comando de voz")
async def process_voice_command(cmd: VoiceCommand):
    """
    Endpoint para wake_word.py.
    Recibe texto transcrito, lo procesa con el orquestador y retorna
    la respuesta TTS que debe leer el detector de voz.
    """
    logger.info(f"[Voice API] 🎙️ Comando: '{cmd.text}' (source={cmd.source})")

    orchestrator: Orchestrator = app.state.orchestrator
    result = await orchestrator.process(cmd.text)

    # Notificar al dashboard en tiempo real
    await ws_manager.broadcast({
        "event":  "voice_command",
        "text":   cmd.text,
        "source": cmd.source,
        "result": result,
    })

    return result


@app.post("/api/voice/state", summary="Actualiza estado del micrófono")
async def update_voice_state(state: VoiceState):
    """Recibe el estado del wake_word.py y lo broadcast al dashboard."""
    await ws_manager.broadcast({
        "event": "voice_state",
        "state": state.state,
    })
    return {"ok": True}


@app.get("/api/voice/history", summary="Historial de comandos de voz")
async def get_voice_history():
    """Retorna los últimos 50 comandos de voz procesados."""
    orchestrator: Orchestrator = app.state.orchestrator
    return {"history": orchestrator.get_history()}


# ══════════════════════════════════════════════════════
#  API — HEALTH CHECK
# ══════════════════════════════════════════════════════

@app.get("/api/status", summary="Estado del sistema")
async def system_status():
    """Health check completo del sistema Nika."""
    mqtt    = MQTTManager.get_instance()
    devices = mqtt.get_devices()

    return {
        "status":          "online",
        "version":         "1.0.0",
        "mqtt_connected":  mqtt.connected,
        "mqtt_broker":     f"{mqtt.broker}:{mqtt.port}",
        "devices_total":   len(devices),
        "devices_online":  sum(1 for d in devices.values() if d.get("status") == "online"),
        "wake_word_active": (
            app.state.wake_proc is not None and
            app.state.wake_proc.poll() is None
        ) if hasattr(app.state, "wake_proc") else False,
    }


# ══════════════════════════════════════════════════════
#  PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))

    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true",
        log_level="info",
    )
