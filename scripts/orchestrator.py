"""
scripts/orchestrator.py — Cerebro Central de Nika OS
=====================================================
Recibe texto transcrito del wake_word.py y decide qué acción ejecutar.

Pipeline NLU (Natural Language Understanding) basado en:
  1. Normalización del texto (minúsculas, strip)
  2. Match de intención con patrones regex priorizados
  3. Extracción de entidades (app, dispositivo, modo)
  4. Ejecución del handler correspondiente
  5. Retorno de respuesta TTS + resultado de acción

Intenciones soportadas:
  - open_app:       "abre spotify [en laptop-01]"
  - close_app:      "cierra word"
  - activate_mode:  "modo trabajo" / "activa el modo gaming"
  - shutdown_device:"apaga el laptop"
  - scan_devices:   "qué dispositivos hay" / "escanea la red"
  - system_status:  "cómo estás" / "estado del sistema"
"""

import re
import json
import logging
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger("nika.orchestrator")

# ══════════════════════════════════════════════════════
#  PATRONES DE INTENCIÓN
#  Lista ordenada por prioridad: el primer match gana.
#  Cada entrada: (nombre_intención, patrón_regex_compilado)
# ══════════════════════════════════════════════════════
INTENT_PATTERNS: list[Tuple[str, re.Pattern]] = [

    # ── ACTIVAR MODO (alta prioridad para evitar conflictos con "abre") ────────
    # Matches: "modo trabajo", "activa el modo gaming", "activa modo estudio"
    ("activate_mode", re.compile(
        r"(?:activa(?:\s+(?:el\s+)?)?modo\s+|(?:^|\s)modo\s+)(.+)$",
        re.IGNORECASE,
    )),

    # ── ABRIR APLICACIÓN ───────────────────────────────────────────────────────
    # Matches: "abre spotify", "inicia word en laptop-01", "lanza chrome"
    ("open_app", re.compile(
        r"(?:abre?|inicia|ejecuta|lanza|pon|enciende)\s+(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CERRAR APLICACIÓN ──────────────────────────────────────────────────────
    # Matches: "cierra spotify", "cierra chrome en mi laptop"
    ("close_app", re.compile(
        r"(?:cierra|detiene?|para|mata)\s+(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── APAGAR DISPOSITIVO ─────────────────────────────────────────────────────
    # Matches: "apaga el laptop", "apaga mi pc"
    ("shutdown_device", re.compile(
        r"(?:apaga|apagar|shutdown)\s+(?:(?:el|mi|la)\s+)?(.+)$",
        re.IGNORECASE,
    )),

    # ── ESCANEAR DISPOSITIVOS ──────────────────────────────────────────────────
    # Matches: "qué dispositivos hay", "escanea la red", "busca dispositivos"
    ("scan_devices", re.compile(
        r"(?:qu[eé]\s+dispositivos|escanea(?:\s+(?:la\s+)?red)?|"
        r"busca\s+dispositivos|dispositivos\s+disponibles|"
        r"qué\s+hay\s+conectado)",
        re.IGNORECASE,
    )),

    # ── ESTADO DEL SISTEMA ─────────────────────────────────────────────────────
    # Matches: "cómo estás", "estado del sistema", "qué estás haciendo"
    ("system_status", re.compile(
        r"(?:c[oó]mo\s+est[aá]s|estado(?:\s+del\s+sistema)?|"
        r"qu[eé]\s+est[aá]s\s+haciendo|informe|status)",
        re.IGNORECASE,
    )),
]

# Respuestas de fallback aleatorias para cuando no se entiende el comando
FALLBACK_RESPONSES = [
    "No entendí eso. Puedes decir: 'abre una app', 'modo trabajo', o 'escanea dispositivos'.",
    "¿Puedes repetirlo? No capturé el comando correctamente.",
    "No reconocí ese comando. Intenta con: 'abre [app]' o 'modo [nombre]'.",
]
_fallback_idx = 0


class Orchestrator:
    """
    Cerebro central de Nika. Procesa texto transcrito y genera acciones MQTT.

    Depende de:
      - MQTTManager: para publicar comandos a dispositivos.
      - db_session_factory: función async que retorna AsyncSession
                            (para consultar modos y apps desde la DB).
    """

    def __init__(self, mqtt_manager=None, db_session_factory=None):
        """
        Args:
            mqtt_manager:        Instancia de MQTTManager (singleton).
            db_session_factory:  Clase/función que retorna un AsyncSession
                                 como context manager async (AsyncSessionLocal).
        """
        self.mqtt        = mqtt_manager
        self.db_factory  = db_session_factory
        self._command_history: list[Dict[str, Any]] = []    # Últimos 50 comandos

        logger.info("[Orchestrator] Inicializado y listo para procesar comandos.")

    # ── Punto de entrada principal ────────────────────────────────────────────

    async def process(self, text: str) -> Dict[str, Any]:
        """
        Punto de entrada. Recibe texto transcrito y retorna el resultado.

        Args:
            text: Texto transcrito del comando de voz (ej. "abre spotify en laptop-01")

        Returns:
            Dict con:
              - success (bool): Si la acción se ejecutó correctamente.
              - action (str):   Nombre de la intención ejecutada.
              - response (str): Texto de respuesta para TTS.
              - data (dict):    Datos adicionales de la acción.
        """
        logger.info(f"[Orchestrator] 🎙️ Procesando: '{text}'")

        intent, match = self._extract_intent(text)

        if intent is None:
            global _fallback_idx
            response = FALLBACK_RESPONSES[_fallback_idx % len(FALLBACK_RESPONSES)]
            _fallback_idx += 1
            result = {"success": False, "action": "none", "response": response, "data": {}}
        else:
            # Despachar al handler de la intención detectada
            handlers = {
                "open_app":       self._handle_open_app,
                "close_app":      self._handle_close_app,
                "activate_mode":  self._handle_activate_mode,
                "shutdown_device":self._handle_shutdown_device,
                "scan_devices":   self._handle_scan_devices,
                "system_status":  self._handle_system_status,
            }
            handler = handlers.get(intent)
            if handler:
                result = await handler(match)
            else:
                result = {"success": False, "action": intent,
                          "response": "Intención reconocida pero handler no implementado.", "data": {}}

        # Guardar en historial (máximo 50 entradas)
        self._command_history.append({"text": text, "intent": intent, **result})
        if len(self._command_history) > 50:
            self._command_history.pop(0)

        logger.info(f"[Orchestrator] Resultado: {result['action']} | éxito={result['success']}")
        return result

    # ── Extracción de intención ───────────────────────────────────────────────

    def _extract_intent(self, text: str) -> Tuple[Optional[str], Optional[re.Match]]:
        """
        Evalúa el texto contra los patrones de intención en orden de prioridad.

        Returns:
            (nombre_intención, match_object) o (None, None) si no hay coincidencia.
        """
        normalized = text.lower().strip()

        for intent_name, pattern in INTENT_PATTERNS:
            match = pattern.search(normalized)
            if match:
                logger.debug(f"[Orchestrator] Intención: '{intent_name}' | match='{match.group(0)}'")
                return intent_name, match

        logger.warning(f"[Orchestrator] Sin intención para: '{text}'")
        return None, None

    # ══════════════════════════════════════════════════
    #  HANDLERS DE INTENCIONES
    # ══════════════════════════════════════════════════

    async def _handle_open_app(self, match: re.Match) -> Dict[str, Any]:
        """
        Abre una aplicación en el dispositivo indicado (o en el primero disponible).
        
        Match grupos:
          group(1) → nombre de la app (ej. "spotify")
          group(2) → hint del dispositivo, opcional (ej. "laptop-01")
        """
        app_name    = match.group(1).strip()
        device_hint = match.group(2).strip() if match.lastindex >= 2 and match.group(2) else None

        device_id = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "open_app",
                "response": f"No hay dispositivos conectados para abrir {app_name}. "
                            f"Di 'escanea la red' primero.",
                "data": {"app": app_name},
            }

        command = {
            "action":   "open_app",
            "app_name": app_name,
            "app_path": app_name.lower(),    # El cliente intentará resolverlo a ejecutable
        }

        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False
        return {
            "success":  success,
            "action":   "open_app",
            "response": f"Abriendo {app_name} en {device_id}." if success
                        else f"No pude conectar con {device_id}. Verifica que esté en línea.",
            "data": {"app": app_name, "device": device_id},
        }

    async def _handle_close_app(self, match: re.Match) -> Dict[str, Any]:
        """Cierra una aplicación en el dispositivo indicado."""
        app_name    = match.group(1).strip()
        device_hint = match.group(2).strip() if match.lastindex >= 2 and match.group(2) else None
        device_id   = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "close_app",
                "response": f"No hay dispositivos conectados para cerrar {app_name}.",
                "data":     {},
            }

        command = {"action": "close_app", "app_name": app_name}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "close_app",
            "response": f"Cerrando {app_name} en {device_id}." if success
                        else "Error de conexión.",
            "data": {"app": app_name, "device": device_id},
        }

    async def _handle_activate_mode(self, match: re.Match) -> Dict[str, Any]:
        """
        Activa un Modo: busca sus apps en la DB y lanza cada una
        en su dispositivo asignado vía MQTT.
        """
        mode_name = match.group(1).strip()
        logger.info(f"[Orchestrator] Activando modo: '{mode_name}'")

        if not self.db_factory:
            return {
                "success":  False,
                "action":   "activate_mode",
                "response": "La base de datos no está disponible.",
                "data":     {},
            }

        # Importar aquí para evitar importación circular (database → orchestrator)
        from database import Mode, ModeApp
        from sqlalchemy import select

        async with self.db_factory() as session:
            # Búsqueda case-insensitive con LIKE
            result = await session.execute(
                select(Mode).where(Mode.name.ilike(f"%{mode_name}%"))
            )
            mode = result.scalar_one_or_none()

            if not mode:
                return {
                    "success":  False,
                    "action":   "activate_mode",
                    "response": f"No encontré el modo '{mode_name}'. "
                                f"Créalo en el dashboard de Nika.",
                    "data":     {},
                }

            # Lanzar todas las apps del modo en sus respectivos dispositivos
            launched = []
            failed   = []

            for app in mode.apps:
                command = {
                    "action":   "open_app",
                    "app_name": app.app_name,
                    "app_path": app.app_path,
                    "mode_id":  mode.id,
                    "mode_name":mode.name,
                }
                if self.mqtt and self.mqtt.publish_command(app.device_id, command):
                    launched.append(f"{app.app_name}@{app.device_id}")
                else:
                    failed.append(f"{app.app_name}@{app.device_id}")

            response = f"Modo {mode.name} activado. Iniciando {len(launched)} aplicaciones."
            if failed:
                response += f" {len(failed)} no pudieron lanzarse."

            return {
                "success":  len(launched) > 0,
                "action":   "activate_mode",
                "response": response,
                "data": {
                    "mode_id":  mode.id,
                    "mode_name":mode.name,
                    "launched": launched,
                    "failed":   failed,
                },
            }

    async def _handle_shutdown_device(self, match: re.Match) -> Dict[str, Any]:
        """Envía un comando de apagado a un dispositivo remoto."""
        device_hint = match.group(1).strip()
        device_id   = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "shutdown_device",
                "response": f"No encontré el dispositivo '{device_hint}'.",
                "data":     {},
            }

        command = {"action": "shutdown"}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "shutdown_device",
            "response": f"Enviando comando de apagado a {device_id}." if success
                        else "No pude conectar con el dispositivo.",
            "data": {"device": device_id},
        }

    async def _handle_scan_devices(self, match: re.Match) -> Dict[str, Any]:
        """Dispara un escaneo MQTT de dispositivos en la red."""
        if not self.mqtt:
            return {"success": False, "action": "scan_devices",
                    "response": "MQTT no disponible.", "data": {}}

        self.mqtt.scan_devices()
        devices     = self.mqtt.get_devices()
        online_count = sum(1 for d in devices.values() if d.get("status") == "online")

        return {
            "success":  True,
            "action":   "scan_devices",
            "response": f"Escaneando la red. Actualmente hay {online_count} dispositivos en línea.",
            "data":     {"devices_online": online_count},
        }

    async def _handle_system_status(self, match: re.Match) -> Dict[str, Any]:
        """Retorna un informe del estado general del sistema Nika."""
        online_count = 0
        device_names = []

        if self.mqtt:
            devices      = self.mqtt.get_devices()
            online_count = sum(1 for d in devices.values() if d.get("status") == "online")
            device_names = [k for k, v in devices.items() if v.get("status") == "online"]

        mqtt_status = "conectada" if (self.mqtt and self.mqtt.connected) else "desconectada"

        if online_count == 0:
            response = f"Estoy en línea. La red MQTT está {mqtt_status} y no hay dispositivos conectados."
        elif online_count == 1:
            response = f"Todo en orden. Tengo {device_names[0]} conectado."
        else:
            names_str = ", ".join(device_names[:-1]) + " y " + device_names[-1]
            response  = f"Sistema operativo. {online_count} dispositivos en línea: {names_str}."

        return {
            "success":  True,
            "action":   "system_status",
            "response": response,
            "data": {
                "mqtt_connected": self.mqtt.connected if self.mqtt else False,
                "devices_online": online_count,
                "device_names":   device_names,
            },
        }

    # ── Helper: resolver dispositivo ─────────────────────────────────────────

    def _resolve_device(self, hint: Optional[str]) -> Optional[str]:
        """
        Resuelve el ID del dispositivo a partir de un hint de texto.

        Estrategia:
          1. Si hay hint: busca dispositivos cuyo hostname contenga el hint.
          2. Si no hay hint o no hay match: usa el primer dispositivo online.
          3. Si no hay ningún dispositivo online: retorna None.

        Args:
            hint: String opcional del usuario (ej. "laptop", "gaming", "mi pc").

        Returns:
            device_id (hostname) del dispositivo a usar, o None.
        """
        if not self.mqtt:
            return None

        devices = self.mqtt.get_devices()
        online  = {k: v for k, v in devices.items() if v.get("status") == "online"}

        if not online:
            return None

        if hint:
            hint_lower = hint.lower().replace("el ", "").replace("mi ", "").strip()
            for device_id in online:
                if hint_lower in device_id.lower():
                    return device_id

        # Fallback: primer dispositivo online (orden de inserción en Python 3.7+)
        return next(iter(online))

    def get_history(self) -> list:
        """Retorna el historial de comandos procesados (últimos 50)."""
        return list(self._command_history)
