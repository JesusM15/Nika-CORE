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
  - activate_mode:  "modo trabajo" / "activa el modo gaming" / "abre modo estudio en MSI"
  - play_music:     "pon música [en MSI]" / "reproduce música"
  - media_control:  "pausa la música" / "siguiente canción" / "sube el volumen"
  - shutdown_device:"apaga el laptop"
  - scan_devices:   "qué dispositivos hay" / "escanea la red"
  - system_status:  "cómo estás" / "estado del sistema"
"""
import os
import re
import json
import logging
import difflib
from typing import Optional, Tuple, Dict, Any

logger = logging.getLogger("nika.orchestrator")

# ══════════════════════════════════════════════════════
#  PATRONES DE INTENCIÓN
#  Lista ordenada por prioridad: el primer match gana.
#  Cada entrada: (nombre_intención, patrón_regex_compilado)
# ══════════════════════════════════════════════════════
INTENT_PATTERNS: list[Tuple[str, re.Pattern]] = [

    # ── ACTIVAR MODO (prioridad máxima — evita conflicto con "abre") ───────────
    # Matches: "modo trabajo", "activa el modo gaming", "cambia a modo estudio en MSI"
    ("activate_mode", re.compile(
        r"(?:"
        r"(?:activa|abre|inicia|lanza|pon|enciende|cambia\s+a|ejecuta)(?:\s+(?:el\s+)?)?"
        r"modo\s+"
        r"|(?:^|\s)modo\s+"
        r")(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CONTROL DE MÚSICA (antes de open_app para que "pon música" no abra "música") ─
    # Matches: "pon música", "reproduce una cancion de taylor", "quiero escuchar a the weeknd"
    ("play_music", re.compile(
        r"(?:pon(?:me)?|reproduce|reprod[uú]ceme|toca|lanza|escuchar|quiero\s+escuchar)\s+"
        r"(?:(?:la\s+|una\s+)?(?:m[uú]sica|canci[oó]n|pista|rola)(?:\s+de)?|(?:el\s+|un\s+)?(?:[aá]lbum|disco)|(?:el\s+|un\s+)?artista|a\s+)?\s*"
        r"(.*?)"
        r"(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CONTROL MULTIMEDIA (pausa, siguiente, volumen) ────────────────────────
    # Matches: "pausa", "callate", "siguiente", "otra", "subele al volumen", "reanuda la cancion"
    ("media_control", re.compile(
        r"(?:"
        r"(?:pausa|para|det[eé]n|detiene|c[aá]llate|silencio|ponle\s+pausa|desactiva)"
        r"|(?:siguiente|salta|otra|cambia|p[aá]sala)"
        r"|(?:anterior|atr[aá]s|regresa|vuelve)"
        r"|(?:sube|baja|m[aá]s|menos|s[uú]bele|b[aá]jale)"
        r"|(?:contin[uú]a|resume|reanuda|despausa|dale\s+play|qu[ií]tale\s+la\s+pausa|reproduce)"
        r")"
        r"(?:\s+(?:la\s+|el\s+)?(?:m[uú]sica|canci[oó]n|pista|track|rola|volumen|lo|la))*"
        r"(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── ABRIR APLICACIÓN ───────────────────────────────────────────────────────
    # Matches: "abreme spotify", "arranca word", "habre el navegador"
    ("open_app", re.compile(
        r"(?:[aá]bre(?:me)?|habre(?:me)?|inicia|ejecuta|lanza|pon(?:me)?|enciende|arranca|mu[eé]strame)\s+(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CERRAR APLICACIÓN ──────────────────────────────────────────────────────
    # Matches: "cierrame spotify", "sierra chrome", "quita el juego"
    ("close_app", re.compile(
        r"(?:cierra(?:me)?|sierra(?:me)?|det[eé]n|detiene|para|mata|quita|apaga)\s+(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── APAGAR DISPOSITIVO ─────────────────────────────────────────────────────
    # Matches: "apaga el laptop", "suspende mi pc"
    ("shutdown_device", re.compile(
        r"(?:apaga(?:me)?|apagar|shutdown|suspende)\s+(?:(?:el|mi|la)\s+)?(.+)$",
        re.IGNORECASE,
    )),

    # ── ESCANEAR DISPOSITIVOS ──────────────────────────────────────────────────
    # Matches: "qué dispositivos hay", "actualiza dispositivos"
    ("scan_devices", re.compile(
        r"(?:qu[eé]\s+dispositivos|escanea(?:\s+(?:la\s+)?red)?|"
        r"busca\s+dispositivos|dispositivos\s+disponibles|"
        r"qu[eé]\s+hay\s+conectado|actualiza\s+dispositivos)",
        re.IGNORECASE,
    )),

    # ── BÚSQUEDA WEB ───────────────────────────────────────────────────────────
    # Matches: "busca en google recetas", "googlea sobre perros", "investiga"
    ("web_search", re.compile(
        r"(?:b[uú]sca(?:me)?|buscar|b[uú]scalo|investiga(?:me)?|googlea)\s+"
        r"(?:(?:algo\s+)?(?:en\s+)?(?:google|internet|la\s+web)\s+(?:sobre\s+|de\s+)?)?"
        r"(.*?)"
        r"(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── ESTADO DEL SISTEMA ─────────────────────────────────────────────────────
    # Matches: "cómo estás", "estás ahí", "estado del sistema"
    ("system_status", re.compile(
        r"(?:c[oó]mo\s+est[aá]s|estado(?:\s+del\s+sistema)?|"
        r"qu[eé]\s+est[aá]s\s+haciendo|informe|status|est[aá]s\s+ah[ií])",
        re.IGNORECASE,
    )),

    # ── CONVERSACIÓN IA (FALLBACK) ─────────────────────────────────────────────
    # Matches: Cualquier cosa que no haya coincidido con los anteriores (chistes, preguntas)
    ("chat_ai", re.compile(
        r"^(.*)$",
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
                "play_music":     self._handle_play_music,
                "media_control":  self._handle_media_control,
                "web_search":     self._handle_web_search,
                "shutdown_device":self._handle_shutdown_device,
                "scan_devices":   self._handle_scan_devices,
                "system_status":  self._handle_system_status,
                "chat_ai":        self._handle_chat_ai,
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

        Ahora soporta override de dispositivo por voz:
          "abre modo estudio"       → usa el device_id de cada app
          "abre modo estudio en MSI" → manda TODAS las apps a MSI
        """
        mode_name   = match.group(1).strip()
        device_hint = match.group(2).strip() if match.lastindex >= 2 and match.group(2) else None

        logger.info(f"[Orchestrator] Activando modo: '{mode_name}'"
                     + (f" en dispositivo hint='{device_hint}'" if device_hint else ""))

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

            # Si el usuario dijo "en MSI", resolver ese dispositivo
            override_device = None
            if device_hint:
                override_device = self._resolve_device(device_hint)
                if not override_device:
                    return {
                        "success":  False,
                        "action":   "activate_mode",
                        "response": f"No encontré el dispositivo '{device_hint}'. "
                                    f"Di 'escanea la red' primero.",
                        "data":     {},
                    }

            # Lanzar todas las apps del modo
            launched = []
            failed   = []

            for app in mode.apps:
                # El device_id se toma del override de voz, o del que tenga la app,
                # o se resuelve al primer dispositivo online
                target_device = override_device or app.device_id or self._resolve_device(None)

                if not target_device:
                    failed.append(f"{app.app_name}@sin_dispositivo")
                    continue

                command = {
                    "action":   "open_app",
                    "app_name": app.app_name,
                    "app_path": app.app_name,   # Solo nombre: el cliente resuelve la ruta
                    "mode_id":  mode.id,
                    "mode_name":mode.name,
                }
                if self.mqtt and self.mqtt.publish_command(target_device, command):
                    launched.append(f"{app.app_name}@{target_device}")
                else:
                    failed.append(f"{app.app_name}@{target_device}")

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

        Estrategia (mejorada con fuzzy matching):
          1. Si hay hint:
             a. Match exacto por substring (case-insensitive)
             b. Fuzzy matching con difflib sobre hostnames
          2. Si no hay hint o no hay match: usa el primer dispositivo online.
          3. Si no hay ningún dispositivo online: retorna None.

        Args:
            hint: String opcional del usuario (ej. "MSI", "laptop", "gaming").

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
            # Limpiar artículos y posesivos del hint
            hint_clean = hint.lower()
            for word in ("el ", "mi ", "la ", "los ", "las ", "del "):
                hint_clean = hint_clean.replace(word, "")
            hint_clean = hint_clean.strip()

            # ── Paso A: Match por substring ─────────────────────────────────
            for device_id in online:
                if hint_clean in device_id.lower():
                    logger.info(f"[Orchestrator] Device match (substring): '{hint}' → '{device_id}'")
                    return device_id

            # ── Paso B: Fuzzy matching con difflib ──────────────────────────
            # Busca el hostname más similar al hint
            device_ids = list(online.keys())
            best_match = difflib.get_close_matches(
                hint_clean,
                [d.lower() for d in device_ids],
                n=1,
                cutoff=0.4,    # Umbral bajo: "MSI" debe matchear "MSI-GF63"
            )
            if best_match:
                # Encontrar el device_id original (case-preserving)
                for device_id in device_ids:
                    if device_id.lower() == best_match[0]:
                        logger.info(f"[Orchestrator] Device match (fuzzy): '{hint}' → '{device_id}'")
                        return device_id

            # ── Paso C: Match parcial por tokens del hostname ───────────────
            # "gaming" matchea "DESKTOP-GAMING-01" separando por guiones
            for device_id in device_ids:
                tokens = device_id.lower().replace("-", " ").replace("_", " ").split()
                if hint_clean in tokens:
                    logger.info(f"[Orchestrator] Device match (token): '{hint}' → '{device_id}'")
                    return device_id

            logger.warning(f"[Orchestrator] No se encontró dispositivo para hint: '{hint}'")

        # Fallback: primer dispositivo online (orden de inserción en Python 3.7+)
        return next(iter(online))

    # ══════════════════════════════════════════════════
    #  HANDLERS DE MÚSICA Y MULTIMEDIA
    # ══════════════════════════════════════════════════

    async def _handle_play_music(self, match: re.Match) -> Dict[str, Any]:
        """
        Abre Spotify y reproduce música en el dispositivo indicado.
        Si no se indica dispositivo, usa el primero disponible.

        Match grupos:
          group(1) → query a buscar (ej. "taylor swift")
          group(2) → hint del dispositivo, opcional (ej. "MSI")
        """
        query       = match.group(1).strip() if match.group(1) else ""
        
        # ── Corrección Fonética (Spanglish) ───────────────────────────────────
        # El modelo Vosk en español fuerza las palabras en inglés a sonar como español.
        # Aquí mapeamos los errores más comunes para que Spotify los entienda.
        phonetic_map = {
            "way destruir": "wildest dreams",
            "fieles": "fearless",
            "reputación": "reputation",
            "cruel somer": "cruel summer",
            "loc history": "love story",
            "lo que stori": "love story",
            "sheik it off": "shake it off",
            "blank space": "blank space",
            "bad blod": "bad blood",
            "yugilón wimi": "you belong with me"
        }
        for wrong, right in phonetic_map.items():
            query = query.replace(wrong, right)
            
        device_hint = match.group(2).strip() if match.group(2) else None
        device_id   = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "play_music",
                "response": "No hay dispositivos conectados para reproducir música.",
                "data":     {},
            }

        command = {
            "action":   "play_music",
            "service":  "spotify",
            "query":    query,
        }

        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False
        
        if query:
            response_text = f"Reproduciendo {query}"
        else:
            response_text = "Reproduciendo música"

        return {
            "success":  success,
            "action":   "play_music",
            "response": f"{response_text} en {device_id}." if success
                        else f"No pude conectar con {device_id}.",
            "data": {"device": device_id, "service": "spotify", "query": query},
        }

    async def _handle_web_search(self, match: re.Match) -> Dict[str, Any]:
        """Realiza una búsqueda en Google en el dispositivo."""
        query       = match.group(1).strip() if match.group(1) else ""
        device_hint = match.group(2).strip() if match.group(2) else None
        device_id   = self._resolve_device(device_hint)

        if not query:
            return {
                "success":  False,
                "action":   "web_search",
                "response": "¿Qué quieres que busque?",
                "data":     {},
            }

        if not device_id:
            return {
                "success":  False,
                "action":   "web_search",
                "response": "No hay dispositivos conectados para buscar.",
                "data":     {},
            }

        command = {"action": "web_search", "query": query}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "web_search",
            "response": f"Buscando {query} en {device_id}." if success
                        else f"No pude conectar con {device_id}.",
            "data": {"query": query, "device": device_id},
        }

    async def _handle_media_control(self, match: re.Match) -> Dict[str, Any]:
        """
        Controla la reproducción multimedia (pause, next, prev, volumen).
        Usa media keys del sistema operativo.
        """
        full_text   = match.group(0).lower().strip()
        device_hint = match.group(1).strip() if match.group(1) else None
        device_id   = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "media_control",
                "response": "No hay dispositivos conectados.",
                "data":     {},
            }

        # Determinar la acción de media según el texto capturado
        if any(w in full_text for w in ("pausa", "para", "detén", "detiene")):
            control = "pause"
            response_text = "Pausando la música"
        elif any(w in full_text for w in ("continua", "resume")):
            control = "play"
            response_text = "Continuando la música"
        elif any(w in full_text for w in ("siguiente", "salta")):
            control = "next"
            response_text = "Siguiente canción"
        elif any(w in full_text for w in ("anterior", "atrás")):
            control = "prev"
            response_text = "Canción anterior"
        elif any(w in full_text for w in ("sube", "más")):
            control = "volume_up"
            response_text = "Subiendo volumen"
        elif any(w in full_text for w in ("baja", "menos")):
            control = "volume_down"
            response_text = "Bajando volumen"
        else:
            control = "play_pause"
            response_text = "Alternando reproducción"

        command = {"action": "media_control", "control": control}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "media_control",
            "response": f"{response_text} en {device_id}." if success
                        else "No pude conectar con el dispositivo.",
            "data": {"control": control, "device": device_id},
        }

    # ── Utilidades ────────────────────────────────────────────────────────────

    async def _handle_chat_ai(self, match: re.Match) -> Dict[str, Any]:
        """
        Intención de fallback: Usa Google Gemini para responder de forma conversacional.
        """
        query = match.group(1).strip()
        api_key = os.getenv("GEMINI_API_KEY")
        
        if not api_key:
            return {
                "success": False,
                "action": "chat_ai",
                "response": "No tengo configurada mi clave de inteligencia artificial.",
                "data": {"query": query}
            }
            
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            
            # Usar el modelo gemini-pro que es más compatible
            model = genai.GenerativeModel(
                'gemini-pro', 
                system_instruction="Eres Nika, un asistente virtual de PC inteligente, amigable y muy conciso. Da respuestas muy breves y directas en español (1 o 2 oraciones máximo) porque vas a ser leída en voz alta por un sintetizador de voz. No uses asteriscos ni markdown."
            )
            # Ejecutar de forma síncrona en un hilo para no bloquear el loop asyncio, o simplemente directo
            response = model.generate_content(query)
            text_response = response.text.strip()
            
            # Limpiar posibles asteriscos residuales de markdown para que espeak no los lea
            text_response = text_response.replace("*", "").replace("#", "")
            
            return {
                "success": True,
                "action": "chat_ai",
                "response": text_response,
                "data": {"query": query, "response": text_response}
            }
            
        except ImportError:
            import logging
            logger = logging.getLogger("nika.orchestrator")
            logger.error("[Orchestrator] Librería google-generativeai no instalada.")
            return {
                "success": False,
                "action": "chat_ai",
                "response": "Me falta un módulo interno para poder pensar.",
                "data": {"query": query}
            }
        except Exception as e:
            import logging
            logger = logging.getLogger("nika.orchestrator")
            logger.error(f"[Orchestrator] Error en Gemini API: {e}")
            return {
                "success": False,
                "action": "chat_ai",
                "response": "Lo siento, tuve un problema conectándome a mi cerebro.",
                "data": {"query": query, "error": str(e)}
            }

    def get_history(self) -> list:
        """Retorna el historial de comandos procesados (últimos 50)."""
        return list(self._command_history)
