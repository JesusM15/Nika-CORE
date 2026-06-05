"""
nika_client/nika_client.py — Cliente Nika para Laptops Windows
==============================================================
Script liviano que corre en las laptops y actúa como agente remoto
controlado por Nika Core vía MQTT.

Responsabilidades:
  · Al iniciar: descubrir todas las apps instaladas → guardar en apps.db
  · Responder al ping de descubrimiento → reportar hostname, IP, OS y apps
  · Escuchar comandos de Nika en 'nika/command/{hostname}'
  · Abrir apps por nombre (resuelto via apps.db con búsqueda fuzzy)
  · Cerrar apps via taskkill usando el exe_name almacenado en apps.db
  · Publicar estado online/offline periódicamente

Configuración:
  · Crear nika_client/.env con MQTT_BROKER=<IP de la Raspberry Pi>
  · Para añadir una app: solo necesitas su nombre — el descubrimiento lo hace todo

Ejecución:
  python nika_client.py
  (En producción: Tarea programada de Windows o un servicio NSSM)
"""

import os
import sys
import json
import time
import ctypes
import socket
import platform
import subprocess
import threading
import logging
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

# Módulo de descubrimiento y BD local de apps
from app_discovery import AppDatabase, discover_apps, launch_app

# Cargar .env local (misma carpeta que este script)
load_dotenv(Path(__file__).parent / ".env")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nika.client")

# ── Configuración MQTT desde .env ─────────────────────────────────────────────
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.100")   # IP de la Raspberry Pi
MQTT_PORT   = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER   = os.getenv("MQTT_USER", "")
MQTT_PASS   = os.getenv("MQTT_PASS", "")
HOSTNAME    = socket.gethostname()    # Identificador único de este equipo

# ── Base de datos local de apps (inicializada en main) ────────────────────────
_app_db: Optional[AppDatabase] = None


def get_db() -> AppDatabase:
    """Retorna la instancia de AppDatabase (singleton)."""
    global _app_db
    if _app_db is None:
        _app_db = AppDatabase()
    return _app_db


# ── Helpers de red ────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """Obtiene la IP local del equipo sin necesidad de acceso a internet."""
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
    Retorna la lista de apps disponibles desde la BD local.
    Formato compatible con el protocolo MQTT de Nika.
    """
    db = get_db()
    return [
        {
            "name":      app["name"],
            "canonical": app["canonical"],
            "path":      app["exe_path"],
            "available": bool(app["available"]),
            "category":  app.get("category", "general"),
        }
        for app in db.get_all()
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  CLIENTE NIKA
# ══════════════════════════════════════════════════════════════════════════════

class NikaClient:
    """
    Agente MQTT que corre en la laptop y ejecuta comandos de Nika Core.

    Al iniciarse: descubre apps instaladas → BD local → publica lista al broker.
    Luego: escucha comandos MQTT y los ejecuta (open_app, close_app, shutdown, ping).
    """

    def __init__(self):
        self.connected            = False
        self._reconnect_delay     = 2.0
        self._status_timer: Optional[threading.Timer] = None

        # Referencia a la BD de apps
        self.db = get_db()

        # Configurar cliente paho-mqtt
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"nika-client-{HOSTNAME}-{int(time.time())}",
            clean_session=True,
        )

        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)

        # LWT: si el cliente se cae, el broker publica offline automáticamente
        lwt = json.dumps({
            "status":   "offline",
            "hostname": HOSTNAME,
            "ts":       int(time.time()),
        })
        self.client.will_set(f"nika/status/{HOSTNAME}", lwt, qos=1, retain=True)

        # Asignar callbacks
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

        logger.info(f"[Client] Dispositivo: {HOSTNAME}")
        logger.info(f"[Client] IP local:    {get_local_ip()}")
        logger.info(f"[Client] Broker:      {MQTT_BROKER}:{MQTT_PORT}")
        logger.info(f"[Client] Apps en BD:  {self.db.count()}")

    # ── Conexión ──────────────────────────────────────────────────────────────

    def connect(self):
        """Conecta al broker MQTT y entra en el loop de red."""
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

    # ── Callbacks de paho ─────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected        = True
            self._reconnect_delay = 2.0
            logger.info("[Client] ✓ Conectado al broker MQTT.")

            # Suscribirse a topics relevantes
            client.subscribe([
                (f"nika/command/{HOSTNAME}", 1),   # Comandos directos a este equipo
                ("nika/discovery/ping",      1),   # Broadcast de descubrimiento
                ("nika/reminder/fire",       1),   # Canal de recordatorios y alarmas
            ])
            logger.info(f"[Client] Escuchando en: nika/command/{HOSTNAME}")

            # Publicar presencia y lista de apps
            self._publish_status("online")
            self._publish_apps()

            # Heartbeat cada 60 segundos
            self._start_heartbeat()
        else:
            logger.error(f"[Client] ✗ Conexión rechazada (code={reason_code})")

    def _on_disconnect(self, client, userdata, flags, reason_code=None, properties=None):
        self.connected = False
        if reason_code != 0:
            logger.warning(f"[Client] Desconectado inesperadamente (code={reason_code})")
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
            elif topic == "nika/reminder/fire":
                self._handle_reminder_fire(payload)
            else:
                logger.debug(f"[Client] Topic no manejado: {topic}")

        except Exception as e:
            logger.error(f"[Client] Error procesando mensaje: {e}", exc_info=True)

    # ── Handlers de topics ────────────────────────────────────────────────────

    def _handle_discovery_ping(self):
        """
        Responde al ping de descubrimiento de Nika Core.
        Publica en 'nika/discovery/pong/{hostname}' con apps de la BD local.
        """
        from app_discovery import get_apps_list, get_local_ip
        apps     = get_apps_list()
        response = {
            "hostname":         HOSTNAME,
            "platform":         platform.system().lower(),
            "platform_version": platform.version(),
            "ip":               get_local_ip(),
            "apps":             apps,
            "ts":               int(time.time()),
        }
        self.client.publish(
            f"nika/discovery/pong/{HOSTNAME}",
            json.dumps(response),
            qos=1,
        )
        avail = sum(1 for a in apps if a["available"])
        logger.info(f"[Client] Pong enviado. Apps disponibles: {avail}/{len(apps)}")

    def _handle_reminder_fire(self, payload: dict):
        """
        Maneja el disparo de un recordatorio, alarma o temporizador.
        Reproduce un sonido de alerta y habla usando edge-tts o el sintetizador nativo de Windows.
        """
        text = payload.get("text", "Recordatorio")
        logger.info(f"[Client] 🔔 Recordatorio recibido: '{text}'")

        # 1. Reproducir sonido de alerta de Windows
        try:
            import winsound
            winsound.PlaySound("SystemQuestion", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception as e:
            logger.warning(f"[Client] No se pudo reproducir el sonido: {e}")

        # Determinación de texto
        if text.lower() in ("timer", "temporizador"):
            speak_text = "El temporizador ha terminado."
        elif text.lower() == "alarma":
            speak_text = "La alarma está sonando."
        else:
            speak_text = f"Recordatorio: {text}."

        # 2. Sintetizar la voz asíncronamente con edge-tts (Voz Alexa: es-MX-DaliaNeural)
        def _run_speak():
            try:
                import edge_tts
                import asyncio
                import tempfile

                # Crear archivo temporal para el audio mp3
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_f:
                    tmp_path = tmp_f.name

                async def _generate():
                    communicate = edge_tts.Communicate(speak_text, "es-MX-DaliaNeural")
                    await communicate.save(tmp_path)

                loop = asyncio.new_event_loop()
                loop.run_until_complete(_generate())
                loop.close()

                # Reproducir con MCI (winmm.dll) de manera nativa en Windows
                buf = ctypes.create_unicode_buffer(260)
                ctypes.windll.kernel32.GetShortPathNameW(tmp_path, buf, 260)
                short_path = buf.value

                ctypes.windll.winmm.mciSendStringW(f"open {short_path} type MPEGVideo alias tts_audio", None, 0, 0)
                ctypes.windll.winmm.mciSendStringW("play tts_audio wait", None, 0, 0)
                ctypes.windll.winmm.mciSendStringW("close tts_audio", None, 0, 0)

                # Intentar eliminar el archivo temporal
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

            except Exception as e:
                logger.warning(f"[Client] Error con edge-tts, usando fallback de Windows: {e}")
                # Fallback: Sintetizar la voz en Windows usando PowerShell (Speech API nativa)
                try:
                    safe_text = speak_text.replace("'", "''")
                    cmd = f"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{safe_text}')"
                    subprocess.Popen(["powershell", "-Command", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception as ex:
                    logger.error(f"[Client] Error al sintetizar voz en fallback: {ex}")

        # Ejecutar en hilo secundario para evitar bloquear el hilo principal MQTT
        threading.Thread(target=_run_speak, daemon=True).start()

    def _handle_command(self, payload: dict):
        """
        Despacha un comando de Nika Core al handler correcto.

        Comandos soportados:
          open_app:  { "action": "open_app",  "app_name": "spotify" }
          close_app: { "action": "close_app", "app_name": "spotify" }
          shutdown:  { "action": "shutdown",  "delay": 60 }
          ping:      { "action": "ping" }
          rescan:    { "action": "rescan" }   ← re-descubre todas las apps
        """
        action = payload.get("action", "").lower()

        action_map = {
            "open_app":      self._open_app,
            "close_app":     self._close_app,
            "play_music":    self._play_music,
            "media_control": self._media_control,
            "web_search":    self._web_search,
            "send_email":    self._send_email,
            "shutdown":      self._handle_shutdown,
            "rescan":        self._handle_rescan,
            "ping":          lambda p: self._publish_status("online"),
        }

        handler = action_map.get(action)
        if handler:
            threading.Thread(target=handler, args=(payload,), daemon=True).start()
        else:
            logger.warning(f"[Client] Acción desconocida: '{action}'")

    # ── Ejecutores de comandos ────────────────────────────────────────────────

    def _open_app(self, payload: dict):
        """
        Abre una aplicación resolviendo su nombre via la BD local (fuzzy search).

        El campo 'app_name' puede ser cualquier variante del nombre:
          - "spotify", "Spotify", "música" → abre Spotify
          - "bloc de notas", "notepad"     → abre Bloc de Notas
          - "antigravity ide", "vs code"   → abre VS Code

        Si la app no está en la BD, reporta el error pero no falla el proceso.
        """
        app_name = (payload.get("app_name") or "").strip()

        logger.info(f"[Client] Resolviendo: '{app_name}'")

        app = self.db.resolve(app_name)

        if not app:
            logger.error(
                f"[Client] ✗ No se encontró '{app_name}' en la BD.\n"
                f"  → Ejecuta el rescan o verifica que la app esté instalada."
            )
            return

        logger.info(f"[Client] Abriendo '{app['canonical']}' → {app['exe_path']}")

        success = launch_app(app)
        if success:
            logger.info(f"[Client] ✓ App iniciada: {app['name']}")
            self._publish_status("online", event=f"opened:{app['canonical']}")
        else:
            logger.error(f"[Client] ✗ Falló al abrir: {app['name']}")

    def _close_app(self, payload: dict):
        """
        Cierra una aplicación por nombre usando taskkill.

        Resuelve el nombre via la BD para obtener el exe_name correcto.
        Si no está en la BD, intenta con el nombre dado directamente.
        """
        app_name = (payload.get("app_name") or "").strip()

        logger.info(f"[Client] Cerrando: '{app_name}'")

        # Intentar resolver desde la BD
        app = self.db.resolve(app_name)
        if app:
            exe = app.get("exe_name") or ""
        else:
            # Fallback: usar el nombre directamente como nombre de proceso
            exe = app_name

        # Asegurarse de que tiene extensión .exe
        if exe and not exe.lower().endswith(".exe"):
            exe += ".exe"

        if not exe:
            logger.error(f"[Client] ✗ No se pudo resolver el exe para '{app_name}'")
            return

        logger.info(f"[Client] Matando proceso: {exe}")

        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", exe],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info(f"[Client] ✓ Proceso terminado: {exe}")
                self._publish_status("online", event=f"closed:{exe}")
            else:
                logger.warning(f"[Client] taskkill: {result.stderr.strip()}")
        except subprocess.TimeoutExpired:
            logger.error(f"[Client] Timeout cerrando {exe}")
        except Exception as e:
            logger.error(f"[Client] Error cerrando {exe}: {e}")

    def _handle_shutdown(self, payload: dict):
        """Programa el apagado del sistema (cancelable con 'shutdown /a')."""
        delay = payload.get("delay", 60)
        logger.warning(f"[Client] ⚠ APAGADO en {delay}s. Cancela con: shutdown /a")
        self._publish_status("offline", event="shutdown_scheduled")
        subprocess.run(["shutdown", "/s", "/t", str(delay)], shell=True)

    def _handle_rescan(self, payload: dict):
        """Re-ejecuta el descubrimiento de apps y actualiza la BD."""
        logger.info("[Client] Iniciando re-escaneo de aplicaciones...")
        total = discover_apps(self.db)
        logger.info(f"[Client] Re-escaneo completo: {total} apps en BD")
        self._publish_apps()    # Notificar a Nika Core con la lista actualizada

    def _play_music(self, payload: dict):
        """
        Abre Spotify y reproduce música.
        Si las credenciales de Spotipy están en el .env, usa la Web API para
        buscar y darle play de forma invisible. Si no, usa el protocolo URI
        como fallback.
        """
        service = payload.get("service", "spotify")
        query   = payload.get("query", "")
        logger.info(f"[Client] 🎵 Reproduciendo música via {service}. Query: '{query}'")

        if service == "spotify":
            client_id = os.getenv("SPOTIPY_CLIENT_ID")
            client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
            
            # --- INTENTO 1: Usar Spotify Web API (si hay credenciales) ---
            if client_id and client_secret:
                try:
                    import spotipy
                    from spotipy.oauth2 import SpotifyOAuth
                    
                    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                        client_id=client_id,
                        client_secret=client_secret,
                        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
                        scope="user-modify-playback-state user-read-playback-state"
                    ))
                    
                    # Obtener dispositivos activos
                    devices = sp.devices()
                    
                    if not devices.get('devices'):
                        # Si no hay dispositivos activos, intentamos abrir Spotify localmente primero
                        logger.info("[Client] No hay dispositivos Spotify activos. Abriendo app...")
                        subprocess.Popen(["cmd", "/c", "start", "spotify:"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(4)
                        devices = sp.devices()
                    
                    device_id = None
                    if devices.get('devices'):
                        # Buscar el dispositivo activo, o tomar el primero
                        active_devs = [d for d in devices['devices'] if d['is_active']]
                        device_id = active_devs[0]['id'] if active_devs else devices['devices'][0]['id']
                    
                    if device_id:
                        if query:
                            import re
                            import difflib
                            
                            uri_to_play = None
                            is_track = False
                            
                            # --- BÚSQUEDA INTELIGENTE CON MULTICANDIDATOS ---
                            clean_query = query.lower().strip()
                            pref_type = None
                            
                            # Extraer tipo preferido de música
                            if any(w in clean_query for w in ("album", "disco", "álbum")):
                                pref_type = "album"
                                clean_query = re.sub(r'\b(album|disco|álbum)\b', '', clean_query).strip()
                            elif any(w in clean_query for w in ("artista", "grupo", "banda")):
                                pref_type = "artist"
                                clean_query = re.sub(r'\b(artista|grupo|banda)\b', '', clean_query).strip()
                            elif any(w in clean_query for w in ("cancion", "canción", "pista", "rola", "tema", "sencillo", "single")):
                                pref_type = "track"
                                clean_query = re.sub(r'\b(cancion|canción|pista|rola|tema|sencillo|single)\b', '', clean_query).strip()
                            
                            # Optimizar query para el motor de Spotify (reemplazar "de" / "por" por espacios)
                            # Ej: "thriller de michael jackson" -> "thriller michael jackson"
                            search_q = re.sub(r'\b(?:de|por)\b', ' ', clean_query)
                            search_q = ' '.join(search_q.split()) # normalizar espacios
                            
                            logger.info(f"[Client] Spotify Smart Search: query='{clean_query}', search_q='{search_q}', pref_type={pref_type}")
                            
                            # Buscar en Spotify (limit=10 por tipo)
                            results = sp.search(q=search_q, limit=10, type='track,album,artist')
                            candidates = []
                            
                            # 1. Parsear canciones
                            if 'tracks' in results and results['tracks']['items']:
                                for t in results['tracks']['items']:
                                    name = t['name']
                                    artist = t['artists'][0]['name'] if t['artists'] else ""
                                    popularity = t.get('popularity', 0)
                                    uri = t['uri']
                                    
                                    # Similitud contra query original y query limpia
                                    r_name1 = difflib.SequenceMatcher(None, clean_query, name.lower()).ratio()
                                    r_name2 = difflib.SequenceMatcher(None, search_q, name.lower()).ratio()
                                    
                                    combined = f"{name} {artist}".lower()
                                    r_comb1 = difflib.SequenceMatcher(None, clean_query, combined).ratio()
                                    r_comb2 = difflib.SequenceMatcher(None, search_q, combined).ratio()
                                    
                                    rev_combined = f"{artist} {name}".lower()
                                    r_rev1 = difflib.SequenceMatcher(None, clean_query, rev_combined).ratio()
                                    r_rev2 = difflib.SequenceMatcher(None, search_q, rev_combined).ratio()
                                    
                                    best_ratio = max(r_name1, r_name2, r_comb1, r_comb2, r_rev1, r_rev2)
                                    
                                    if pref_type == "track":
                                        best_ratio += 0.20
                                        
                                    candidates.append({
                                        'name': f"{name} - {artist}",
                                        'uri': uri,
                                        'type': 'track',
                                        'ratio': best_ratio,
                                        'popularity': popularity,
                                        'score': best_ratio * 0.80 + (popularity / 100.0) * 0.20
                                    })
                                    
                            # 2. Parsear álbumes
                            if 'albums' in results and results['albums']['items']:
                                for a in results['albums']['items']:
                                    name = a['name']
                                    artist = a['artists'][0]['name'] if a['artists'] else ""
                                    popularity = 50 # Baseline para álbumes
                                    uri = a['uri']
                                    
                                    r_name1 = difflib.SequenceMatcher(None, clean_query, name.lower()).ratio()
                                    r_name2 = difflib.SequenceMatcher(None, search_q, name.lower()).ratio()
                                    
                                    combined = f"{name} {artist}".lower()
                                    r_comb1 = difflib.SequenceMatcher(None, clean_query, combined).ratio()
                                    r_comb2 = difflib.SequenceMatcher(None, search_q, combined).ratio()
                                    
                                    best_ratio = max(r_name1, r_name2, r_comb1, r_comb2)
                                    
                                    if pref_type == "album":
                                        best_ratio += 0.20
                                        
                                    candidates.append({
                                        'name': f"Album: {name} - {artist}",
                                        'uri': uri,
                                        'type': 'album',
                                        'ratio': best_ratio,
                                        'popularity': popularity,
                                        'score': best_ratio * 0.80 + (popularity / 100.0) * 0.20
                                    })
                                    
                            # 3. Parsear artistas
                            if 'artists' in results and results['artists']['items']:
                                for art in results['artists']['items']:
                                    name = art['name']
                                    popularity = art.get('popularity', 0)
                                    uri = art['uri']
                                    
                                    r_name1 = difflib.SequenceMatcher(None, clean_query, name.lower()).ratio()
                                    r_name2 = difflib.SequenceMatcher(None, search_q, name.lower()).ratio()
                                    best_ratio = max(r_name1, r_name2)
                                    
                                    if pref_type == "artist":
                                        best_ratio += 0.20
                                        
                                    candidates.append({
                                        'name': f"Artist: {name}",
                                        'uri': uri,
                                        'type': 'artist',
                                        'ratio': best_ratio,
                                        'popularity': popularity,
                                        'score': best_ratio * 0.80 + (popularity / 100.0) * 0.20
                                    })
                                    
                            # Ordenar candidatos por puntuación descendente
                            candidates.sort(key=lambda c: c['score'], reverse=True)
                            
                            uri_to_play = None
                            is_track = False
                            
                            if candidates:
                                best = candidates[0]
                                logger.info(
                                    f"[Client] Spotify Smart Choice: '{best['name']}' ({best['type']}) "
                                    f"score={best['score']:.3f} ratio={best['ratio']:.3f} pop={best['popularity']} uri={best['uri']}"
                                )
                                uri_to_play = best['uri']
                                is_track = (best['type'] == 'track')
                            
                            if uri_to_play:
                                sp.start_playback(
                                    device_id=device_id, 
                                    uris=[uri_to_play] if is_track else None,
                                    context_uri=uri_to_play if not is_track else None
                                )
                                logger.info(f"[Client] ✓ Spotipy reproduciendo: {uri_to_play}")
                                self._publish_status("online", event=f"music:spotify:playing")
                                return
                            else:
                                logger.warning(f"[Client] No se encontraron candidatos válidos para: {query}")
                        else:
                            # Solo darle play a lo que sea que esté pausado
                            sp.start_playback(device_id=device_id)
                            logger.info("[Client] ✓ Play enviado a Spotify via API")
                            return
                except ImportError:
                    logger.warning("[Client] spotipy no instalado. (pip install spotipy)")
                except Exception as e:
                    logger.error(f"[Client] ✗ Error con Spotipy API: {e}")

            # --- INTENTO 2: Fallback al protocolo URI de Spotify ---
            logger.info("[Client] Usando fallback URI de Spotify")
            try:
                import urllib.parse
                if query:
                    safe_query = urllib.parse.quote(query)
                    uri = f"spotify:search:{safe_query}"
                else:
                    uri = "spotify:"

                subprocess.Popen(
                    ["cmd", "/c", "start", uri],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info(f"[Client] ✓ Spotify abierto via URI: {uri}")

                if not query:
                    # Si no hay query, mandamos el media key para darle Play
                    time.sleep(3)
                    self._send_media_key("play_pause")
                    logger.info("[Client] ✓ Play enviado via media key")

                self._publish_status("online", event="music:spotify:playing")

            except Exception as e:
                logger.error(f"[Client] ✗ Error en fallback de Spotify: {e}")
        else:
            logger.warning(f"[Client] Servicio de música no soportado: {service}")

    def _web_search(self, payload: dict):
        """Abre Google Chrome (o default browser) buscando el query."""
        query = payload.get("query", "")
        logger.info(f"[Client] 🌐 Búsqueda web: '{query}'")
        if query:
            import urllib.parse
            safe_query = urllib.parse.quote(query)
            url = f"https://www.google.com/search?q={safe_query}"
            subprocess.Popen(["cmd", "/c", "start", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._publish_status("online", event="web_search")

    def _send_email(self, payload: dict):
        """Abre el cliente de correo predeterminado para redactar un email."""
        to = payload.get("to", "")
        subject = payload.get("subject", "")
        
        logger.info(f"[Client] ✉️ Redactando email a: '{to}' con asunto: '{subject}'")
        
        import urllib.parse
        # Construir URI mailto
        uri = "mailto:"
        if to:
            uri += urllib.parse.quote(to)
        if subject:
            uri += f"?subject={urllib.parse.quote(subject)}"
            
        subprocess.Popen(["cmd", "/c", "start", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._publish_status("online", event="email:draft")

    def _media_control(self, payload: dict):
        """
        Controla la reproducción multimedia usando media keys de Windows.

        Usa ctypes para enviar virtual key events al sistema operativo.
        Funciona con cualquier reproductor que respete las media keys
        del sistema (Spotify, VLC, Windows Media Player, etc.).

        Controles soportados:
          play_pause  → VK_MEDIA_PLAY_PAUSE (0xB3)
          play        → VK_MEDIA_PLAY_PAUSE (0xB3)
          pause       → VK_MEDIA_PLAY_PAUSE (0xB3)
          next        → VK_MEDIA_NEXT_TRACK (0xB0)
          prev        → VK_MEDIA_PREV_TRACK (0xB1)
          stop        → VK_MEDIA_STOP (0xB2)
          volume_up   → VK_VOLUME_UP (0xAF)
          volume_down → VK_VOLUME_DOWN (0xAE)
          mute        → VK_VOLUME_MUTE (0xAD)
        """
        control = payload.get("control", "play_pause")
        logger.info(f"[Client] 🎵 Media control: {control}")

        self._send_media_key(control)
        self._publish_status("online", event=f"media:{control}")

    @staticmethod
    def _send_media_key(control: str):
        """
        Envía un virtual key event de media al sistema operativo Windows.

        Usa la API keybd_event de user32.dll para simular la pulsación
        de las teclas multimedia del teclado.
        """
        # Mapa de controles → Virtual Key codes de Windows
        VK_MAP = {
            "play_pause":  0xB3,   # VK_MEDIA_PLAY_PAUSE
            "play":        0xB3,   # VK_MEDIA_PLAY_PAUSE (toggle)
            "pause":       0xB3,   # VK_MEDIA_PLAY_PAUSE (toggle)
            "next":        0xB0,   # VK_MEDIA_NEXT_TRACK
            "prev":        0xB1,   # VK_MEDIA_PREV_TRACK
            "stop":        0xB2,   # VK_MEDIA_STOP
            "volume_up":   0xAF,   # VK_VOLUME_UP
            "volume_down": 0xAE,   # VK_VOLUME_DOWN
            "mute":        0xAD,   # VK_VOLUME_MUTE
        }

        vk_code = VK_MAP.get(control)
        if vk_code is None:
            logger.warning(f"[Client] Media key desconocida: {control}")
            return

        try:
            # KEYEVENTF_EXTENDEDKEY = 0x0001, KEYEVENTF_KEYUP = 0x0002
            ctypes.windll.user32.keybd_event(vk_code, 0, 0x0001, 0)        # Key down
            time.sleep(0.05)
            ctypes.windll.user32.keybd_event(vk_code, 0, 0x0001 | 0x0002, 0)  # Key up
            logger.info(f"[Client] ✓ Media key enviada: {control} (VK=0x{vk_code:02X})")
        except Exception as e:
            logger.error(f"[Client] ✗ Error enviando media key: {e}")

    # ── Publicaciones MQTT ────────────────────────────────────────────────────

    def _publish_status(self, status: str, event: Optional[str] = None):
        """Publica el estado online/offline de este dispositivo."""
        payload: dict = {
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
            retain=True,   # El broker retiene el último estado para nuevos suscriptores
        )

    def _publish_apps(self):
        """Publica la lista completa de apps disponibles en la BD local."""
        apps    = get_apps_list()
        payload = {"apps": apps, "hostname": HOSTNAME, "ts": int(time.time())}
        self.client.publish(
            f"nika/apps/{HOSTNAME}",
            json.dumps(payload),
            qos=1,
            retain=True,
        )
        logger.info(f"[Client] Lista de apps publicada ({len(apps)} apps)")

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    def _start_heartbeat(self):
        """Publica estado online cada 60 segundos para mantener presencia."""
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

    # Verificar dependencias
    try:
        import paho.mqtt.client
    except ImportError:
        logger.critical("paho-mqtt no instalado. Ejecuta: pip install paho-mqtt")
        sys.exit(1)

    # ── Fase 1: Descubrimiento de apps ────────────────────────────────────────
    logger.info("[Inicio] Iniciando descubrimiento de aplicaciones instaladas...")
    db    = get_db()
    total = discover_apps(db)
    logger.info(f"[Inicio] ✓ {total} aplicaciones disponibles en la BD local")

    # ── Fase 2: Conexión MQTT y loop principal ────────────────────────────────
    client = NikaClient()
    client.connect()
