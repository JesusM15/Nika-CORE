"""
scripts/reminders.py — Servicio de Recordatorios y Alarmas de Nika OS
======================================================================
Motor de recordatorios persistentes que:
  1. Guarda recordatorios en un archivo JSON local (reminders.json).
  2. Corre un hilo de fondo que verifica cada 30 s si hay recordatorios pendientes.
  3. Cuando un recordatorio vence, lo dispara vía TTS (espeak-ng o HTTP al cliente).
  4. Soporta recordatorios puntuales (fecha+hora) y alarmas recurrentes (diarias).

Uso desde orchestrator.py:
    from scripts.reminders import ReminderService
    svc = ReminderService()                  # singleton, crea el hilo internamente
    svc.add(text="Reunión con Juan", when_str="15:30")
    svc.add(text="Tomar medicamento",  when_str="mañana 08:00")
    svc.list_upcoming()  → lista de dicts
"""

import json
import re
import threading
import time
import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nika.reminders")

# ── Ruta del archivo JSON de recordatorios ────────────────────────────────────
REMINDERS_FILE = Path(__file__).parent.parent / "data" / "reminders.json"
REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── Intervalo de verificación del hilo (segundos) ────────────────────────────
CHECK_INTERVAL = 1

# ── Tolerancia para disparar (un recordatorio vence dentro de ±window) ────────
FIRE_WINDOW_SECS = 5   # Tolerancia de respaldo



# ══════════════════════════════════════════════════════════════════════════════
#  PARSER DE FECHA/HORA EN ESPAÑOL COLOQUIAL
# ══════════════════════════════════════════════════════════════════════════════

def _parse_when(text: str) -> Optional[datetime]:
    """
    Convierte frases coloquiales en español a un datetime.
    Ejemplos:
      "15:30"              → hoy a las 15:30
      "a las 3 de la tarde"→ hoy a las 15:00
      "mañana 8"           → mañana a las 08:00
      "en 20 minutos"      → ahora + 20 min
      "en 2 horas"         → ahora + 2h
      "el lunes a las 10"  → próximo lunes a las 10:00
    """
    text = text.lower().strip()
    now = datetime.now()

    # ── Relativo: "(en/de) X segundos/minutos/horas" ───────────────────────────
    m = re.search(r'\b(?:en|de\s+)?(\d+)\s*(segundo|segundos|seg|s|minuto|minutos|min|m|hora|horas|h)\b', text)
    if m:
        amount = int(m.group(1))
        unit   = m.group(2)
        if unit.startswith('seg') or unit == 's':
            delta = timedelta(seconds=amount)
        elif unit.startswith('min') or unit == 'm':
            delta = timedelta(minutes=amount)
        else:
            delta = timedelta(hours=amount)
        return now + delta

    # ── Extraer hora de la cadena ("15:30", "3 de la tarde", "8 en punto") ───
    def _extract_hour(s: str) -> Optional[tuple]:
        """Retorna (hora, minuto) o None."""
        # Formato HH:MM
        hm = re.search(r'\b(\d{1,2}):(\d{2})\b', s)
        if hm:
            return int(hm.group(1)), int(hm.group(2))

        # "a las X de la tarde/noche/mañana"
        ampm = re.search(r'\b(\d{1,2})\s+(?:de\s+la\s+)?(mañana|tarde|noche)\b', s)
        if ampm:
            h = int(ampm.group(1))
            period = ampm.group(2)
            if period == 'tarde' and h < 12:
                h += 12
            elif period == 'noche' and h < 12:
                h += 12
            elif period == 'mañana' and h == 12:
                h = 0
            return h, 0

        # Solo un número que suena a hora ("a las 8", "a las 15")
        solo = re.search(r'\b(?:a\s+las?\s+)?(\d{1,2})\b', s)
        if solo:
            h = int(solo.group(1))
            if 0 <= h <= 23:
                return h, 0
        return None

    # ── Mañana ────────────────────────────────────────────────────────────────
    if 'mañana' in text:
        base = (now + timedelta(days=1)).replace(second=0, microsecond=0)
        hm   = _extract_hour(text)
        if hm:
            return base.replace(hour=hm[0], minute=hm[1])
        return base.replace(hour=8, minute=0)

    # ── Días de la semana ─────────────────────────────────────────────────────
    DAYS_ES = {
        'lunes': 0, 'martes': 1, 'miércoles': 2, 'miercoles': 2,
        'jueves': 3, 'viernes': 4, 'sábado': 5, 'sabado': 5, 'domingo': 6,
    }
    for day_name, day_num in DAYS_ES.items():
        if day_name in text:
            days_ahead = (day_num - now.weekday()) % 7 or 7
            base = (now + timedelta(days=days_ahead)).replace(second=0, microsecond=0)
            hm   = _extract_hour(text)
            if hm:
                return base.replace(hour=hm[0], minute=hm[1])
            return base.replace(hour=9, minute=0)

    # ── Hoy a cierta hora ─────────────────────────────────────────────────────
    hm = _extract_hour(text)
    if hm:
        target = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        # Si ya pasó esa hora hoy, programar para mañana
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  SERVICIO DE RECORDATORIOS (SINGLETON)
# ══════════════════════════════════════════════════════════════════════════════

class ReminderService:
    """
    Servicio de recordatorios. Se instancia una vez y maneja su propio hilo.
    Persiste los recordatorios en reminders.json.
    """
    _instance: Optional["ReminderService"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, tts_callback=None, mqtt_manager=None):
        if self._initialized:
            return
        self._initialized = True

        self._lock         = threading.Lock()
        self._reminders: list[dict] = []
        self._tts_callback = tts_callback   # función(text) para hablar
        self._mqtt         = mqtt_manager
        self._running      = True

        self._load()
        self._thread = threading.Thread(target=self._checker_loop, daemon=True, name="reminder-checker")
        self._thread.start()
        logger.info(f"[Reminders] Servicio iniciado. {len(self._reminders)} recordatorio(s) cargado(s).")

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if REMINDERS_FILE.exists():
                data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
                self._reminders = data.get("reminders", [])
                # Limpiar los que ya vencieron hace más de 1 hora
                now_ts = time.time()
                self._reminders = [
                    r for r in self._reminders
                    if not r.get("fired") or (now_ts - r.get("fire_ts", 0) < 3600)
                ]
        except Exception as e:
            logger.warning(f"[Reminders] Error cargando recordatorios: {e}")
            self._reminders = []

    def _save(self):
        try:
            REMINDERS_FILE.write_text(
                json.dumps({"reminders": self._reminders}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[Reminders] Error guardando: {e}")

    # ── API pública ───────────────────────────────────────────────────────────

    def add(self, text: str, when_str: str) -> dict:
        """
        Agrega un nuevo recordatorio o temporizador.
        Retorna un dict con 'ok' (bool) y 'message' (str para TTS).
        """
        when = _parse_when(when_str)
        if when is None:
            return {
                "ok": False,
                "message": f"No entendí cuándo programar el recordatorio. Di por ejemplo: 'en 30 minutos' o 'mañana a las 8'."
            }

        reminder = {
            "id":       int(time.time() * 1000),
            "text":     text,
            "when_str": when_str,
            "when_ts":  when.timestamp(),
            "fired":    False,
            "created":  time.time(),
        }

        with self._lock:
            self._reminders.append(reminder)
            self._save()

        # Formatear hora legible
        hora = when.strftime("%H:%M")
        dia  = "hoy" if when.date() == datetime.now().date() else when.strftime("el %d/%m")
        
        # Mensajes premium y orgánicos estilo Alexa
        if text.lower() in ("timer", "temporizador"):
            diff_secs = int(when.timestamp() - time.time())
            if diff_secs < 3600:
                mins = diff_secs // 60
                secs = diff_secs % 60
                time_parts = []
                if mins > 0:
                    time_parts.append(f"{mins} minuto{'s' if mins > 1 else ''}")
                if secs > 0:
                    time_parts.append(f"{secs} segundo{'s' if secs > 1 else ''}")
                
                time_desc = " y ".join(time_parts) if time_parts else "0 segundos"
                msg = f"Listo, pongo un temporizador de {time_desc}."
            else:
                horas = diff_secs // 3600
                msg = f"Listo, pongo un temporizador de {horas} hora{'s' if horas > 1 else ''}."
        elif text.lower() == "alarma":
            msg = f"Alarma guardada para las {hora}."
        else:
            msg = f"Recordatorio programado: '{text}' {dia} a las {hora}."

        return {
            "ok": True,
            "message": msg,
            "when": when.isoformat(),
        }

    def list_upcoming(self, limit: int = 5) -> list[dict]:
        """Retorna los próximos N recordatorios pendientes, ordenados por fecha."""
        now_ts = time.time()
        with self._lock:
            pending = [r for r in self._reminders if not r.get("fired") and r["when_ts"] > now_ts]
        pending.sort(key=lambda r: r["when_ts"])
        return pending[:limit]

    def cancel_last(self) -> dict:
        """Cancela el recordatorio/timer más próximo que aún no se haya disparado."""
        with self._lock:
            now_ts = time.time()
            pending = [r for r in self._reminders if not r.get("fired") and r["when_ts"] > now_ts]
            if not pending:
                return {"ok": False, "message": "No tienes recordatorios ni temporizadores pendientes."}
            pending.sort(key=lambda r: r["when_ts"])
            target = pending[0]
            self._reminders = [r for r in self._reminders if r["id"] != target["id"]]
            self._save()
        
        if target["text"].lower() in ("timer", "temporizador"):
            return {"ok": True, "message": "Temporizador cancelado."}
        elif target["text"].lower() == "alarma":
            return {"ok": True, "message": "Alarma cancelada."}
        return {"ok": True, "message": f"Recordatorio cancelado: '{target['text']}'."}

    def stop(self):
        self._running = False

    # ── Hilo verificador ──────────────────────────────────────────────────────

    def _checker_loop(self):
        while self._running:
            try:
                self._check_due()
            except Exception as e:
                logger.error(f"[Reminders] Error en checker_loop: {e}")
            time.sleep(CHECK_INTERVAL)

    def _check_due(self):
        now_ts = time.time()
        fired_any = False

        with self._lock:
            for r in self._reminders:
                if r.get("fired"):
                    continue
                diff = now_ts - r["when_ts"]
                # Dispara si ya es hora (diff >= 0) y no es excesivamente viejo (menos de 5 minutos)
                if diff >= 0 and diff < 300:
                    r["fired"]   = True
                    r["fire_ts"] = now_ts
                    fired_any    = True
                    self._fire_reminder(r)

            if fired_any:
                self._save()

    def _fire_reminder(self, reminder: dict):
        text = reminder['text']
        # Texto de voz premium
        if text.lower() in ("timer", "temporizador"):
            msg = "El temporizador ha terminado."
        elif text.lower() == "alarma":
            msg = "La alarma está sonando."
        else:
            msg = f"Recordatorio: {text}"
            
        logger.info(f"[Reminders] 🔔 Disparando: '{text}'")

        # Intentar TTS via callback (espeak-ng en la Pi)
        if self._tts_callback:
            try:
                self._tts_callback(msg)
                return
            except Exception as e:
                logger.warning(f"[Reminders] TTS callback falló: {e}")

        # Fallback: espeak-ng directo si estamos en Linux
        try:
            subprocess.Popen(
                ["espeak-ng", "-v", "es", msg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            pass

        # Fallback: publicar vía MQTT para que el cliente lo maneje
        if self._mqtt:
            try:
                import json as _json
                self._mqtt.client.publish(
                    "nika/reminder/fire",
                    _json.dumps({"text": text, "ts": reminder["when_ts"]}),
                    qos=1,
                )
            except Exception as e:
                logger.warning(f"[Reminders] MQTT publish falló: {e}")


# ── Instancia global (se crea cuando orchestrator la importa) ─────────────────
_reminder_service: Optional[ReminderService] = None


def get_reminder_service(tts_callback=None, mqtt_manager=None) -> ReminderService:
    """Factory function que retorna la instancia singleton del servicio."""
    global _reminder_service
    if _reminder_service is None:
        _reminder_service = ReminderService(tts_callback=tts_callback, mqtt_manager=mqtt_manager)
    return _reminder_service
