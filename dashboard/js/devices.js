/**
 * dashboard/js/devices.js — Gestión de Dispositivos
 * ==================================================
 * Responsabilidades:
 *   · Renderizar la lista de dispositivos descubiertos (en card expandida y preview)
 *   · Disparar escaneo MQTT (POST /api/devices/scan)
 *   · Enviar comandos individuales a dispositivos
 *   · Actualizar el contador de dispositivos online en el topbar
 *   · Reaccionar a eventos WebSocket de cambio de estado
 *
 * Dependencias: app.js (NikaState, showToast, logConsole, escapeHTML)
 *               cards.js (onCardExpand)
 */

'use strict';

/* ══════════════════════════════════════════════════
   API
   ══════════════════════════════════════════════════ */

async function fetchDevices() {
  const res = await fetch('/api/devices');
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

async function scanDevices() {
  const res = await fetch('/api/devices/scan', { method: 'POST' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

async function sendDeviceCommand(deviceId, command) {
  const res = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/command`, {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(command),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}


/* ══════════════════════════════════════════════════
   RENDERIZADO: LISTA DE DISPOSITIVOS (expandida)
   ══════════════════════════════════════════════════ */

/**
 * Renderiza la lista de dispositivos en el panel expandido.
 * @param {Object} devices - Diccionario hostname→info
 */
function renderDevicesList(devices) {
  const list  = document.getElementById('devices-list');
  const empty = document.getElementById('devices-empty');

  if (!list) return;

  const entries = Object.entries(devices);

  // Contador en el subtitle de la card
  const onlineCount = entries.filter(([, d]) => d.status === 'online').length;
  const countEl = document.getElementById('devices-online-count');
  if (countEl) countEl.textContent = onlineCount;

  if (entries.length === 0) {
    list.innerHTML = '';
    if (empty) empty.hidden = false;
    return;
  }

  if (empty) empty.hidden = true;

  // Ordenar: online primero, luego offline
  entries.sort(([, a], [, b]) => {
    if (a.status === 'online' && b.status !== 'online') return -1;
    if (b.status === 'online' && a.status !== 'online') return  1;
    return 0;
  });

  list.innerHTML = entries.map(([deviceId, info]) => buildDeviceCardHTML(deviceId, info)).join('');

  // Attachar listeners a las acciones
  entries.forEach(([deviceId, info]) => {
    document.getElementById(`shutdown-${deviceId}`)?.addEventListener('click', () => {
      handleShutdownDevice(deviceId);
    });
  });
}

/**
 * Genera el HTML de una device-card individual.
 * @param {string} deviceId - Hostname del dispositivo.
 * @param {Object} info     - Info del dispositivo (status, ip, apps, platform).
 * @returns {string}
 */
function buildDeviceCardHTML(deviceId, info) {
  const isOnline   = info.status === 'online';
  const appsCount  = (info.apps || []).length;
  const lastSeen   = info.last_seen
    ? new Date(info.last_seen).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit' })
    : '—';

  const platformIcon = {
    windows: '🪟',
    linux:   '🐧',
    darwin:  '🍎',
  }[info.platform] || '💻';

  return `
    <article
      class="device-card device-card--${isOnline ? 'online' : 'offline'}"
      role="listitem"
      aria-label="${escapeHTML(deviceId)}: ${isOnline ? 'en línea' : 'desconectado'}">

      <!-- Indicador de estado -->
      <span
        class="device-card__status device-card__status--${isOnline ? 'online' : 'offline'}"
        title="${isOnline ? 'En línea' : 'Desconectado'}"
        aria-hidden="true"></span>

      <!-- Info principal -->
      <div class="device-card__info">
        <p class="device-card__hostname">
          ${platformIcon} ${escapeHTML(deviceId)}
        </p>
        <p class="device-card__meta">
          <span>${escapeHTML(info.ip || 'IP desconocida')}</span>
          <span>Últ. vez: ${lastSeen}</span>
          ${info.platform ? `<span>${escapeHTML(info.platform)}</span>` : ''}
        </p>
      </div>

      <!-- Contador de apps -->
      <span class="device-card__apps-count" title="${appsCount} aplicaciones disponibles">
        ${appsCount} apps
      </span>

      <!-- Acciones -->
      ${isOnline ? `
        <div class="device-card__actions">
          <button
            class="device-action-btn device-action-btn--shutdown"
            id="shutdown-${escapeHTML(deviceId)}"
            type="button"
            title="Apagar ${escapeHTML(deviceId)}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>
              <line x1="12" y1="2" x2="12" y2="12"/>
            </svg>
          </button>
        </div>
      ` : ''}

    </article>
  `;
}

/**
 * Renderiza el preview compacto de dispositivos en la cabecera de la card.
 */
function renderDevicesMiniPreview(devices) {
  const list = document.getElementById('device-mini-list');
  if (!list) return;

  const entries = Object.entries(devices);

  if (entries.length === 0) {
    list.innerHTML = '<span class="mini-device-placeholder">Sin dispositivos — escanea la red</span>';
    return;
  }

  list.innerHTML = entries
    .slice(0, 3)    // Mostrar máximo 3 en el preview
    .map(([deviceId, info]) => `
      <div class="device-mini-row">
        <span
          class="device-mini-dot device-mini-dot--${info.status === 'online' ? 'online' : 'offline'}"
          aria-hidden="true"></span>
        <span class="device-mini-name">${escapeHTML(deviceId)}</span>
        <span class="device-mini-status device-mini-status--${info.status === 'online' ? 'online' : 'offline'}">
          ${info.status === 'online' ? 'ON' : 'OFF'}
        </span>
      </div>
    `)
    .join('') + (entries.length > 3
      ? `<div class="device-mini-row"><span class="mini-device-placeholder">+${entries.length - 3} más</span></div>`
      : '');
}


/* ══════════════════════════════════════════════════
   HANDLERS
   ══════════════════════════════════════════════════ */

async function handleScan() {
  const btn   = document.getElementById('btn-scan');
  const label = document.getElementById('scan-label');

  if (btn) {
    btn.disabled = true;
    btn.classList.add('btn--loading');
  }
  if (label) label.textContent = 'Escaneando...';

  logConsole('mqtt', 'Iniciando escaneo de dispositivos MQTT...');

  try {
    const result = await scanDevices();
    const online = result.online_count || 0;

    NikaState.devices = result.devices || {};
    renderDevicesList(NikaState.devices);
    renderDevicesMiniPreview(NikaState.devices);

    if (label) label.textContent = `Encontrados: ${online} en línea`;
    showToast(`Escaneo completado: ${online} dispositivos en línea`, 'success');
    logConsole('success', `Escaneo completado: ${online} dispositivos online`);

  } catch (e) {
    showToast(`Error en escaneo: ${e.message}`, 'error');
    logConsole('error', `Error en escaneo: ${e.message}`);
    if (label) label.textContent = 'Error en escaneo';
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.classList.remove('btn--loading');
    }
    // Limpiar label después de 5s
    setTimeout(() => { if (label) label.textContent = ''; }, 5000);
  }
}

async function handleShutdownDevice(deviceId) {
  const btn = document.getElementById(`shutdown-${deviceId}`);

  if (btn && btn.dataset.confirming !== 'true') {
    btn.dataset.confirming = 'true';
    btn.title = '¿Seguro? Clic de nuevo para confirmar';
    btn.style.color = 'var(--red)';
    setTimeout(() => {
      if (btn.dataset.confirming) {
        btn.dataset.confirming = '';
        btn.style.color = '';
        btn.title = `Apagar ${deviceId}`;
      }
    }, 3000);
    return;
  }

  try {
    await sendDeviceCommand(deviceId, { action: 'shutdown' });
    showToast(`Apagando ${deviceId}...`, 'warn');
    logConsole('warn', `Comando de apagado enviado a: ${deviceId}`);
  } catch (e) {
    showToast(`Error apagando ${deviceId}: ${e.message}`, 'error');
  }
}


/* ══════════════════════════════════════════════════
   CARGA INICIAL
   ══════════════════════════════════════════════════ */

async function loadDevices() {
  try {
    const result    = await fetchDevices();
    NikaState.devices = result.devices || {};
    renderDevicesList(NikaState.devices);
    renderDevicesMiniPreview(NikaState.devices);
  } catch (e) {
    logConsole('error', `Error cargando dispositivos: ${e.message}`);
  }
}


/* ══════════════════════════════════════════════════
   INICIALIZACIÓN
   ══════════════════════════════════════════════════ */

function initDevices() {
  // Botón de escaneo
  document.getElementById('btn-scan')?.addEventListener('click', (e) => {
    e.stopPropagation();
    handleScan();
  });

  // Actualizar UI cuando llegan eventos de dispositivos via WebSocket
  NikaState.on('devices:update', (devices) => {
    renderDevicesList(devices);
    renderDevicesMiniPreview(devices);
  });

  // Cargar al expandir la card de dispositivos
  onCardExpand('card-devices', loadDevices);
}

document.addEventListener('DOMContentLoaded', initDevices);
