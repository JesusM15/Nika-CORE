# Guía de Migración: Windows (Desarrollo) → Raspberry Pi (Producción)

Esta guía cubre todos los pasos necesarios para migrar Nika OS de tu PC Windows
a una Raspberry Pi con Raspbian/Raspberry Pi OS.

---

## 📋 Prerrequisitos

- Raspberry Pi 3B+ o superior (RPi 4 recomendada)
- Raspberry Pi OS Lite o Desktop (Bookworm/Bullseye)
- Micrófono USB conectado
- RPi y laptops en la misma red LAN
- IP estática configurada en la RPi (recomendado)

---

## 1. Preparar la Raspberry Pi

### 1.1 Actualizar el sistema

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 Instalar dependencias del sistema

```bash
# Audio (requerido por pyaudio)
sudo apt install -y portaudio19-dev python3-dev libasound2-dev

# TTS: espeak-ng
sudo apt install -y espeak-ng libespeak-ng-dev

# Python
sudo apt install -y python3-pip python3-venv python3-wheel

# Utilidades
sudo apt install -y git wget unzip
```

### 1.3 Instalar y configurar Mosquitto MQTT Broker

```bash
# Instalar Mosquitto
sudo apt install -y mosquitto mosquitto-clients

# Habilitar el servicio para que inicie con el sistema
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Verificar que está corriendo
sudo systemctl status mosquitto
```

**Configuración de Mosquitto** — editar `/etc/mosquitto/mosquitto.conf`:

```bash
sudo nano /etc/mosquitto/mosquitto.conf
```

Añadir estas líneas al final:

```
# Escuchar en todas las interfaces en puerto 1883
listener 1883
# Permitir conexiones sin autenticación (red local)
allow_anonymous true
# Logging básico
log_type all
```

```bash
# Reiniciar con la nueva configuración
sudo systemctl restart mosquitto

# Probar: suscribirse a todos los topics de Nika
mosquitto_sub -t "nika/#" -v
```

> **Seguridad**: Para una red doméstica esto es suficiente. Para entornos más
> seguros, añade autenticación con `password_file` en mosquitto.conf.

---

## 2. Transferir el código a la Raspberry Pi

### Opción A: Git (recomendado)

```bash
# En la RPi
cd /home/pi
git clone <tu-repo> nikaOS
cd nikaOS
```

### Opción B: SCP desde Windows

```powershell
# En Windows, desde la carpeta del proyecto
scp -r . pi@192.168.1.XXX:/home/pi/nikaOS/
```

### Opción C: USB / tarjeta SD

Copia la carpeta `nikaOS/` a la RPi manualmente.

---

## 3. Configurar el entorno Python

```bash
cd /home/pi/nikaOS

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias Python
pip install -r requirements.txt
```

> **Nota**: `pyaudio` en la RPi requiere que `portaudio19-dev` esté instalado
> (ya instalado en el paso 1.2). Si falla, ejecuta:
> ```bash
> sudo apt install -y python3-pyaudio
> pip install pyaudio
> ```

---

## 4. Colocar el modelo Vosk

El modelo `vosk-model-es-0.42` es ~1.4GB. Dos opciones:

### Opción A: Transferir desde Windows

```powershell
# En Windows
scp -r models\vosk-model-es-0.42 pi@192.168.1.XXX:/home/pi/nikaOS/models/
```

### Opción B: Descargar directamente en la RPi

```bash
cd /home/pi/nikaOS
mkdir -p models
cd models

# Descargar (puede tardar varios minutos según conexión)
wget https://alphacephei.com/vosk/models/vosk-model-es-0.42.zip

# Descomprimir
unzip vosk-model-es-0.42.zip
rm vosk-model-es-0.42.zip

# Verificar estructura
ls vosk-model-es-0.42/
# Debe mostrar: am/ conf/ graph/ ivector/ README
```

---

## 5. Configurar el .env en la RPi

```bash
cd /home/pi/nikaOS
cp .env.example .env
nano .env
```

Valores para producción en RPi:

```env
# MQTT: ahora el broker está en la misma máquina
MQTT_BROKER=localhost
MQTT_PORT=1883
MQTT_USER=
MQTT_PASS=

# API
API_HOST=0.0.0.0
API_PORT=8000

# Vosk: ruta absoluta para mayor claridad
VOSK_MODEL_PATH=models/vosk-model-es-0.42

# Micrófono: listar con:
# python3 -c "import pyaudio; p=pyaudio.PyAudio(); [print(i, p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"
AUDIO_DEVICE_INDEX=-1

# TTS
TTS_ENABLED=true
TTS_RATE=145
TTS_VOLUME=80

NIKA_NAME=Nika
DEBUG=false
```

---

## 6. Verificar el micrófono USB

```bash
# Listar dispositivos de audio
arecord -l

# Probar grabación de 5 segundos
arecord -D hw:1,0 -f S16_LE -r 16000 -d 5 test.wav

# Reproducir para verificar
aplay test.wav
```

Si el índice del micrófono no es el por defecto, actualiza `AUDIO_DEVICE_INDEX` en `.env`.

---

## 7. Prueba Manual

```bash
cd /home/pi/nikaOS
source venv/bin/activate

# 1. Verificar MQTT
mosquitto_pub -t "nika/test" -m "hola"
mosquitto_sub -t "nika/#" -v &

# 2. Iniciar el servidor (sin wake_word primero)
python main.py

# 3. En otra terminal: probar el wake word
python scripts/wake_word.py
```

Abrir el dashboard en tu PC: `http://192.168.1.XXX:8000`

---

## 8. Configurar como Servicio Systemd

Crea servicios para que Nika arranque automáticamente con la RPi.

### 8.1 Servicio principal (main.py)

```bash
sudo nano /etc/systemd/system/nika-main.service
```

```ini
[Unit]
Description=Nika OS — Servidor Principal (FastAPI)
After=network.target mosquitto.service
Wants=mosquitto.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/nikaOS
Environment="PATH=/home/pi/nikaOS/venv/bin"
ExecStart=/home/pi/nikaOS/venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 8.2 Servicio de wake word

```bash
sudo nano /etc/systemd/system/nika-wake.service
```

```ini
[Unit]
Description=Nika OS — Detector de Keyword (Vosk)
After=nika-main.service
Wants=nika-main.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/nikaOS
Environment="PATH=/home/pi/nikaOS/venv/bin"
ExecStart=/home/pi/nikaOS/venv/bin/python scripts/wake_word.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> **Nota**: Si usas el modo wake_word integrado en main.py (subprocess), solo necesitas
> el servicio de main. El servicio de wake word es para correrlo de forma totalmente independiente.

### 8.3 Habilitar y arrancar

```bash
sudo systemctl daemon-reload
sudo systemctl enable nika-main.service
sudo systemctl start nika-main.service

# Verificar estado
sudo systemctl status nika-main.service

# Ver logs en tiempo real
journalctl -u nika-main.service -f
```

---

## 9. Configuración en el Laptop Windows

En cada laptop:

```powershell
cd nika_client
pip install -r requirements.txt
copy .env.example .env
# Editar .env:
#   MQTT_BROKER=192.168.1.XXX   ← IP de la Raspberry Pi
```

Para que el cliente arranque automáticamente con Windows:
```
Win+R → shell:startup → crear acceso directo a:
pythonw.exe "C:\ruta\nika_client\nika_client.py"
```

O crear una tarea programada en el Programador de tareas de Windows.

---

## 🔄 Diferencias Windows vs Raspberry Pi

| Aspecto | Windows (desarrollo) | Raspberry Pi (producción) |
|---------|---------------------|--------------------------|
| **MQTT Broker** | Mosquitto instalado manualmente o broker externo | Mosquitto nativo vía apt, servicio systemd |
| **PyAudio** | Wheels pre-compilados en pip | Requiere `portaudio19-dev` via apt primero |
| **espeak-ng** | Instalación manual opcional | Nativo en Raspbian via apt |
| **Autoarranque** | Tarea programada o NSSM | Servicio systemd |
| **Vosk** | Funciona igual en x64 | Funciona igual en ARM64 |
| **GPIO** | No disponible | Disponible para expansión futura |

---

## 🔍 Diagnóstico en la RPi

```bash
# Ver logs del servidor
journalctl -u nika-main -n 50 --no-pager

# Ver topics MQTT en tiempo real
mosquitto_sub -t "nika/#" -v

# Probar endpoint de estado
curl http://localhost:8000/api/status

# Ver dispositivos conectados
curl http://localhost:8000/api/devices

# Probar un comando de voz manualmente
curl -X POST http://localhost:8000/api/voice/command \
  -H "Content-Type: application/json" \
  -d '{"text": "abre spotify", "source": "test"}'
```
