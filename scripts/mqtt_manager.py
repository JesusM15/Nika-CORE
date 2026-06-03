"""
scripts/mqtt_manager.py — Gestor Centralizado MQTT de Nika OS
=============================================================
Módulo Singleton que encapsula TODA la lógica de mensajería MQTT.

Responsabilidades:
  · Conexión/reconexión automática con backoff exponencial
  · Topics estructurados bajo el namespace "nika/"
  · Descubrimiento de dispositivos vía broadcast ping/pong
  · Callbacks registrables para responder a cualquier topic
  · LWT (Last Will Testament) para detectar desconexiones abruptas de Nika
  · Broadcast de eventos al dashboard vía WebSocket (inyectado desde main.py)

Topics MQTT utilizados:
  nika/discovery/ping          → Broadcast de descubrimiento (Nika → todos)
  nika/discovery/pong/{id}     → Respuesta de cada dispositivo (dispositivo → Nika)
  nika/command/{device_id}     → Comando a un dispositivo específico (Nika → dispositivo)
  nika/status/{device_id}      → Estado online/offline de un dispositivo
  nika/apps/{device_id}        → Lista de apps disponibles en el dispositivo
  nika/core/status             → Estado de Nika core (con LWT de offline)

Uso:
  mqtt = MQTTManager.get_instance()
  mqtt.connect()
  mqtt.publish_command("laptop-01", {"action": "open_app", "app_path": "spotify"})
"""

import os
import json
import time
import threading
import logging
from typing import Callable, Optional, Dict, Any, List
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("nika.mqtt")

# ── Definición de topics (namespace centralizado) ─────────────────────────────
TOPIC_DISCOVERY_PING = "nika/discovery/ping"
TOPIC_DISCOVERY_PONG = "nika/discovery/pong/+"      # '+' wildcard: cualquier device_id
TOPIC_STATUS         = "nika/status/+"
TOPIC_APPS           = "nika/apps/+"
TOPIC_NIKA_STATUS    = "nika/core/status"

# Función helper para construir topics con ID
def topic_command(device_id: str) -> str:
    return f"nika/command/{device_id}"


class MQTTManager:
    """
    Gestor singleton de todas las operaciones MQTT de Nika.

    El patrón Singleton garantiza que solo exista una conexión MQTT
    activa en todo el proceso, compartida entre main.py y orchestrator.py.
    """

    _instance: Optional["MQTTManager"] = None
    _lock = threading.Lock()           # Lock para thread-safety del singleton

    def __init__(self):
        # ── Configuración desde variables de entorno ──────────────────────────
        self.broker    = os.getenv("MQTT_BROKER", "localhost")
        self.port      = int(os.getenv("MQTT_PORT", "1883"))
        self.username  = os.getenv("MQTT_USER", "")
        self.password  = os.getenv("MQTT_PASS", "")
        self.client_id = f"nika-core-{int(time.time())}"

        # ── Estado interno ────────────────────────────────────────────────────
        # Diccionario en memoria de dispositivos descubiertos.
        # Clave: hostname (device_id), Valor: dict con status, ip, apps, etc.
        self.devices: Dict[str, Dict[str, Any]] = {}

        # Callbacks externos registrados por topic pattern
        # Dict[pattern_str, List[Callable]]
        self._callbacks: Dict[str, List[Callable]] = {}

        # Función de broadcast al WebSocket (inyectada desde main.py)
        self._ws_broadcast: Optional[Callable] = None

        # Estado de conexión y backoff
        self.connected       = False
        self._reconnect_delay     = 1.0     # Delay inicial de reconexión (segundos)
        self._max_reconnect_delay = 60.0    # Delay máximo (1 minuto)

        # ── Cliente paho-mqtt ─────────────────────────────────────────────────
        self.client = mqtt.Client(
            client_id=self.client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        self._configure_client()

        logger.info(f"[MQTT] MQTTManager creado. Broker: {self.broker}:{self.port}")

    # ── Patrón Singleton ──────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "MQTTManager":
        """
        Retorna la instancia única del MQTTManager.
        Thread-safe: usa double-checked locking.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Configuración del cliente paho ────────────────────────────────────────

    def _configure_client(self):
        """
        Aplica credenciales, LWT y callbacks al cliente paho.
        Llamado una sola vez en __init__.
        """
        # Credenciales opcionales (Mosquitto puede correr sin autenticación)
        if self.username:
            self.client.username_pw_set(self.username, self.password)

        # LWT (Last Will Testament): si la conexión de Nika cae de forma abrupta,
        # el broker publicará automáticamente este mensaje en nombre de Nika.
        # retain=True garantiza que los clientes que se suscriban después lo vean.
        lwt_payload = json.dumps({
            "status": "offline",
            "reason": "unexpected_disconnect",
            "ts":     int(time.time()),
        })
        self.client.will_set(
            topic=TOPIC_NIKA_STATUS,
            payload=lwt_payload,
            qos=1,
            retain=True,
        )

        # Hooks del ciclo de vida del cliente
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    # ── Gestión de Conexión ───────────────────────────────────────────────────

    def connect(self):
        """
        Inicia la conexión al broker y arranca el loop de red en un hilo daemon.
        El loop de paho maneja reconexiones y keepalives automáticamente.
        """
        logger.info(f"[MQTT] → Conectando a {self.broker}:{self.port} ...")
        try:
            self.client.connect(
                host=self.broker,
                port=self.port,
                keepalive=60,    # Ping al broker cada 60s para mantener la conexión
            )
            # loop_start() arranca un hilo daemon de red interno de paho.
            # No bloquea y maneja recv/send de forma concurrente con FastAPI.
            self.client.loop_start()
        except ConnectionRefusedError:
            logger.error(
                f"[MQTT] ✗ Broker rechazó la conexión. "
                f"¿Está Mosquitto corriendo en {self.broker}:{self.port}?"
            )
            self._schedule_reconnect()
        except Exception as e:
            logger.error(f"[MQTT] ✗ Error al conectar: {e}")
            self._schedule_reconnect()

    def disconnect(self):
        """Desconexión limpia: publica estado offline y cierra el loop."""
        if self.connected:
            payload = json.dumps({"status": "offline", "ts": int(time.time())})
            self.client.publish(TOPIC_NIKA_STATUS, payload, qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()
        self.connected = False
        logger.info("[MQTT] Desconectado limpiamente del broker.")

    def _schedule_reconnect(self):
        """
        Programa un reintento de conexión con backoff exponencial.
        El delay se duplica en cada fallo hasta _max_reconnect_delay.
        """
        logger.warning(f"[MQTT] ⏳ Reintentando en {self._reconnect_delay:.1f}s...")
        threading.Timer(self._reconnect_delay, self.connect).start()
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    # ── Callbacks del ciclo de vida MQTT ─────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc: int):
        """
        Callback de paho: conexión establecida (rc=0) o fallida (rc>0).
        rc codes: 0=OK, 1=versión, 2=client_id, 3=server, 4=creds, 5=authz
        """
        if rc == 0:
            logger.info(f"[MQTT] ✓ Conectado al broker: {self.broker}:{self.port}")
            self.connected = True
            self._reconnect_delay = 1.0    # Resetear backoff al conectar exitosamente

            # Publicar presencia de Nika (retain=True para que los clientes nuevos la vean)
            online_payload = json.dumps({"status": "online", "ts": int(time.time())})
            client.publish(TOPIC_NIKA_STATUS, online_payload, qos=1, retain=True)

            # Suscribirse a los topics relevantes con QoS 1 (al menos una vez)
            subscriptions = [
                (TOPIC_DISCOVERY_PONG, 1),   # Respuestas de dispositivos al ping
                (TOPIC_STATUS, 1),           # Cambios de estado de dispositivos
                (TOPIC_APPS, 1),             # Listas de apps de dispositivos
            ]
            client.subscribe(subscriptions)
            logger.info(f"[MQTT] Suscrito a {len(subscriptions)} topics.")

            # Broadcast al dashboard via WebSocket
            self._broadcast_ws({"event": "mqtt_connected", "broker": self.broker})
        else:
            error_map = {
                1: "Versión de protocolo incorrecta",
                2: "Identificador de cliente inválido",
                3: "Broker no disponible",
                4: "Credenciales incorrectas",
                5: "No autorizado",
            }
            logger.error(f"[MQTT] ✗ Fallo rc={rc}: {error_map.get(rc, 'Error desconocido')}")
            self._broadcast_ws({"event": "mqtt_disconnected"})
            self._schedule_reconnect()

    def _on_disconnect(self, client, userdata, rc: int):
        """Callback: desconexión detectada por paho."""
        self.connected = False
        self._broadcast_ws({"event": "mqtt_disconnected"})
        if rc != 0:     # rc=0 = desconexión intencional (por disconnect())
            logger.warning(f"[MQTT] ⚠ Desconexión inesperada rc={rc}. Reconectando...")
            self._schedule_reconnect()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        """
        Callback principal: recibe TODOS los mensajes de los topics suscritos.
        Actúa como router que despacha a handlers específicos.
        """
        try:
            topic   = msg.topic
            raw     = msg.payload.decode("utf-8")
            payload = json.loads(raw)
            logger.debug(f"[MQTT] ← {topic}: {str(payload)[:120]}")

            # ── Router de topics ───────────────────────────────────────────────
            if topic.startswith("nika/discovery/pong/"):
                self._handle_device_pong(topic, payload)
            elif topic.startswith("nika/status/"):
                self._handle_device_status(topic, payload)
            elif topic.startswith("nika/apps/"):
                self._handle_device_apps(topic, payload)

            # ── Disparar callbacks externos registrados ──────────────────────
            # Permite que main.py u otros módulos reaccionen a mensajes específicos
            for pattern, callbacks in self._callbacks.items():
                if mqtt.topic_matches_sub(pattern, topic):
                    for cb in callbacks:
                        try:
                            cb(topic, payload)
                        except Exception as e:
                            logger.error(f"[MQTT] Error en callback para '{pattern}': {e}")

        except json.JSONDecodeError:
            logger.warning(f"[MQTT] Payload no-JSON en '{msg.topic}': {msg.payload[:80]!r}")
        except Exception as e:
            logger.error(f"[MQTT] Error procesando mensaje de '{msg.topic}': {e}", exc_info=True)

    # ── Handlers específicos por tipo de topic ────────────────────────────────

    def _handle_device_pong(self, topic: str, payload: dict):
        """
        Procesa la respuesta al ping de descubrimiento.
        Payload esperado del nika_client:
          {"hostname": "laptop-01", "platform": "windows", "ip": "192.168.1.x",
           "apps": [{"name": "spotify", "path": "..."}]}
        """
        device_id = topic.split("/")[-1]    # Extraer device_id del topic

        # Actualizar o crear entrada en el diccionario de dispositivos
        self.devices[device_id] = {
            "hostname":  payload.get("hostname", device_id),
            "platform":  payload.get("platform", "unknown"),
            "ip":        payload.get("ip", "unknown"),
            "apps":      payload.get("apps", []),
            "status":    "online",
            "last_seen": datetime.utcnow().isoformat(),
        }

        logger.info(
            f"[MQTT] 📡 Dispositivo: {device_id} | "
            f"IP: {payload.get('ip')} | "
            f"Apps: {len(payload.get('apps', []))}"
        )

        self._broadcast_ws({
            "event":  "device_discovered",
            "device_id": device_id,
            "device": self.devices[device_id],
        })

    def _handle_device_status(self, topic: str, payload: dict):
        """
        Actualiza el estado online/offline de un dispositivo en el diccionario.
        Esto permite mostrar el badge correcto en el dashboard.
        """
        device_id = topic.split("/")[-1]
        status    = payload.get("status", "unknown")

        # Actualizar o crear el dispositivo
        if device_id not in self.devices:
            self.devices[device_id] = {"hostname": device_id}

        self.devices[device_id].update({
            "status":    status,
            "last_seen": datetime.utcnow().isoformat(),
        })

        logger.info(f"[MQTT] 💻 {device_id} → {status.upper()}")
        self._broadcast_ws({
            "event":     "device_status",
            "device_id": device_id,
            "status":    status,
        })

    def _handle_device_apps(self, topic: str, payload: dict):
        """
        Almacena la lista actualizada de apps de un dispositivo.
        El dispositivo puede publicar esto proactivamente al conectarse.
        """
        device_id = topic.split("/")[-1]
        apps      = payload.get("apps", [])

        if device_id not in self.devices:
            self.devices[device_id] = {"hostname": device_id, "status": "online"}

        self.devices[device_id]["apps"] = apps
        logger.info(f"[MQTT] 📦 Apps de {device_id}: {len(apps)} aplicaciones")

    # ── API Pública ───────────────────────────────────────────────────────────

    def publish_command(self, device_id: str, command: dict) -> bool:
        """
        Publica un comando JSON a un dispositivo específico.

        Args:
            device_id: Hostname del dispositivo destino (ej. "laptop-gamer").
            command:   Diccionario con la acción, ej:
                       {"action": "open_app", "app_name": "Spotify", "app_path": "spotify"}

        Returns:
            True si el mensaje se encoló para envío, False si no hay conexión.
        """
        if not self.connected:
            logger.error(f"[MQTT] Sin conexión. Comando para '{device_id}' descartado: {command}")
            return False

        # Enriquecer el payload con metadata
        full_payload = {
            **command,
            "ts":   int(time.time()),
            "from": "nika-core",
        }

        topic  = topic_command(device_id)
        result = self.client.publish(topic, json.dumps(full_payload), qos=1)

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            logger.info(f"[MQTT] → {topic} | acción: {command.get('action')}")
            return True
        else:
            logger.error(f"[MQTT] Error publicando en '{topic}': rc={result.rc}")
            return False

    def scan_devices(self):
        """
        Envía un broadcast ping a todos los dispositivos.
        Los dispositivos suscritos a 'nika/discovery/ping' responden
        en 'nika/discovery/pong/{su_hostname}'.

        Llamar a get_devices() ~2 segundos después para ver los resultados.
        """
        if not self.connected:
            logger.warning("[MQTT] Sin conexión. Scan omitido.")
            return

        payload = json.dumps({
            "action": "ping",
            "ts":     int(time.time()),
            "from":   "nika-core",
        })
        self.client.publish(TOPIC_DISCOVERY_PING, payload, qos=1)
        logger.info("[MQTT] 📡 Broadcast de descubrimiento enviado.")

    def register_callback(self, topic_pattern: str, callback: Callable[[str, dict], None]):
        """
        Registra una función callback para mensajes de un topic pattern.
        Soporta wildcards MQTT estándar: '+' (un nivel) y '#' (multinivel).

        Args:
            topic_pattern: Ej. "nika/status/+", "nika/#"
            callback:      fn(topic: str, payload: dict) → None
        """
        if topic_pattern not in self._callbacks:
            self._callbacks[topic_pattern] = []
        self._callbacks[topic_pattern].append(callback)
        logger.debug(f"[MQTT] Callback registrado para pattern: '{topic_pattern}'")

    def set_ws_broadcast(self, broadcast_fn: Callable[[dict], None]):
        """
        Inyecta la función de broadcast del WebSocket desde main.py.
        Esta función se llama cada vez que hay un evento MQTT relevante
        para actualizar el dashboard en tiempo real.

        Args:
            broadcast_fn: Función async o sync que acepta un dict y lo envía a todos
                          los clientes WebSocket conectados.
        """
        self._ws_broadcast = broadcast_fn
        logger.info("[MQTT] Función de broadcast WebSocket configurada.")

    def get_devices(self) -> Dict[str, Dict[str, Any]]:
        """Retorna una copia del diccionario de dispositivos en memoria."""
        return dict(self.devices)

    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Retorna los datos de un dispositivo específico o None si no existe."""
        return self.devices.get(device_id)

    # ── Helper interno ────────────────────────────────────────────────────────

    def _broadcast_ws(self, data: dict):
        """
        Llama a la función de broadcast WebSocket de forma segura.
        La función puede ser None si main.py no la ha inyectado todavía.
        """
        if self._ws_broadcast:
            try:
                self._ws_broadcast(data)
            except Exception as e:
                logger.debug(f"[MQTT] Error en WS broadcast: {e}")
