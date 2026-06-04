"""
scripts/wake_word.py — Detector de Keyword y Captura de Comandos de Voz
========================================================================
Proceso independiente que corre en background mientras Nika está activa.

Flujo principal (máquina de estados):
  ┌─────────┐   keyword detected   ┌────────────────┐
  │  IDLE   │ ──────────────────► │ RECORDING_CMD  │
  │ (Vosk   │                     │ (graba N secs) │
  │ stream) │ ◄────────────────── │                │
  └─────────┘   grabación fin     └───────┬────────┘
                                          │
                                          ▼
                               transcripción → POST /api/voice/command
                               TTS responde con espeak-ng

Detección de keyword:
  - Se usan TANTO resultados parciales (baja latencia) COMO finales (mayor precisión)
  - Keywords: {"nika", "nica", "nyka", "nicas", "nikas", "oye nika", ...}
  - Un keyword detectado en resultado parcial evita esperar el fin de la frase

Compatibilidad:
  - Windows: funciona con micrófono USB/integrado y espeak-ng (si instalado)
  - Raspberry Pi: funciona con micrófono USB y espeak-ng vía apt
"""

import os
import sys
import json
import time
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

# ── Path setup: permite importar desde la raíz del proyecto ──────────────────
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv
load_dotenv(dotenv_path=ROOT_DIR / ".env")

import requests
import pyaudio
from vosk import Model, KaldiRecognizer

# ── Configuración de logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("nika.wake_word")

# ── Constantes desde variables de entorno ────────────────────────────────────
VOSK_MODEL_PATH  = os.getenv("VOSK_MODEL_PATH", "models/vosk-model-es-0.42")
AUDIO_DEVICE_IDX = int(os.getenv("AUDIO_DEVICE_INDEX", "-1"))
API_PORT         = os.getenv("API_PORT", "8000")
API_URL          = f"http://localhost:{API_PORT}"
TTS_ALSA_DEVICE  = os.getenv("TTS_ALSA_DEVICE", "plughw:1,0")

# Parámetros de audio: Vosk requiere 16kHz mono 16-bit
SAMPLE_RATE  = 16000     # Hz (Requerido por Vosk internamente)
HARDWARE_SAMPLE_RATE = int(os.getenv("HARDWARE_SAMPLE_RATE", "16000")) # Tasa física del mic

CHUNK_SIZE   = int(os.getenv("CHUNK_SIZE", "4000"))      # Frames por chunk (4000=0.25s, más responsivo)
N_CHANNELS   = int(os.getenv("HARDWARE_CHANNELS", "1"))   # Canales físicos (mono=1, estéreo=2)

# Segundos de audio a grabar DESPUÉS de detectar la keyword
COMMAND_RECORD_SECS = float(os.getenv("COMMAND_RECORD_SECS", "5.0"))

# Número de chunks pre-keyword a guardar (captura inicio de habla solapado)
# Con CHUNK_SIZE=4000 y 16kHz, cada chunk = 0.25s, entonces 3 chunks = 0.75s
PRE_KEYWORD_CHUNKS = int(os.getenv("PRE_KEYWORD_CHUNKS", "3"))

# ── Set de keywords y variaciones fonéticas ───────────────────────────────────
# Incluye variaciones comunes en STT español y errores fonéticos frecuentes.
# Vosk a veces añade puntuación al final (ej. "nika."), por eso se incluyen.
WAKE_WORDS: set[str] = {
    # Variaciones principales
    "nika", "nica", "nyka", "nikas", "nicas", "nykas",
    "mica", "kika", "mika", "lika", "dica", "ni k", "ni cap",
    # Con puntuación (artefactos de Vosk)
    "nika.", "nica.", "nyka.", "hola.",
    # Con saludo (activación más natural)
    "hola nika", "hola nica", "hola nyka", "hola kika", "hola mica",
    "oye nika",  "oye nica",  "oye nyka",
    # Otras opciones pedidas
    "hola", "ola", "buenas", "despierta", "me oyes", "escuchas",
    # Errores fonéticos comunes en español
    "nica os",   "nika os",
}

# ── Estado global del detector de voz (para broadcast WS) ────────────────────
_voice_active = False


def _notify_voice_state(state: str):
    """
    Notifica al servidor (main.py) el estado del micrófono vía HTTP.
    Esto permite al dashboard mostrar el indicador de voz correctamente.
    """
    try:
        requests.post(
            f"{API_URL}/api/voice/state",
            json={"state": state},
            timeout=1.0,
        )
    except Exception:
        pass    # No bloquear si la API no está disponible todavía


class WakeWordDetector:
    """
    Motor de detección de keyword y captura de comandos.

    Máquina de estados interna:
      IDLE        → Escucha continua buscando keyword en stream de Vosk
      RECORDING   → Grabando audio del comando post-keyword
    """

    # Estados de la máquina
    STATE_IDLE      = "idle"
    STATE_RECORDING = "recording"

    def __init__(self):
        self._state: str = self.STATE_IDLE

        # ── Validar y cargar el modelo Vosk ──────────────────────────────────
        model_path = ROOT_DIR / VOSK_MODEL_PATH
        if not model_path.exists():
            logger.critical(
                f"\n{'='*60}\n"
                f"  ✗ Modelo Vosk NO encontrado en: {model_path}\n"
                f"  Descarga desde: https://alphacephei.com/vosk/models/\n"
                f"  Modelo recomendado: vosk-model-es-0.42.zip\n"
                f"  Extrae el ZIP en: {model_path.parent}/\n"
                f"{'='*60}"
            )
            sys.exit(1)

        logger.info(f"[WakeWord] Cargando modelo Vosk: {model_path}")
        logger.info("[WakeWord] (Esto puede tardar 5-15 segundos la primera vez...)")
        self.model          = Model(str(model_path))
        self.recognizer     = self._new_recognizer()
        logger.info("[WakeWord] ✓ Modelo cargado correctamente.")

        # ── Inicializar PyAudio ───────────────────────────────────────────────
        self.audio          = pyaudio.PyAudio()
        self.stream: Optional[pyaudio.Stream] = None

        # ── Estado de grabación ───────────────────────────────────────────────
        self._running         = False
        self._command_buffer: list[bytes] = []
        self._record_start    = 0.0

        # Buffer circular de chunks pre-keyword (captura inicio de habla solapado)
        from collections import deque
        self._pre_buffer: deque = deque(maxlen=PRE_KEYWORD_CHUNKS)

        # ── TTS: espeak-ng ────────────────────────────────────────────────────
        self._tts_rate   = int(os.getenv("TTS_RATE", "145"))
        self._tts_volume = int(os.getenv("TTS_VOLUME", "200"))
        self._tts_enabled = os.getenv("TTS_ENABLED", "true").lower() == "true"

        logger.info(f"[WakeWord] TTS: {'activado' if self._tts_enabled else 'desactivado'}")
        logger.info(f"[WakeWord] Keywords activos: {sorted(WAKE_WORDS)}")

    # ── Fábrica de reconocedores ──────────────────────────────────────────────

    def _new_recognizer(self) -> KaldiRecognizer:
        """
        Crea un nuevo KaldiRecognizer con el modelo cargado.
        SetWords(True) incluye timestamps y confianza por palabra en los resultados.
        """
        rec = KaldiRecognizer(self.model, float(HARDWARE_SAMPLE_RATE))
        rec.SetWords(True)
        return rec

    # ── TTS con espeak-ng ─────────────────────────────────────────────────────

    def speak(self, text: str):
        """
        Sintetiza voz con espeak-ng de forma no bloqueante.

        Flujo: espeak-ng --stdout → pipe → aplay -D {TTS_ALSA_DEVICE}

        Al usar --stdout, espeak-ng genera el PCM en lugar de intentar
        abrir /dev/dsp directamente, y aplay lo envía al dispositivo ALSA
        correcto (ej. hw:1,0 para el adaptador USB con micrófono y bocina).

        Flags de espeak-ng:
          -v es       → voz en español
          -s {rate}   → velocidad en palabras por minuto
          -a {volume} → amplitud (volumen) 0-200
          --stdout    → escribir PCM a stdout en lugar de reproducir directo
        """
        if not self._tts_enabled:
            return

        try:
            # espeak-ng genera audio PCM a su stdout
            espeak_proc = subprocess.Popen(
                ["espeak-ng",
                 "-v", "es+f2",
                 "-s", str(self._tts_rate),
                 "-a", str(self._tts_volume),
                 "--stdout",
                 text],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            # aplay recibe el PCM por stdin y lo reproduce en el dispositivo USB
            subprocess.Popen(
                ["aplay", "-D", TTS_ALSA_DEVICE, "-"],
                stdin=espeak_proc.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Permitir que espeak_proc sepa que aplay tomó el pipe
            if espeak_proc.stdout:
                espeak_proc.stdout.close()

        except FileNotFoundError as e:
            # espeak-ng o aplay no instalados
            logger.warning(f"[WakeWord] TTS no disponible (espeak-ng/aplay): {e}")
            logger.warning("  → Instala con: sudo apt install espeak-ng alsa-utils")
        except Exception as e:
            logger.warning(f"[WakeWord] Error TTS: {e}")

    # ── Stream de audio ───────────────────────────────────────────────────────

    def _open_stream(self) -> pyaudio.Stream:
        """Abre el stream de entrada de audio con los parámetros de Vosk."""
        device_idx = AUDIO_DEVICE_IDX if AUDIO_DEVICE_IDX >= 0 else None

        try:
            stream = self.audio.open(
                format=pyaudio.paInt16,          # PCM 16-bit (requerido por Vosk)
                channels=N_CHANNELS,
                rate=HARDWARE_SAMPLE_RATE,
                input=True,
                input_device_index=device_idx,
                frames_per_buffer=CHUNK_SIZE,
            )
            logger.info(
                f"[WakeWord] ✓ Stream de audio abierto: "
                f"{HARDWARE_SAMPLE_RATE}Hz, {N_CHANNELS}ch, chunk={CHUNK_SIZE}"
            )
            return stream
        except Exception as e:
            logger.error(f"[WakeWord] ✗ Error abriendo stream de audio: {e}")
            logger.error(
                "  → Verifica que tienes un micrófono conectado.\n"
                "  → En Windows, revisa 'Configuración de sonido > Entrada'.\n"
                f"  → AUDIO_DEVICE_INDEX actual: {AUDIO_DEVICE_IDX}"
            )
            raise

    # ── Loop principal ────────────────────────────────────────────────────────

    def listen(self):
        """
        Loop principal de escucha. Corre indefinidamente hasta que
        self._running se ponga a False.

        Máquina de estados:
          STATE_IDLE      → alimenta audio a Vosk, chequea keywords
          STATE_RECORDING → acumula chunks en buffer durante COMMAND_RECORD_SECS
        """
        self._running = True
        self.stream   = self._open_stream()

        logger.info("[WakeWord] 🎙️ Escuchando... (di 'Nika' para activar)")
        self.speak("Sistema Nika iniciado y escuchando.")
        _notify_voice_state("listening")

        while self._running:
            try:
                # Leer chunk de audio
                # exception_on_overflow=False: ignora buffer overflow en lugar de crashear
                data = self.stream.read(CHUNK_SIZE, exception_on_overflow=False)

                if self._state == self.STATE_RECORDING:
                    self._step_recording(data)
                else:
                    self._step_idle(data)

            except OSError as e:
                # Error de I/O de audio (ej. dispositivo desconectado)
                logger.error(f"[WakeWord] Error de audio: {e}. Reintentando en 2s...")
                time.sleep(2.0)
                try:
                    self.stream = self._open_stream()
                except Exception:
                    time.sleep(5.0)

            except Exception as e:
                logger.error(f"[WakeWord] Error inesperado en loop: {e}", exc_info=True)
                time.sleep(0.5)

    def _step_idle(self, data: bytes):
        """
        Procesamiento en estado IDLE.
        Alimenta el chunk a Vosk y verifica si hay keyword
        tanto en resultados parciales (baja latencia) como finales.

        IMPORTANTE: Se llama AcceptWaveform ANTES de PartialResult para que
        el resultado parcial incluya el chunk recién alimentado (no el anterior).
        """
        # Guardar chunk en el buffer circular pre-keyword
        self._pre_buffer.append(data)

        # ── Alimentar chunk actual PRIMERO ────────────────────────────────
        is_final = self.recognizer.AcceptWaveform(data)

        # ── Resultado parcial (ahora incluye el chunk actual) ─────────────
        if not is_final:
            partial_json = json.loads(self.recognizer.PartialResult())
            partial_text = partial_json.get("partial", "")
            if partial_text:
                # Imprime en la misma línea para que veas qué está escuchando Vosk en tiempo real
                print(f"\r👂 Vosk escuchando: {partial_text}\033[K", end="", flush=True)
                
            if partial_text and self._contains_wake_word(partial_text):
                print() # Salto de línea
                logger.info(f"[WakeWord] Keyword en parcial: '{partial_text}'")
                self._on_keyword_detected()
                return

        # ── Resultado final (cuando el usuario deja de hablar) ────────────
        if is_final:
            final_json = json.loads(self.recognizer.Result())
            final_text = final_json.get("text", "")
            if final_text:
                logger.debug(f"[WakeWord] Resultado final: '{final_text}'")
                if self._contains_wake_word(final_text):
                    logger.info(f"[WakeWord] Keyword en final: '{final_text}'")
                    self._on_keyword_detected()

    def _step_recording(self, data: bytes):
        """
        Procesamiento en estado RECORDING.
        Acumula chunks de audio del comando hasta que se agota el tiempo.
        """
        self._command_buffer.append(data)
        elapsed = time.time() - self._record_start

        if elapsed >= COMMAND_RECORD_SECS:
            # Tiempo de grabación agotado: transcribir y enviar
            logger.info(
                f"[WakeWord] Grabación completada ({elapsed:.1f}s, "
                f"{len(self._command_buffer)} chunks)"
            )
            _notify_voice_state("processing")
            self._process_command()

            # Volver a estado IDLE
            self._state          = self.STATE_IDLE
            self._command_buffer = []
            self.recognizer      = self._new_recognizer()   # Reset Vosk
            _notify_voice_state("listening")
            logger.info("[WakeWord] Volviendo a escucha...")

    # ── Eventos de la máquina de estados ─────────────────────────────────────

    def _on_keyword_detected(self):
        """Transición IDLE → RECORDING al detectar la keyword."""
        logger.info("[WakeWord] ✨ Keyword detectada! Grabando comando...")
        self.speak("Dime.")
        _notify_voice_state("recording")

        # Resetear el reconocedor para limpiar el buffer de la keyword
        self.recognizer = self._new_recognizer()

        # Entrar en modo grabación
        self._state          = self.STATE_RECORDING
        self._command_buffer = []
        self._record_start   = time.time()

    def _process_command(self):
        """
        Transcribe el buffer de audio del comando y lo envía al orquestador.

        Usa un KaldiRecognizer temporal (no el de idle) para no contaminar el estado.
        """
        if not self._command_buffer:
            logger.warning("[WakeWord] Buffer de comando vacío.")
            self.speak("No escuché ningún comando.")
            return

        # Transcribir el buffer completo
        cmd_rec = self._new_recognizer()
        for chunk in self._command_buffer:
            cmd_rec.AcceptWaveform(chunk)

        result = json.loads(cmd_rec.FinalResult())
        text   = result.get("text", "").strip()

        if not text:
            logger.warning("[WakeWord] No se reconoció texto en el comando.")
            self.speak("No te escuché bien. Inténtalo de nuevo diciendo mi nombre primero.")
            return

        logger.info(f"[WakeWord] ✓ Comando transcrito: '{text}'")
        self._send_to_api(text)

    def _send_to_api(self, text: str):
        """
        Envía el texto transcrito al endpoint /api/voice/command del servidor.
        El orquestador procesa el comando y retorna la respuesta TTS.
        """
        try:
            response = requests.post(
                f"{API_URL}/api/voice/command",
                json={"text": text, "source": "wake_word"},
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                logger.info(f"[WakeWord] Respuesta del orquestador: {data}")

                # Leer la respuesta TTS en voz alta
                tts_text = data.get("response", "")
                if tts_text:
                    self.speak(tts_text)
            else:
                logger.warning(f"[WakeWord] API respondió {response.status_code}")
                self.speak("Hubo un error procesando tu comando.")

        except requests.ConnectionError:
            logger.warning("[WakeWord] La API de Nika no está disponible. ¿Está main.py corriendo?")
            self.speak("No pude conectar con el servidor de Nika.")
        except requests.Timeout:
            logger.warning("[WakeWord] Timeout esperando respuesta de la API.")
            self.speak("El servidor tardó demasiado en responder.")
        except Exception as e:
            logger.error(f"[WakeWord] Error enviando comando a la API: {e}")

    # ── Helper de detección ───────────────────────────────────────────────────

    @staticmethod
    def _contains_wake_word(text: str) -> bool:
        """
        Verifica si el texto contiene alguna keyword activa.
        Normaliza el texto antes de comparar para mayor robustez.

        Args:
            text: Texto transcrito por Vosk (puede ser parcial o final).

        Returns:
            True si se detectó alguna keyword.
        """
        normalized = text.lower().strip()

        # Comparación exacta primero (más eficiente)
        if normalized in WAKE_WORDS:
            return True

        # Búsqueda de substring (para casos como "oye nika cómo estás")
        for word in WAKE_WORDS:
            if word in normalized:
                return True

        return False

    # ── Limpieza ──────────────────────────────────────────────────────────────

    def stop(self):
        """Detiene el loop y libera recursos de audio."""
        logger.info("[WakeWord] Deteniendo detector...")
        self._running = False

        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass

        try:
            self.audio.terminate()
        except Exception:
            pass

        _notify_voice_state("offline")
        logger.info("[WakeWord] Detector detenido.")


# ── Punto de entrada (ejecutado como proceso independiente) ───────────────────

if __name__ == "__main__":
    detector = WakeWordDetector()
    try:
        detector.listen()
    except KeyboardInterrupt:
        logger.info("\n[WakeWord] Interrupción de usuario (Ctrl+C).")
    finally:
        detector.stop()
