/**
 * dashboard/js/app.js — Núcleo del Dashboard de Nika OS
 * =======================================================
 * Responsabilidades:
 *   · Estado global de la aplicación (NikaState)
 *   · Conexión WebSocket con auto-reconnect
 *   · Router de eventos WebSocket → handlers de módulos
 *   · Reloj y fecha en tiempo real
 *   · Sistema de notificaciones Toast
 *   · Logger de consola en tiempo real
 *   · Inicialización y carga de configuración inicial
 *
 * Dependencias de ejecución: ninguna (es el primer script)
 * Expone globalmente: NikaState, NikaWS, showToast, logConsole
 */

'use strict';

/* ══════════════════════════════════════════════════
   1. ESTADO GLOBAL REACTIVO
   ══════════════════════════════════════════════════ */

/**
 * NikaState — Store centralizado de la aplicación.
 * Los módulos leen y escriben aquí para mantener la UI sincronizada.
 */
const NikaState = {
  // Configuración del sistema
  settings: {
    nika_name:   'Nika',
    theme:       'dark',
    tts_enabled: 'true',
    tts_rate:    '145',
  },

  // Dispositivos descubiertos vía MQTT. Key: hostname, Value: info del dispositivo
  devices: {},

  // Modos configurados
  modes: [],

  // Estado de la conexión MQTT
  mqttConnected: false,

  // Estado del detector de voz
  voiceState: 'offline',    // offline | listening | recording | processing

  // Historial de comandos de voz (últimos 50)
  voiceHistory: [],

  // Observadores: permiten a módulos suscribirse a cambios de estado
  _observers: {},

  /**
   * Notifica a los observadores de un evento específico.
   * @param {string} event - Nombre del evento.
   * @param {*} data       - Datos del evento.
   */
  emit(event, data) {
    const handlers = this._observers[event] || [];
    handlers.forEach(fn => {
      try { fn(data); }
      catch (e) { console.error(`[NikaState] Error en observer '${event}':`, e); }
    });
  },

  /**
   * Registra un callback para un evento de estado.
   * @param {string}   event - Nombre del evento.
   * @param {Function} fn    - Callback fn(data) => void.
   */
  on(event, fn) {
    if (!this._observers[event]) this._observers[event] = [];
    this._observers[event].push(fn);
  },
};


/* ══════════════════════════════════════════════════
   2. CONEXIÓN WEBSOCKET CON AUTO-RECONNECT
   ══════════════════════════════════════════════════ */

/**
 * NikaWS — Gestiona la conexión WebSocket con el servidor.
 * Auto-reconnect con backoff exponencial.
 */
const NikaWS = {
  ws:              null,
  reconnectDelay:  1000,   // ms
  maxDelay:        30000,  // ms
  _pingInterval:   null,
  _reconnectTimer: null,

  /**
   * Abre la conexión WebSocket al servidor FastAPI.
   */
  connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url      = `${protocol}//${location.host}/ws`;

    logConsole('info', `Conectando WebSocket → ${url}`);

    this.ws = new WebSocket(url);

    this.ws.onopen = () => {
      logConsole('success', 'WebSocket conectado al servidor Nika.');
      this.reconnectDelay = 1000;  // Resetear backoff

      // Ping cada 20s para mantener la conexión viva
      this._pingInterval = setInterval(() => {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, 20000);
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        NikaWS._handleMessage(msg);
      } catch (e) {
        console.warn('[WS] Mensaje no-JSON recibido:', event.data);
      }
    };

    this.ws.onclose = (event) => {
      clearInterval(this._pingInterval);
      if (event.code !== 1000) {  // 1000 = cierre normal
        logConsole('warn', `WebSocket cerrado (code=${event.code}). Reconectando...`);
        this._scheduleReconnect();
      }
    };

    this.ws.onerror = () => {
      logConsole('error', 'Error de WebSocket. ¿Está el servidor corriendo?');
    };
  },

  /**
   * Programa un reintento de conexión con backoff exponencial.
   */
  _scheduleReconnect() {
    clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(() => {
      this.connect();
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxDelay);
    }, this.reconnectDelay);
  },

  /**
   * Router de mensajes WebSocket. Despacha al handler correcto.
   * @param {Object} msg - Mensaje JSON deserializado del servidor.
   */
  _handleMessage(msg) {
    const { event } = msg;

    switch (event) {
      // Estado inicial al conectarse
      case 'init':
        NikaState.devices       = msg.devices       || {};
        NikaState.mqttConnected = msg.mqtt_connected || false;
        updateMQTTStatus(NikaState.mqttConnected);
        NikaState.emit('devices:update', NikaState.devices);
        logConsole('mqtt', `MQTT ${NikaState.mqttConnected ? 'conectado' : 'desconectado'} (broker: ${msg.broker})`);
        break;

      // Heartbeat periódico con estado de dispositivos
      case 'heartbeat':
        NikaState.devices       = msg.devices || NikaState.devices;
        NikaState.mqttConnected = msg.mqtt_ok  || false;
        updateMQTTStatus(NikaState.mqttConnected);
        NikaState.emit('devices:update', NikaState.devices);
        break;

      // Nuevo dispositivo descubierto
      case 'device_discovered':
        NikaState.devices[msg.device_id] = msg.device;
        NikaState.emit('devices:update', NikaState.devices);
        showToast(`Dispositivo encontrado: ${msg.device_id}`, 'success');
        logConsole('mqtt', `Dispositivo descubierto: ${msg.device_id} (${msg.device.ip || '?'})`);
        break;

      // Cambio de estado de un dispositivo
      case 'device_status': {
        const dev = NikaState.devices[msg.device_id] || {};
        dev.status = msg.status;
        NikaState.devices[msg.device_id] = dev;
        NikaState.emit('devices:update', NikaState.devices);
        logConsole('mqtt', `${msg.device_id} → ${msg.status.toUpperCase()}`);
        break;
      }

      // MQTT conectado/desconectado
      case 'mqtt_connected':
        NikaState.mqttConnected = true;
        updateMQTTStatus(true);
        logConsole('success', `MQTT conectado al broker ${msg.broker || ''}`);
        break;

      case 'mqtt_disconnected':
        NikaState.mqttConnected = false;
        updateMQTTStatus(false);
        logConsole('warn', 'MQTT desconectado del broker.');
        break;

      // Eventos de modos
      case 'mode_created':
        NikaState.emit('modes:refresh');
        showToast(`Modo "${msg.mode?.name}" creado`, 'success');
        logConsole('info', `Modo creado: ${msg.mode?.name}`);
        break;

      case 'mode_updated':
        NikaState.emit('modes:refresh');
        break;

      case 'mode_deleted':
        NikaState.emit('modes:refresh');
        showToast(`Modo eliminado`, 'info');
        break;

      case 'mode_activated':
        showToast(`Modo "${msg.name}" activado — ${msg.launched?.length || 0} apps`, 'success');
        logConsole('success', `Modo activado: ${msg.name} (${msg.launched?.length} apps lanzadas)`);
        break;

      // Comando de voz procesado
      case 'voice_command': {
        const entry = { text: msg.text, result: msg.result, ts: Date.now() };
        NikaState.voiceHistory.unshift(entry);
        if (NikaState.voiceHistory.length > 50) NikaState.voiceHistory.pop();
        NikaState.emit('voice:command', entry);
        logConsole('voice', `🎙️ "${msg.text}" → ${msg.result?.response || ''}`);
        break;
      }

      // Estado del micrófono
      case 'voice_state':
        NikaState.voiceState = msg.state;
        updateVoiceStatus(msg.state);
        NikaState.emit('voice:state', msg.state);
        break;

      // Configuración cambiada
      case 'settings_changed':
        if (msg.nika_name) {
          NikaState.settings.nika_name = msg.nika_name;
          const el = document.getElementById('nika-name-display');
          if (el) el.textContent = msg.nika_name;
        }
        break;

      // Cambio de tema
      case 'theme_changed':
        applyTheme(msg.theme);
        NikaState.settings.theme = msg.theme;
        NikaState.emit('settings:theme', msg.theme);
        break;

      case 'scan_complete':
        NikaState.devices = msg.devices || {};
        NikaState.emit('devices:update', NikaState.devices);
        break;

      case 'pong':
        // Respuesta al ping de keepalive, ignorar
        break;

      default:
        console.debug('[WS] Evento no manejado:', event, msg);
    }
  },

  /**
   * Envía un mensaje al servidor por WebSocket.
   * @param {Object} data - Objeto a serializar como JSON.
   */
  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  },
};


/* ══════════════════════════════════════════════════
   3. UI: ACTUALIZACIÓN DE INDICADORES DE ESTADO
   ══════════════════════════════════════════════════ */

function updateMQTTStatus(connected) {
  const dot = document.getElementById('dot-mqtt');
  if (dot) dot.dataset.state = connected ? 'online' : 'offline';
}

function updateVoiceStatus(state) {
  const dot   = document.getElementById('dot-voice');
  const label = document.getElementById('voice-state-label');
  const overlay = document.getElementById('voice-overlay');
  const overlayText = document.getElementById('voice-overlay-text');
  const waveform  = document.getElementById('waveform-mini');

  const stateLabels = {
    listening:  'Voz',
    recording:  'Grabando',
    processing: 'Procesando',
    offline:    'Voz Offline',
  };

  if (dot)   dot.dataset.state = state;
  if (label) label.textContent = stateLabels[state] || 'Voz';

  // Mostrar/ocultar overlay de voz
  if (overlay) {
    if (state === 'recording' || state === 'processing') {
      overlay.hidden = false;
      if (overlayText) {
        overlayText.textContent = state === 'recording'
          ? '🔴 Grabando comando...'
          : '⚙️ Procesando...';
      }
    } else {
      overlay.hidden = true;
    }
  }

  // Animación del waveform en la card
  if (waveform) {
    waveform.classList.toggle('waveform--active', state === 'recording');
  }
}

function updateDeviceCount() {
  const online = Object.values(NikaState.devices)
    .filter(d => d.status === 'online').length;
  const el = document.getElementById('device-online-count');
  if (el) el.textContent = online;
}


/* ══════════════════════════════════════════════════
   4. TEMA
   ══════════════════════════════════════════════════ */

/**
 * Aplica un tema visual al documento.
 * @param {string} theme - 'dark' | 'cyberpunk' | 'matrix'
 */
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('nika-theme', theme);

  // Actualizar botones de selector de tema en el panel de ajustes
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', btn.dataset.theme === theme ? 'true' : 'false');
  });
}

function loadSavedTheme() {
  const saved = localStorage.getItem('nika-theme') || NikaState.settings.theme || 'dark';
  applyTheme(saved);
}


/* ══════════════════════════════════════════════════
   5. RELOJ EN TIEMPO REAL
   ══════════════════════════════════════════════════ */

function startClock() {
  const timeEl = document.getElementById('clock-time');
  const dateEl = document.getElementById('clock-date');

  const DAYS   = ['Domingo', 'Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado'];
  const MONTHS = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic'];

  function tick() {
    const now  = new Date();
    const hh   = String(now.getHours()).padStart(2, '0');
    const mm   = String(now.getMinutes()).padStart(2, '0');
    const ss   = String(now.getSeconds()).padStart(2, '0');
    const day  = DAYS[now.getDay()];
    const date = now.getDate();
    const mon  = MONTHS[now.getMonth()];

    if (timeEl) timeEl.textContent = `${hh}:${mm}:${ss}`;
    if (dateEl) dateEl.textContent = `${day}, ${date} ${mon}`;
  }

  tick();    // Llamada inmediata para evitar flash de "00:00:00"
  setInterval(tick, 1000);
}


/* ══════════════════════════════════════════════════
   6. SISTEMA DE TOASTS (NOTIFICACIONES)
   ══════════════════════════════════════════════════ */

/**
 * Muestra una notificación toast flotante.
 * @param {string} message  - Texto de la notificación.
 * @param {string} [type]   - 'success' | 'error' | 'info' | 'warn'
 * @param {number} [ms]     - Duración en ms (por defecto 4000).
 */
function showToast(message, type = 'info', ms = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const icons = { success: '✓', error: '✗', info: 'ℹ', warn: '⚠' };

  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.innerHTML = `
    <span class="toast__icon">${icons[type] || 'ℹ'}</span>
    <span class="toast__text">${escapeHTML(message)}</span>
  `;

  container.appendChild(toast);

  // Auto-dismiss
  setTimeout(() => {
    toast.classList.add('toast--exit');
    setTimeout(() => toast.remove(), 400);
  }, ms);
}


/* ══════════════════════════════════════════════════
   7. CONSOLA / LOG
   ══════════════════════════════════════════════════ */

const _consoleLines = [];
const MAX_CONSOLE_LINES = 200;

/**
 * Añade una línea al log de consola del dashboard.
 * @param {string} level   - 'info' | 'success' | 'warn' | 'error' | 'voice' | 'mqtt'
 * @param {string} message - Texto del mensaje.
 */
function logConsole(level, message) {
  const output   = document.getElementById('console-output');
  const autoscroll = document.getElementById('cb-autoscroll');
  const subtitle   = document.getElementById('console-subtitle');

  const now = new Date();
  const ts  = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;

  // Actualizar subtítulo de la card de consola
  if (subtitle) subtitle.textContent = `Último: ${ts}`;

  if (!output) return;

  // Crear línea
  const line = document.createElement('div');
  line.className = `console-line console-line--${level}`;
  line.innerHTML = `
    <span class="console-line__ts">${ts}</span>
    <span class="console-line__msg">${escapeHTML(message)}</span>
  `;

  output.appendChild(line);
  _consoleLines.push(line);

  // Limitar número de líneas
  while (_consoleLines.length > MAX_CONSOLE_LINES) {
    const old = _consoleLines.shift();
    if (old.parentNode === output) output.removeChild(old);
  }

  // Auto-scroll al fondo si está activado
  if (!autoscroll || autoscroll.checked) {
    output.scrollTop = output.scrollHeight;
  }
}


/* ══════════════════════════════════════════════════
   8. CARGA DE CONFIGURACIÓN INICIAL
   ══════════════════════════════════════════════════ */

async function loadInitialSettings() {
  try {
    const res  = await fetch('/api/settings');
    if (!res.ok) return;
    const data = await res.json();

    Object.assign(NikaState.settings, data);

    // Aplicar nombre de Nika
    const nameEl = document.getElementById('nika-name-display');
    if (nameEl) nameEl.textContent = data.nika_name || 'Nika';

    // El tema se aplica desde loadSavedTheme() antes que esto
    // pero lo sincronizamos con la DB
    if (data.theme) applyTheme(data.theme);

    logConsole('info', `Configuración cargada: nombre="${data.nika_name}", tema="${data.theme}"`);
  } catch (e) {
    logConsole('warn', 'No se pudo cargar la configuración del servidor.');
  }
}


/* ══════════════════════════════════════════════════
   9. EVENTOS DE CONSOLA
   ══════════════════════════════════════════════════ */

function initConsoleBtns() {
  // Limpiar consola
  document.getElementById('btn-clear-console')?.addEventListener('click', () => {
    const output = document.getElementById('console-output');
    if (output) output.innerHTML = '';
    _consoleLines.length = 0;
  });
}


/* ══════════════════════════════════════════════════
   10. SUSCRIPCIÓN AL ESTADO: ACTUALIZAR DEVICE COUNT
   ══════════════════════════════════════════════════ */

NikaState.on('devices:update', () => {
  updateDeviceCount();
});


/* ══════════════════════════════════════════════════
   UTILIDADES
   ══════════════════════════════════════════════════ */

/**
 * Escapa caracteres HTML para prevenir XSS al insertar texto de usuario.
 * @param {string} str
 * @returns {string}
 */
function escapeHTML(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/**
 * Formatrea fecha/hora como "HH:MM:SS".
 * @param {Date|number} [date]
 * @returns {string}
 */
function formatTime(date = new Date()) {
  const d  = new Date(date);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}


/* ══════════════════════════════════════════════════
   INICIALIZACIÓN PRINCIPAL
   ══════════════════════════════════════════════════ */

document.addEventListener('DOMContentLoaded', () => {
  // 1. Tema guardado (antes de mostrar la UI para evitar flash)
  loadSavedTheme();

  // 2. Reloj
  startClock();

  // 3. Conexión WebSocket
  NikaWS.connect();

  // 4. Cargar configuración del servidor
  loadInitialSettings();

  // 5. Botones de consola
  initConsoleBtns();

  // 6. Log inicial
  logConsole('success', 'Nika OS Dashboard iniciado.');
});
