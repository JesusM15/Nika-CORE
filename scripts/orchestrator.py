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

    # ── RECORDATORIOS Y ALARMAS (prioridad alta para evitar conflictos con 'pon') ──
    # Matches: "recuerdame la reunión a las 3", "pon alarma mañana 8", "que recordatorios tengo"
    ("set_reminder", re.compile(
        r"^(?:rec[uiu]erdame?|pon(?:me)?(?:\s+un[ao]?)?\s+(?:alarma|recordatorio|temporizador|timer)|agend[ae]|prog?r[ae]ma|avisa(?:me)?(?:\s+que)?|recordar|timer|temporizador)\b"
        r"(.+)$",
        re.IGNORECASE,
    )),
    ("list_reminders", re.compile(
        r"(?:qu[eé]\s+recordatorios|mis\s+recordatorios|qu[eé]\s+tengo(?:\s+pendiente)?|lista\s+de\s+(?:alarmas|recordatorios|temporizadores|timers)|ver\s+alarmas|alarmas\s+pendientes|ver\s+timers|ver\s+temporizadores)",
        re.IGNORECASE,
    )),
    ("cancel_reminder", re.compile(
        r"(?:cancela|borra|elimina|quita)\s+(?:el\s+|la\s+|ese\s+|esa\s+)?(?:[uú]ltimo\s+)?(?:recordatorio|alarma|timer|temporizador)",
        re.IGNORECASE,
    )),

    # ── ACTIVAR MODO (prioridad máxima — evita conflicto con "abre") ───────────
    # Matches: "modo trabajo", "activa el modo gaming", "cambia a modo estudio en MSI"
    ("activate_mode", re.compile(
        r"^(?:(?:activa|abre|inicia|lanza|pon|enciende|cambia|ejecuta)\b\s*(?:a\s+)?(?:el\s+)?)?"
        r"modo\s+"
        r"(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CONTROL DE MÚSICA (antes de open_app para que "pon música" no abra "música") ─
    # Matches: "pon música", "reproduce una cancion de taylor", "quiero escuchar a the weeknd"
    ("play_music", re.compile(
        r"^(?:pon|coloca|reproduce|reproducir|toca|lanza|escucha(?:r)?)\b\s*"
        r"(?:(?:me\s+)?(?:la\s+|una\s+|un\s+)?(?:m[uú]sica|algo\s+de|canci[oó]n|pista|rola)(?:\s+de)?|(?:el\s+|un\s+)?(?:[aá]lbum|disco)|(?:el\s+|un\s+)?artista|a\s+)?\s*"
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
    # Matches: "abreme spotify", "puedes arrancar word", "habre el navegador"
    ("open_app", re.compile(
        r"^(?:[aá]bre|habre|inicia|ejecuta|lanza|pon|enciende|arranca|mu[eé]stra)\b\s*(?:me\s+)?(.+?)(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── CERRAR APLICACIÓN ──────────────────────────────────────────────────────
    # Matches: "cierrame spotify", "por favor sierra chrome", "quita el juego"
    ("close_app", re.compile(
        r"^(?:cierra|sierra|det[eé]n|detiene|para|mata|quita|apaga)\b\s*(?:me\s+)?(.+?)(?:\s+en\s+(.+))?$",
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

    # ── REDACTAR EMAIL ─────────────────────────────────────────────────────────
    # Matches: "redacta un correo a juan", "escribe un email"
    ("send_email", re.compile(
        r"^(?:redacta|escribe|manda|env[ií]a)\b\s*(?:me\s+)?(?:.*?)(?:correo|email|e-mail|mensaje)(?:\s+a\s+(.+?))?(?:\s+(?:con\s+(?:el\s+)?asunto|sobre)\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── BÚSQUEDA WEB ───────────────────────────────────────────────────────────
    # Matches: "busca en google recetas", "googlea sobre perros", "investiga"
    ("web_search", re.compile(
        r"^(?:b[uú]sca|buscar|investiga|googlea)\b\s*(?:me\s+)?(?:lo\s+)?"
        r"(?:(?:algo\s+)?(?:en\s+)?(?:google|internet|la\s+web)\s+(?:sobre\s+|de\s+)?)?"
        r"(.*?)"
        r"(?:\s+en\s+(.+))?$",
        re.IGNORECASE,
    )),

    # ── ESTADO DEL SISTEMA ─────────────────────────────────────────────────────
    # Matches: "cómo estás", "estás ahí", "estado del sistema"
    ("system_status", re.compile(
        r"(?:(?:puedes\s+|podr[ií]as\s+)?(?:dime\s+|darme\s+)?(?:el\s+)?estado(?:\s+del\s+sistema)?|"
        r"c[oó]mo\s+est[aá]s|qu[eé]\s+est[aá]s\s+haciendo|informe|status|est[aá]s\s+ah[ií])",
        re.IGNORECASE,
    )),
]


import random

# ══════════════════════════════════════════════════════
#  BANCO DE RESPUESTAS ORGÁNICAS
#  Variedad de frases para que Nika no suene robótica.
#  Uso: _pick(RESPONSES["open_app"]["ok"], app="Spotify")
# ══════════════════════════════════════════════════════

def _pick(phrases: list, **kwargs) -> str:
    """Elige una frase al azar del listado y la formatea con kwargs."""
    return random.choice(phrases).format(**kwargs)


def _safe_group(match: re.Match, n: int, default: str = "") -> str:
    """
    Accede a match.group(n) de forma segura.
    Retorna 'default' si el grupo no existe o es None.
    Evita IndexError cuando el match viene de la capa semántica
    (que genera matches con menos grupos que los handlers esperan).
    """
    try:
        val = match.group(n)
        return val.strip() if val else default
    except (IndexError, AttributeError):
        return default

RESPONSES = {
    "open_app": {
        "ok": [
            "Listo, abriendo {app}.",
            "Abriendo {app} ahora mismo.",
            "Ya te abro {app}.",
            "{app} en camino.",
            "De acuerdo, iniciando {app}.",
        ],
        "fail": [
            "No pude abrir {app}. Verifica que esté instalado.",
            "Hubo un problema al abrir {app}.",
            "No encontré {app} en la lista de apps.",
            "Fallo al intentar abrir {app}.",
        ],
        "no_device": [
            "No hay dispositivos disponibles para abrir {app}. ¿Di 'escanea la red' primero?",
            "No encontré ningún dispositivo conectado. Intenta escanear la red.",
        ],
    },
    "close_app": {
        "ok": [
            "Cerrando {app}.",
            "Listo, cerrando {app}.",
            "Apagando {app} ahora.",
            "{app} cerrada.",
            "De acuerdo, cerrando {app}.",
        ],
        "fail": [
            "No pude cerrar {app}. Puede que ya esté cerrada.",
            "Tuve un problema cerrando {app}.",
            "No logré cerrar {app}.",
        ],
        "no_device": [
            "No hay dispositivos conectados para cerrar {app}.",
            "No encontré ningún dispositivo activo.",
        ],
    },
    "play_music": {
        "ok_query": [
            "Reproduciendo {query}.",
            "Poniendo {query} ahora.",
            "Dale, aquí va {query}.",
            "Buscando y reproduciendo {query}.",
            "Listo, poniendo {query}.",
        ],
        "ok_noquery": [
            "Reproduciendo música.",
            "Aquí va la música.",
            "Listo, dale play.",
            "Música en camino.",
        ],
        "fail": [
            "No pude conectar con el dispositivo para reproducir.",
            "Hubo un problema al intentar reproducir música.",
            "Fallo al conectar con Spotify.",
        ],
    },
    "media_control": {
        "pause": [
            "Pausado.",
            "Música en pausa.",
            "Listo, pausé la música.",
            "Pausa activada.",
        ],
        "play": [
            "Continuando.",
            "Reanudando la música.",
            "Aquí vamos de nuevo.",
            "Play.",
        ],
        "next": [
            "Siguiente canción.",
            "Saltando a la siguiente.",
            "Cambiando de canción.",
            "Siguiente.",
        ],
        "prev": [
            "Canción anterior.",
            "Regresando a la anterior.",
            "Volviendo atrás.",
        ],
        "volume_up": [
            "Subiendo el volumen.",
            "Más volumen.",
            "Subiendo.",
        ],
        "volume_down": [
            "Bajando el volumen.",
            "Menos volumen.",
            "Bajando.",
        ],
        "toggle": [
            "Alternando reproducción.",
            "Play, pause.",
        ],
        "fail": [
            "No pude controlar la reproducción.",
            "Hubo un error con el control multimedia.",
        ],
    },
    "web_search": {
        "ok": [
            "Buscando {query} ahora.",
            "Dale, busco {query}.",
            "Abriendo Google con {query}.",
            "Aquí va tu búsqueda de {query}.",
        ],
        "fail": [
            "No pude abrir el navegador.",
            "Tuve un problema al buscar {query}.",
        ],
        "no_query": [
            "¿Qué quieres que busque?",
            "Dime qué busco.",
        ],
    },
    "send_email": {
        "ok": [
            "Abriendo tu cliente de correo.",
            "Listo, abriendo el correo para {recipient}.",
            "Aquí va el borrador.",
            "Abriendo correo para redactar.",
        ],
        "fail": [
            "No pude abrir el cliente de correo.",
            "Hubo un error al abrir el correo.",
        ],
    },
    "reminder": {
        "ok": [
            "{message}",
        ],
        "fail": [
            "{message}",
        ],
    },
    "system_status": {
        "ok": [
            "Todo funcionando bien. Estoy activa y lista.",
            "Sistema nominal. Aquí estoy.",
            "Sin problemas. Todo en orden.",
            "Operativa al cien por ciento.",
        ],
    },
    "shutdown": {
        "ok": [
            "Apagando el equipo en {delay} segundos.",
            "Iniciando apagado en {delay} segundos.",
        ],
        "fail": [
            "No pude apagar el dispositivo.",
        ],
    },
}

# Frases de fallback cuando NO se entiende el comando en absoluto
FALLBACK_RESPONSES = [
    "No entendí eso. Prueba: 'abre una app', 'reproduce música', o 'busca algo'.",
    "¿Puedes repetirlo? No capturé bien el comando.",
    "No reconocí ese comando. Di 'modo trabajo' o 'abre chrome' por ejemplo.",
    "Hmm, no estoy segura de lo que pediste. Intenta de nuevo.",
    "No te entendí. Prueba con un comando más simple.",
]
_fallback_idx = 0



def _split_reminder_text_and_time(phrase: str) -> Tuple[str, str]:
    """
    Divide una frase en español en el texto del recordatorio y la especificación de tiempo.
    Ejemplo: "sacar la basura en 10 minutos" -> ("sacar la basura", "en 10 minutos")
             "timer de 5 minutos" -> ("", "de 5 minutos")
    """
    phrase = phrase.strip()
    
    # Expresiones para buscar el inicio de la especificación de tiempo en español
    time_indicators = [
        r'\ben\s+\d+',                      # "en 5 minutos"
        r'\ba\s+las?\s+\d+',                # "a las 3", "a la 1"
        r'\bpara\s+las?\s+\d+',            # "para las 5"
        r'\bmañana\b',                     # "mañana", "mañana a las 8"
        r'\bel\s+(?:lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\b', # "el lunes..."
        r'\bde\s+\d+\s*(?:segundo|minuto|hora)' # "de 5 minutos", "de 10 segundos" (timers)
    ]
    
    earliest_idx = len(phrase)
    
    for pattern in time_indicators:
        match = re.search(pattern, phrase, re.IGNORECASE)
        if match and match.start() < earliest_idx:
            earliest_idx = match.start()
            
    if earliest_idx < len(phrase):
        text = phrase[:earliest_idx].strip()
        time_part = phrase[earliest_idx:].strip()
        # Limpiar palabras conectoras residuales al inicio y final del texto (ej. "para", "de", "que", "a")
        text = re.sub(r'^(?:para|de|a|que)\s+', '', text, flags=re.IGNORECASE).strip()
        text = re.sub(r'\b(para|de|que|a)\b$', '', text, flags=re.IGNORECASE).strip()
        return text, time_part
    else:
        return "", phrase



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

        # Inicializar servicio de recordatorios y alarmas
        from scripts.reminders import get_reminder_service
        self.reminders = get_reminder_service(mqtt_manager=mqtt_manager)

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
                "open_app":        self._handle_open_app,
                "close_app":       self._handle_close_app,
                "activate_mode":   self._handle_activate_mode,
                "play_music":      self._handle_play_music,
                "media_control":   self._handle_media_control,
                "web_search":      self._handle_web_search,
                "shutdown_device": self._handle_shutdown_device,
                "scan_devices":    self._handle_scan_devices,
                "system_status":   self._handle_system_status,
                "send_email":      self._handle_send_email,
                "set_reminder":    self._handle_set_reminder,
                "list_reminders":  self._handle_list_reminders,
                "cancel_reminder": self._handle_cancel_reminder,
                "chat_ai":         self._handle_chat_ai,
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
        Pipeline de 3 etapas:
          1. Normalización NLU (limpia muletillas, typos de STT)
          2. Regex estructurado (intenciones precisas)
          3. Red de seguridad semántica (keywords fuzzy → intent forzado)
        """
        normalized = text.lower().strip()

        # ── ETAPA 1: NORMALIZACIÓN NLU ─────────────────────────────────────────
        # a) Elimina muletillas y cortesía al inicio
        normalized = re.sub(
            r'^(?:oye|mira|nika|nica|nyka|por\s+favor|puedes|podrias|podrías|'
            r'quiero(?:\s+que)?|necesito(?:\s+que)?|me\s+gustar[ií]a|'
            r'trata\s+de|intenta|a\s+ver|eh|um|eh+|mm+)\s+',
            '', normalized
        ).strip()

        # b) Corrige errores ortográficos típicos de Vosk/STT
        normalized = re.sub(r'\bunn+\b', 'un', normalized)
        normalized = re.sub(r'\bunna+\b', 'una', normalized)
        normalized = re.sub(r'\bporf[ao]vor\b', 'por favor', normalized)
        normalized = re.sub(r'\b(haber|aber)\b', 'a ver', normalized)
        normalized = re.sub(r'\b(manda|redacta|escribe|abre|habre|cierra|sierra|'
                            r'pon|reproduce|busca|investiga|apaga|enciende)'
                            r'(me|lo|la|le)\b', r'\1 \2', normalized)

        normalized = normalized.strip()
        logger.info(f"[Orchestrator] NLU→ '{normalized}'")

        # ── ETAPA 2: REGEX ESTRUCTURADO ────────────────────────────────────────
        for intent_name, pattern in INTENT_PATTERNS:
            if intent_name == "chat_ai":
                continue  # lo aplicamos SOLO si ningún otro matchea
            match = pattern.search(normalized)
            if match:
                logger.debug(f"[Orchestrator] Intención regex: '{intent_name}'")
                return intent_name, match

        # ── ETAPA 3: RED DE SEGURIDAD SEMÁNTICA (antes de Gemini) ─────────────
        # Diccionario de palabras clave → intent forzado.
        # Si una palabra clave (o variante fuzzy) aparece en el texto normalizado,
        # re-construimos un match artificial y retornamos el intent correcto.
        KEYWORD_INTENTS: list[Tuple[str, list[str]]] = [
            ("play_music",    ["reproduce", "reproducir", "pon", "toca", "cancion", "musica",
                               "música", "cancio", "spotify", "artista", "album", "disco",
                               "escucha", "escuchar", "pista", "rola", "cantar", "canta"]),
            ("media_control", ["pausa", "pausar", "para", "silencio", "callate", "detén",
                               "siguiente", "salta", "anterior", "atras", "sube", "baja",
                               "volumen", "continua", "reanuda", "resume", "play"]),
            ("open_app",      ["abre", "abrir", "habre", "arranca", "arrancame", "inicia",
                               "ejecuta", "lanza", "enciende", "muestra"]),
            ("close_app",     ["cierra", "sierra", "cerrar", "mata", "quita", "detiene",
                               "apaga", "termina"]),
            ("send_email",    ["correo", "email", "e-mail", "mensaje", "redacta", "escribe",
                               "manda", "envia", "envía", "mándame", "mandame"]),
            ("web_search",    ["busca", "buscar", "busco", "googlea", "investiga", "google",
                               "busqueda", "búsqueda", "internet"]),
            ("system_status", ["estado", "estatus", "como estas", "cómo estás", "status",
                               "informe", "estas ahi", "estás ahí"]),
        ]

        def _fuzzy_in(word: str, candidates: list[str], threshold: float = 0.78) -> bool:
            """Retorna True si 'word' es fuzzy-similar a alguno de los candidates."""
            for cand in candidates:
                if cand in word or word in cand:
                    return True
                max_len = max(len(word), len(cand))
                if max_len == 0:
                    continue
                dist = _levenshtein_simple(word, cand)
                if 1.0 - dist / max_len >= threshold:
                    return True
            return False

        def _levenshtein_simple(s1: str, s2: str) -> int:
            if len(s1) < len(s2):
                s1, s2 = s2, s1
            if not s2:
                return len(s1)
            prev = list(range(len(s2) + 1))
            for i, c1 in enumerate(s1):
                curr = [i + 1]
                for j, c2 in enumerate(s2):
                    curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(c1 != c2)))
                prev = curr
            return prev[-1]

        tokens = normalized.split()
        for intent_name, keywords in KEYWORD_INTENTS:
            for token in tokens:
                if len(token) < 3:
                    continue
                if _fuzzy_in(token, keywords):
                    logger.info(
                        f"[Orchestrator] ⚡ Semántica salvó '{intent_name}': "
                        f"token='{token}' en texto='{normalized}'"
                    )
                    # dummy_match con 2 grupos: group(1)=query/nombre, group(2)=None (sin device)
                    dummy_pattern = re.compile(r"^(.*?)()$", re.IGNORECASE)
                    dummy_match = dummy_pattern.match(normalized)
                    return intent_name, dummy_match

        # ── FALLBACK a chat_ai ─────────────────────────────────────────────────
        logger.warning(f"[Orchestrator] Sin intent estructurado → chat_ai: '{text}'")
        chat_pattern = re.compile(r"^(.*?)()$", re.IGNORECASE)
        chat_match = chat_pattern.match(normalized)
        return "chat_ai", chat_match



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
        app_name    = _safe_group(match, 1)
        device_hint = _safe_group(match, 2) or None

        # Limpiar artículos del nombre capturado (ej. "el chrome" → "chrome")
        app_name = re.sub(r'^(?:el|la|los|las|un|una|lo)\s+', '', app_name, flags=re.IGNORECASE).strip()

        # Fix fonético de apps comunes
        app_name_lower = app_name.lower()
        if app_name_lower in ("crohn", "crome", "cron", "krome"):
            app_name = "chrome"
            
        device_id = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "open_app",
                "response": _pick(RESPONSES["open_app"]["no_device"], app=app_name),
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
            "response": _pick(RESPONSES["open_app"]["ok"], app=app_name) if success
                        else _pick(RESPONSES["open_app"]["fail"], app=app_name),
            "data": {"app": app_name, "device": device_id},
        }

    async def _handle_close_app(self, match: re.Match) -> Dict[str, Any]:
        """Cierra una aplicación en el dispositivo indicado."""
        app_name    = _safe_group(match, 1)
        device_hint = _safe_group(match, 2) or None
        device_id   = self._resolve_device(device_hint)

        # Limpiar artículos del nombre (ej. "la sierra bloc de notas" → "bloc de notas")
        app_name = re.sub(r'^(?:el|la|los|las|un|una|lo)\s+(?:sierra|cierra\s+)?', '', app_name, flags=re.IGNORECASE).strip()
        app_name = re.sub(r'^(?:el|la|los|las|un|una|lo)\s+', '', app_name, flags=re.IGNORECASE).strip()

        if not device_id:
            return {
                "success":  False,
                "action":   "close_app",
                "response": _pick(RESPONSES["close_app"]["no_device"], app=app_name),
                "data":     {},
            }

        command = {"action": "close_app", "app_name": app_name}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "close_app",
            "response": _pick(RESPONSES["close_app"]["ok"], app=app_name) if success
                        else _pick(RESPONSES["close_app"]["fail"], app=app_name),
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
        mode_name   = _safe_group(match, 1)
        device_hint = _safe_group(match, 2) or None

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
                "response": _pick(RESPONSES["shutdown"]["fail"]),
                "data":     {},
            }

        command = {"action": "shutdown"}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "shutdown_device",
            "response": _pick(RESPONSES["shutdown"]["ok"], delay=30) if success
                        else _pick(RESPONSES["shutdown"]["fail"]),
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
            response = _pick(RESPONSES["system_status"]["ok"])
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
        query       = _safe_group(match, 1)

        # query contiene todo lo que match capturó — limpiar prefijos que el regex pudo
        # dejar si viene de la capa semántica (ej. "me reproduce billie eilish")
        query = re.sub(r'^(?:me\s+)?(?:reproduce|pon|toca|lanza|coloca|escucha)\s+', '', query, flags=re.IGNORECASE)
        query = re.sub(r'^(?:la\s+|una\s+|un\s+)?(?:m[uú]sica|canci[oó]n|pista|rola)\s+de\s+', '', query, flags=re.IGNORECASE)
        query = query.strip()
        
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
            "yugilón wimi": "you belong with me",
            "repite ella": "ready for it",
            "mil novecientos ochenta y nueve": "1989",
            "folclor": "folklore",
            "1989": "1989" # just in case
        }
        for wrong, right in phonetic_map.items():
            query = query.replace(wrong, right)
            
        # Determinar si el usuario pidió un álbum, artista o canción
        full_match = match.group(0).lower()
        search_type = "track"
        if any(w in full_match for w in ("álbum", "album", "disco")):
            search_type = "album"
        elif any(w in full_match for w in ("artista", "grupo", "banda")):
            search_type = "artist"
            
        device_hint = _safe_group(match, 2) or None
        device_id   = self._resolve_device(device_hint)

        if not device_id:
            return {
                "success":  False,
                "action":   "play_music",
                "response": _pick(RESPONSES["play_music"]["fail"]),
                "data":     {},
            }

        command = {
            "action":   "play_music",
            "service":  "spotify",
            "query":    query,
        }

        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        if success:
            response_text = _pick(RESPONSES["play_music"]["ok_query"], query=query) if query \
                            else _pick(RESPONSES["play_music"]["ok_noquery"])
        else:
            response_text = _pick(RESPONSES["play_music"]["fail"])

        return {
            "success":  success,
            "action":   "play_music",
            "response": response_text,
            "data": {"device": device_id, "service": "spotify", "query": query},
        }

    async def _handle_web_search(self, match: re.Match) -> Dict[str, Any]:
        """Realiza una búsqueda en Google en el dispositivo."""
        query       = _safe_group(match, 1)
        # Limpiar prefijos de búsqueda si viene de la capa semántica
        query = re.sub(r'^(?:me\s+)?(?:busca|buscar|investiga|googlea)\s+', '', query, flags=re.IGNORECASE).strip()
        device_hint = _safe_group(match, 2) or None
        device_id   = self._resolve_device(device_hint)

        if not query:
            return {
                "success":  False,
                "action":   "web_search",
                "response": _pick(RESPONSES["web_search"]["no_query"]),
                "data":     {},
            }

        if not device_id:
            return {
                "success":  False,
                "action":   "web_search",
                "response": _pick(RESPONSES["web_search"]["fail"], query=query),
                "data":     {},
            }

        command = {"action": "web_search", "query": query}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        return {
            "success":  success,
            "action":   "web_search",
            "response": _pick(RESPONSES["web_search"]["ok"], query=query) if success
                        else _pick(RESPONSES["web_search"]["fail"], query=query),
            "data": {"query": query, "device": device_id},
        }

    async def _handle_send_email(self, match: re.Match) -> Dict[str, Any]:
        """Abre el cliente de correo predeterminado para redactar un email."""
        recipient = _safe_group(match, 1)
        subject   = _safe_group(match, 2)
        device_id = self._resolve_device(None)

        command = {"action": "send_email", "to": recipient, "subject": subject}
        success = self.mqtt.publish_command(device_id, command) if self.mqtt else False

        if success:
            resp = _pick(RESPONSES["send_email"]["ok"], recipient=recipient or "nadie")
        else:
            resp = _pick(RESPONSES["send_email"]["fail"])

        return {
            "success": success,
            "action":  "send_email",
            "response": resp,
            "data": {"to": recipient, "subject": subject}
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

    # ── Recordatorios, Alarmas y Temporizadores ────────────────────────────────

    async def _handle_set_reminder(self, match: re.Match) -> Dict[str, Any]:
        """Programa un recordatorio, alarma o temporizador."""
        phrase = match.group(1).strip()
        text, when_str = _split_reminder_text_and_time(phrase)
        
        # Si no hay texto, deducimos el tipo (Temporizador, Alarma, Recordatorio)
        orig = match.group(0).lower()
        if not text:
            if "timer" in orig or "temporizador" in orig:
                text = "Temporizador"
            elif "alarma" in orig:
                text = "Alarma"
            else:
                text = "Recordatorio"
                
        res = self.reminders.add(text=text, when_str=when_str)
        return {
            "success": res["ok"],
            "action": "set_reminder",
            "response": res["message"],
            "data": {"text": text, "when": res.get("when")} if res["ok"] else {}
        }

    async def _handle_list_reminders(self, match: re.Match) -> Dict[str, Any]:
        """Lista los próximos recordatorios, alarmas y temporizadores."""
        from datetime import datetime
        upcoming = self.reminders.list_upcoming(limit=5)
        if not upcoming:
            return {
                "success": True,
                "action": "list_reminders",
                "response": "No tienes recordatorios ni temporizadores pendientes.",
                "data": {"reminders": []}
            }
            
        phrases = []
        now = datetime.now()
        for r in upcoming:
            dt = datetime.fromtimestamp(r["when_ts"])
            diff_secs = int(r["when_ts"] - now.timestamp())
            
            # Formato premium para leer en voz alta
            if r["text"].lower() in ("timer", "temporizador"):
                if diff_secs < 60:
                    time_desc = f"un temporizador al que le quedan {diff_secs} segundos"
                elif diff_secs < 3600:
                    mins = diff_secs // 60
                    time_desc = f"un temporizador al que le quedan {mins} minuto{'s' if mins > 1 else ''}"
                else:
                    horas = diff_secs // 3600
                    time_desc = f"un temporizador al que le quedan {horas} hora{'s' if horas > 1 else ''}"
                phrases.append(time_desc)
            elif r["text"].lower() == "alarma":
                phrases.append(f"una alarma hoy a las {dt.strftime('%H:%M')}")
            else:
                dia = "hoy" if dt.date() == now.date() else "el " + dt.strftime("%d/%m")
                phrases.append(f"'{r['text']}' {dia} a las {dt.strftime('%H:%M')}")
                
        if len(phrases) == 1:
            resp = f"Tienes un recordatorio pendiente: {phrases[0]}."
        else:
            resp = f"Tienes {len(phrases)} recordatorios pendientes: " + ", ".join(phrases[:-1]) + " y " + phrases[-1] + "."
            
        return {
            "success": True,
            "action": "list_reminders",
            "response": resp,
            "data": {"reminders": upcoming}
        }

    async def _handle_cancel_reminder(self, match: re.Match) -> Dict[str, Any]:
        """Cancela el recordatorio, alarma o temporizador más cercano."""
        res = self.reminders.cancel_last()
        return {
            "success": res["ok"],
            "action": "cancel_reminder",
            "response": res["message"],
            "data": {}
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
            from google import genai
            
            client = genai.Client(api_key=api_key)
            
            prompt = (
                "Eres Nika, un asistente virtual de PC inteligente, amigable y muy conciso. "
                "Da respuestas muy breves y directas en español (1 o 2 oraciones máximo) "
                "porque vas a ser leída en voz alta por un sintetizador de voz. "
                "No uses asteriscos ni formato markdown.\n\n"
                f"Usuario: {query}"
            )
            
            # Intentar primero con el modelo más nuevo (2.5), luego 1.5, luego pro clásico
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                )
            except Exception as e:
                try:
                    response = client.models.generate_content(
                        model='gemini-1.5-flash',
                        contents=prompt,
                    )
                except Exception:
                    response = client.models.generate_content(
                        model='gemini-pro',
                        contents=prompt,
                    )
                
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
            logger.error("[Orchestrator] Librería google-genai no instalada.")
            return {
                "success": False,
                "action": "chat_ai",
                "response": "Me falta un módulo interno para poder pensar.",
                "data": {"query": query}
            }
        except Exception as e:
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
