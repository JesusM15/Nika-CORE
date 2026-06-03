"""
nika_client/nika_client.py — Cliente Nika para Laptops Windows
==============================================================
Script liviano que corre en las laptops y actúa como agente remoto
controlado por Nika Core vía MQTT.

Responsabilidades:
  · Responder al ping de descubrimiento → reportar hostname, IP, OS y apps
  · Escuchar comandos de Nika en 'nika/command/{hostname}'
  · Ejecutar apps locales con subprocess (Windows: .exe, shell=True)
  · Publicar estado online/offline periódicamente
  · Publicar lista de apps al conectarse

Configuración:
  · Editar el diccionario AVAILABLE_APPS para añadir/quitar apps
  · Crear .env con MQTT_BROKER=<IP de la Raspberry Pi>

Ejecución:
  python nika_client.py
  (En producción: Tarea programada de Windows o un servicio NSSM)
"""

import os
import sys
import json
import time
import socket
import platform
import subprocess
import threading
import logging
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# Cargar .env local
load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nika.client")

# ── Configuración ─────────────────────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.100")  # IP de la Raspberry Pi
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER   = os.getenv("MQTT_USER", "")
MQTT_PASS   = os.getenv("MQTT_PASS", "")
HOSTNAME    = socket.gethostname()    # Se usa como device_id único

# ══════════════════════════════════════════════════
#  MAPA DE APLICACIONES DISPONIBLES
#  Edita este diccionario para añadir tus apps.
#
#  Formato:
#    "nombre_amigable": "ruta_al_ejecutable"
#
#  Puedes usar variables de entorno: os.path.expandvars()
#  las expande automáticamente (%USERNAME%, %APPDATA%, etc.)
# ══════════════════════════════════════════════════
AVAILABLE_APPS = {
    # ── Productividad ────────────────────────────
    "word":        r"%PROGRAMFILES%\Microsoft Office\root\Office16\WINWORD.EXE",
    "excel":       r"%PROGRAMFILES%\Microsoft Office\root\Office16\EXCEL.EXE",
    "powerpoint":  r"%PROGRAMFILES%\Microsoft Office\root\Office16\POWERPNT.EXE",
    "notepad":     "notepad.exe",
    "notepad++":   r"%PROGRAMFILES%\Notepad++\notepad++.exe",
    "vscode":      r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",

    # ── Navegadores ──────────────────────────────
    "chrome":      r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    "firefox":     r"%PROGRAMFILES%\Mozilla Firefox\firefox.exe",
    "edge":        r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe",

    # ── Multimedia ───────────────────────────────
    "spotify":     r"%APPDATA%\Spotify\Spotify.exe",
    "vlc":         r"%PROGRAMFILES%\VideoLAN\VLC\vlc.exe",

    # ── Utilidades ───────────────────────────────
    "calculator":  "calc.exe",
    "paint":       "mspaint.exe",
    "explorer":    "explorer.exe",
    "taskmgr":     "taskmgr.exe",
    "cmd":         "cmd.exe",
    "powershell":  "powershell.exe",

    # ── Gaming ───────────────────────────────────
    "steam":       r"%PROGRAMFILES(X86)%\Steam\steam.exe",
    "discord":     r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe",
}


def get_local_ip() -> str:
    """Obtiene la IP local del equipo (sin necesidad de acceso a internet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_apps_list() -> list:
    """
    Retorna la lista de apps disponibles verificando cuáles existen
    en el sistema actual (las que no existen se marcan como no disponibles).
    """
    apps = []
    for name, raw_path in AVAILABLE_APPS.items():
        expanded = os.path.expandvars(raw_path)
        # Para rutas simples (sin extensión), se asume que están en PATH
        is_simple = not os.sep in raw_path and not '%' in raw_path
        available = is_simple or Path(expanded.split()[0]).exists()

        apps.append({
            "name":      name,
            "path":      expanded,
            "available": available,
        })
    return apps


# ══════════════════════════════════════════════════
#  CLIENTE NIKA
# ══════════════════════════════════════════════════

class NikaClient:
    """
    Agente MQTT que corre en la laptop y ejecuta comandos de Nika Core.
    """

    def __init__(self):
        self.connected = False
        self._reconnect_delay = 2.0
        self._status_timer: Optional[threading.Timer] = None

        # Configurar cliente paho
        self.client = mqtt.Client(
            client_id=f"nika-client-{HOSTNAME}-{int(time.time())}",
            clean_session=True,
        )

        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

        # LWT: si el cliente se cae, el broker publica offline
        lwt = json.dumps({"status": "offline", "hostname": HOSTNAME, "ts": int(time.time())})
        self.client.will_set(f"nika/status/{HOSTNAME}", lwt, qos=1, retain=True)

        # Callbacks
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

        logger.info(f"[Client] Dispositivo: {HOSTNAME}")
        logger.info(f"[Client] IP local: {get_local_ip()}")
        logger.info(f"[Client] Broker: {MQTT_BROKER}:{MQTT_PORT}")
        logger.info(f"[Client] Apps configuradas: {len(AVAILABLE_APPS)}")

    # ── Conexión ──────────────────────────────────────────────────────────────

    def connect(self):
        logger.info(f"[Client] Conectando a {MQTT_BROKER}:{MQTT_PORT}...")
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self.client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            logger.info("[Client] Detenido por el usuario.")
            self._publish_status("offline")
            self.client.disconnect()
        except Exception as e:
            logger.error(f"[Client] Error de conexión: {e}")
            logger.info(f"[Client] Reintentando en {self._reconnect_delay:.0f}s...")
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)
            self.connect()

    # ── Callbacks de paho ────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            self._reconnect_delay = 2.0    # Resetear backoff
            logger.info(f"[Client] ✓ Conectado al broker MQTT.")

            # Suscribirse a topics relevantes
            client.subscribe([
                (f"nika/command/{HOSTNAME}", 1),    # Comandos directos
                ("nika/discovery/ping",      1),    # Broadcast de descubrimiento
            ])
            logger.info(f"[Client] Escuchando en: nika/command/{HOSTNAME}")

            # Publicar presencia y lista de apps
            self._publish_status("online")
            self._publish_apps()

            # Timer de heartbeat: publicar estado cada 60 segundos
            self._start_heartbeat()

        else:
            logger.error(f"[Client] ✗ Conexión rechazada (rc={rc})")

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            logger.warning(f"[Client] Desconectado inesperadamente (rc={rc})")
        self._stop_heartbeat()

    def _on_message(self, client, userdata, msg):
        """Despacha mensajes MQTT al handler correspondiente."""
        try:
            topic   = msg.topic
            payload = json.loads(msg.payload.decode("utf-8"))
            logger.info(f"[Client] ← {topic}: {payload}")

            if topic == "nika/discovery/ping":
                self._handle_discovery_ping()
            elif topic == f"nika/command/{HOSTNAME}":
                self._handle_command(payload)
            else:
                logger.debug(f"[Client] Topic no manejado: {topic}")

        except json.JSONDecodeError:
            logger.warning(f"[Client] Payload no-JSON en {msg.topic}: {msg.payload[:50]!r}")
        except Exception as e:
            logger.error(f"[Client] Error procesando mensaje: {e}", exc_info=True)

    # ── Handlers de topics ───────────────────────────────────────────────────

    def _handle_discovery_ping(self):
        """
        Responde al ping de descubrimiento de Nika Core.
        Publica en 'nika/discovery/pong/{hostname}' con info completa del dispositivo.
        """
        apps = get_apps_list()
        response = {
            "hostname": HOSTNAME,
            "platform": platform.system().lower(),    # 'windows'
            "platform_version": platform.version(),
            "ip":       get_local_ip(),
            "apps":     apps,
            "ts":       int(time.time()),
        }
        self.client.publish(
            f"nika/discovery/pong/{HOSTNAME}",
            json.dumps(response),
            qos=1,
        )
        logger.info(f"[Client] Pong enviado. Apps disponibles: {sum(1 for a in apps if a['available'])}/{len(apps)}")

    def _handle_command(self, payload: dict):
        """
        Ejecuta un comando de Nika Core.

        Comandos soportados:
          open_app:  { "action": "open_app", "app_name": "spotify", "app_path": "..." }
          close_app: { "action": "close_app", "app_name": "spotify" }
          shutdown:  { "action": "shutdown" }
          ping:      { "action": "ping" }
        """
        action = payload.get("action", "").lower()

        action_map = {
            "open_app":  self._open_app,
            "close_app": self._close_app,
            "shutdown":  self._handle_shutdown,
            "ping":      lambda p: self._publish_status("online"),
        }

        handler = action_map.get(action)
        if handler:
            threading.Thread(target=handler, args=(payload,), daemon=True).start()
        else:
            logger.warning(f"[Client] Acción desconocida: '{action}'")

    # ── Ejecutores de comandos ───────────────────────────────────────────────

    def _open_app(self, payload: dict):
        """
        Intenta abrir una aplicación.
        Estrategia de resolución:
          1. Buscar en AVAILABLE_APPS por nombre exacto
          2. Búsqueda parcial case-insensitive en AVAILABLE_APPS
          3. Usar app_path directamente como ejecutable
          4. Buscar en %PATH% del sistema
        """
        app_name = (payload.get("app_name") or "").lower().strip()
        app_path = (payload.get("app_path") or "").strip()

        # Resolver la ruta
        resolved_path = None

        # Intentar por nombre en el mapa de apps
        if app_name in AVAILABLE_APPS:
            resolved_path = os.path.expandvars(AVAILABLE_APPS[app_name])
        else:
            # Búsqueda parcial
            for key, path in AVAILABLE_APPS.items():
                if app_name in key or key in app_name:
                    resolved_path = os.path.expandvars(path)
                    break

        # Fallback: usar app_path del payload
        if not resolved_path and app_path:
            resolved_path = os.path.expandvars(app_path)

        # Último recurso: intentar como nombre de ejecutable en PATH
        if not resolved_path:
            resolved_path = app_name

        logger.info(f"[Client] Abriendo: '{app_name}' → {resolved_path}")

        try:
            subprocess.Popen(
                resolved_path,
                shell=True,    # shell=True para rutas con espacios y variables
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.expandvars("%USERPROFILE%"),
            )
            logger.info(f"[Client] ✓ Aplicación iniciada: {app_name}")
            self._publish_status("online", event=f"opened:{app_name}")
        except Exception as e:
            logger.error(f"[Client] ✗ Error abriendo '{app_name}': {e}")

    def _close_app(self, payload: dict):
        """
        Cierra un proceso por nombre usando taskkill (Windows).
        Si el nombre no termina en .exe, lo añade automáticamente.
        """
        app_name = (payload.get("app_name") or "").lower().strip()

        # Mapear nombre amigable a ejecutable
        exe_names = {
            "spotify":    "Spotify.exe",
            "word":       "WINWORD.EXE",
            "excel":      "EXCEL.EXE",
            "chrome":     "chrome.exe",
            "firefox":    "firefox.exe",
            "edge":       "msedge.exe",
            "vscode":     "Code.exe",
            "discord":    "Discord.exe",
            "steam":      "steam.exe",
            "vlc":        "vlc.exe",
            "notepad":    "notepad.exe",
            "notepad++":  "notepad++.exe",
            "calculator": "Calculator.exe",
        }

        exe = exe_names.get(app_name, app_name)
        if not exe.lower().endswith(".exe"):
            exe += ".exe"

        logger.info(f"[Client] Cerrando: {exe}")

        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", exe],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info(f"[Client] ✓ Proceso terminado: {exe}")
            else:
                logger.warning(f"[Client] taskkill warning: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.error(f"[Client] Timeout al cerrar {exe}")
        except Exception as e:
            logger.error(f"[Client] Error cerrando {exe}: {e}")

    def _handle_shutdown(self, payload: dict):
        """Apaga el sistema con 60 segundos de delay (cancelable con 'shutdown /a')."""
        delay = payload.get("delay", 60)
        logger.warning(f"[Client] ⚠️ APAGADO en {delay} segundos. Cancela con: shutdown /a")
        self._publish_status("offline", event="shutdown_scheduled")
        subprocess.run(["shutdown", "/s", "/t", str(delay)], shell=True)

    # ── Publicaciones ────────────────────────────────────────────────────────

    def _publish_status(self, status: str, event: Optional[str] = None):
        """Publica el estado actual del dispositivo."""
        payload = {
            "status":   status,
            "hostname": HOSTNAME,
            "ip":       get_local_ip(),
            "platform": platform.system().lower(),
            "ts":       int(time.time()),
        }
        if event:
            payload["event"] = event

        self.client.publish(
            f"nika/status/{HOSTNAME}",
            json.dumps(payload),
            qos=1,
            retain=True,    # retain=True para que Nika vea el último estado al conectarse
        )

    def _publish_apps(self):
        """Publica la lista de apps disponibles en este dispositivo."""
        apps = get_apps_list()
        payload = {"apps": apps, "hostname": HOSTNAME, "ts": int(time.time())}
        self.client.publish(
            f"nika/apps/{HOSTNAME}",
            json.dumps(payload),
            qos=1,
            retain=True,
        )

    # ── Heartbeat ────────────────────────────────────────────────────────────

    def _start_heartbeat(self):
        """Publica estado online cada 60 segundos."""
        def _beat():
            self._publish_status("online")
            self._status_timer = threading.Timer(60.0, _beat)
            self._status_timer.daemon = True
            self._status_timer.start()

        self._status_timer = threading.Timer(60.0, _beat)
        self._status_timer.daemon = True
        self._status_timer.start()

    def _stop_heartbeat(self):
        if self._status_timer:
            self._status_timer.cancel()
            self._status_timer = None


# ── Punto de entrada ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=" * 55)
    logger.info("  Nika Client — Agente de laptop")
    logger.info(f"  Dispositivo: {HOSTNAME}")
    logger.info(f"  Broker MQTT: {MQTT_BROKER}:{MQTT_PORT}")
    logger.info("=" * 55)

    # Verificar que paho está disponible
    try:
        import paho.mqtt.client
    except ImportError:
        logger.critical("paho-mqtt no instalado. Ejecuta: pip install paho-mqtt")
        sys.exit(1)

    client = NikaClient()
    client.connect()
