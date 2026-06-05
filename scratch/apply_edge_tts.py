"""
Script para reemplazar el método speak() de espeak-ng por edge-tts
en scripts/wake_word.py.
"""

with open('scripts/wake_word.py', 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# 1. Añadir importación de asyncio y tempfile si no están presentes
import_target = "import threading\n"
import_replacement = "import threading\nimport asyncio\nimport tempfile\n"

if "import asyncio" not in content:
    content = content.replace(import_target, import_replacement)
    print("Importaciones añadidas")
else:
    print("Importaciones ya presentes, saltando...")

# 2. Añadir constante de voz edge-tts tras TTS_ALSA_DEVICE
voice_constant_target = 'TTS_ALSA_DEVICE  = os.getenv("TTS_ALSA_DEVICE", "plughw:1,0")'
voice_constant_replacement = (
    'TTS_ALSA_DEVICE  = os.getenv("TTS_ALSA_DEVICE", "plughw:1,0")\n'
    'EDGE_TTS_VOICE   = os.getenv("EDGE_TTS_VOICE", "es-MX-DaliaNeural")'
)

if "EDGE_TTS_VOICE" not in content:
    content = content.replace(voice_constant_target, voice_constant_replacement)
    print("Constante EDGE_TTS_VOICE añadida")
else:
    print("Constante EDGE_TTS_VOICE ya existe, saltando...")

# 3. Reemplazar el método speak() por la versión edge-tts
old_speak = '''    def speak(self, text: str):'''

# Encontrar el bloque completo del método speak
start_idx = content.find(old_speak)
if start_idx == -1:
    print("ERROR: No se encontró el método speak()!")
else:
    # Encontrar donde termina el método buscando el siguiente método
    # "def _open_stream" o "# ── Stream"
    end_markers = ["    def _open_stream", "    # ── Stream de audio"]
    end_idx = len(content)
    for marker in end_markers:
        idx = content.find(marker, start_idx + len(old_speak))
        if idx != -1 and idx < end_idx:
            end_idx = idx

    old_method_block = content[start_idx:end_idx]

    new_method_block = '''    def speak(self, text: str):
        """
        Sintetiza voz con edge-tts (Microsoft Neural TTS) de forma asíncrona.
        Genera audio mp3 en un archivo temporal y lo reproduce con aplay/ffplay.
        Si edge-tts no está disponible, hace fallback a espeak-ng automáticamente.
        """
        if not self._tts_enabled:
            return

        def _run_speak():
            try:
                self._is_speaking = True
                logger.info(f"[WakeWord TTS] Hablando con edge-tts: '{text}'")

                # Crear archivo temporal para el audio mp3 generado por edge-tts
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_f:
                    tmp_path = tmp_f.name

                # Generar audio con edge-tts en el loop de asyncio
                async def _generate():
                    try:
                        import edge_tts
                        communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
                        await communicate.save(tmp_path)
                        return True
                    except ImportError:
                        return False
                    except Exception as e:
                        logger.warning(f"[WakeWord TTS] Error edge-tts: {e}")
                        return False

                loop = asyncio.new_event_loop()
                success = loop.run_until_complete(_generate())
                loop.close()

                if success:
                    # Reproducir el mp3 con ffplay (sin ventana, en el dispositivo ALSA)
                    try:
                        ffplay_proc = subprocess.Popen(
                            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                             "-af", "volume=2.0",
                             tmp_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        try:
                            ffplay_proc.wait(timeout=15.0)
                        except subprocess.TimeoutExpired:
                            ffplay_proc.kill()
                            logger.warning("[WakeWord TTS] Timeout reproduciendo audio edge-tts.")
                    except FileNotFoundError:
                        # ffplay no disponible, intentar mpg123
                        try:
                            mpg_proc = subprocess.Popen(
                                ["mpg123", "-q", tmp_path],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            try:
                                mpg_proc.wait(timeout=15.0)
                            except subprocess.TimeoutExpired:
                                mpg_proc.kill()
                        except FileNotFoundError:
                            logger.warning("[WakeWord TTS] Ni ffplay ni mpg123 disponibles. Instala uno.")
                else:
                    # Fallback a espeak-ng si edge-tts no está instalado
                    logger.warning("[WakeWord TTS] edge-tts no disponible, usando espeak-ng...")
                    try:
                        espeak_proc = subprocess.Popen(
                            ["espeak-ng", "-v", "es+f2",
                             "-s", str(self._tts_rate),
                             "-a", str(self._tts_volume),
                             "--stdout", text],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                        )
                        aplay_proc = subprocess.Popen(
                            ["aplay", "-D", TTS_ALSA_DEVICE, "-"],
                            stdin=espeak_proc.stdout,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        if espeak_proc.stdout:
                            espeak_proc.stdout.close()
                        try:
                            aplay_proc.wait(timeout=12.0)
                        except subprocess.TimeoutExpired:
                            aplay_proc.kill()
                        try:
                            espeak_proc.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            espeak_proc.kill()
                    except FileNotFoundError:
                        logger.warning("[WakeWord TTS] espeak-ng tampoco disponible.")

            except Exception as e:
                logger.warning(f"[WakeWord TTS] Error inesperado en hilo TTS: {e}")
            finally:
                # Limpiar archivo temporal
                try:
                    import os as _os
                    if 'tmp_path' in dir() and _os.path.exists(tmp_path):
                        _os.unlink(tmp_path)
                except Exception:
                    pass
                self._is_speaking = False

        threading.Thread(target=_run_speak, daemon=True).start()

    '''

    content = content[:start_idx] + new_method_block + content[end_idx:]
    print("Método speak() reemplazado correctamente")

with open('scripts/wake_word.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("✓ wake_word.py actualizado.")
