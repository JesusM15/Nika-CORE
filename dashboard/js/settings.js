/**
 * dashboard/js/settings.js — Panel de Ajustes y Actividad de Voz
 * ===============================================================
 * Responsabilidades:
 *   · Cargar y mostrar configuración actual al expandir la card
 *   · Guardar cambios de configuración (PUT /api/settings)
 *   · Selector de tema visual con preview inmediato
 *   · Toggle y velocidad de TTS
 *   · Cambio del nombre de Nika
 *   · Renderizar historial de comandos de voz
 *   · Animar waveform según el estado del micrófono
 *
 * Dependencias: app.js (NikaState, showToast, logConsole, applyTheme)
 *               cards.js (onCardExpand)
 */

'use strict';

/* ══════════════════════════════════════════════════
   AJUSTES: CARGA Y GUARDADO
   ══════════════════════════════════════════════════ */

/**
 * Carga la configuración del servidor y popula el formulario de ajustes.
 */
async function loadSettings() {
  try {
    const res  = await fetch('/api/settings');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    Object.assign(NikaState.settings, data);
    populateSettingsForm(data);

  } catch (e) {
    logConsole('error', `Error cargando ajustes: ${e.message}`);
  }
}

/**
 * Rellena el formulario con los valores de configuración.
 * @param {Object} settings - Objeto clave→valor de la API.
 */
function populateSettingsForm(settings) {
  // Nombre de Nika
  const nameInput = document.getElementById('set-nika-name');
  if (nameInput) nameInput.value = settings.nika_name || 'Nika';

  // TTS toggle
  const ttsInput = document.getElementById('set-tts');
  if (ttsInput) ttsInput.checked = settings.tts_enabled !== 'false';

  // Velocidad TTS
  const rateInput   = document.getElementById('set-tts-rate');
  const rateDisplay = document.getElementById('tts-rate-display');
  const rate        = parseInt(settings.tts_rate || '145', 10);
  if (rateInput)   rateInput.value    = rate;
  if (rateDisplay) rateDisplay.textContent = rate;

  // Tema: actualizar botones del selector
  const theme = settings.theme || 'dark';
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.setAttribute('aria-pressed', btn.dataset.theme === theme ? 'true' : 'false');
  });
}

/**
 * Recopila y guarda los ajustes del formulario.
 */
async function saveSettings() {
  const nameInput = document.getElementById('set-nika-name');
  const ttsInput  = document.getElementById('set-tts');
  const rateInput = document.getElementById('set-tts-rate');
  const saveBtn   = document.getElementById('btn-save-settings');

  const payload = {
    nika_name:   (nameInput?.value.trim() || 'Nika'),
    tts_enabled: ttsInput?.checked ? 'true' : 'false',
    tts_rate:    rateInput?.value || '145',
    theme:       NikaState.settings.theme || 'dark',
  };

  if (saveBtn) saveBtn.disabled = true;

  try {
    const res = await fetch('/api/settings', {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    Object.assign(NikaState.settings, payload);

    // Actualizar el nombre en el topbar
    const nameDisplay = document.getElementById('nika-name-display');
    if (nameDisplay) nameDisplay.textContent = payload.nika_name;

    showToast('Ajustes guardados correctamente.', 'success');
    logConsole('success', `Ajustes guardados: nombre="${payload.nika_name}", tema="${payload.theme}"`);

  } catch (e) {
    showToast(`Error guardando ajustes: ${e.message}`, 'error');
    logConsole('error', `Error guardando ajustes: ${e.message}`);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

/**
 * Resetea el formulario a los valores actuales del servidor.
 */
async function resetSettings() {
  await loadSettings();
  showToast('Ajustes restablecidos.', 'info');
}


/* ══════════════════════════════════════════════════
   SELECTOR DE TEMA
   ══════════════════════════════════════════════════ */

function initThemePicker() {
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.theme;
      if (!theme) return;

      // Aplicar inmediatamente (preview en tiempo real)
      applyTheme(theme);
      NikaState.settings.theme = theme;

      // Actualizar aria-pressed
      document.querySelectorAll('.theme-btn').forEach(b => {
        b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
      });

      logConsole('info', `Tema cambiado a: ${theme}`);
    });
  });
}


/* ══════════════════════════════════════════════════
   RANGE SLIDER: TTS RATE
   ══════════════════════════════════════════════════ */

function initTTSRateSlider() {
  const slider  = document.getElementById('set-tts-rate');
  const display = document.getElementById('tts-rate-display');

  slider?.addEventListener('input', () => {
    if (display) display.textContent = slider.value;
  });
}


/* ══════════════════════════════════════════════════
   ACTIVIDAD DE VOZ: HISTORIAL
   ══════════════════════════════════════════════════ */

/**
 * Renderiza el historial de comandos de voz en la card de Voz.
 * @param {Array} history - Array de entradas de historial desde NikaState.
 */
function renderVoiceHistory(history) {
  const log = document.getElementById('voice-log');
  if (!log) return;

  if (history.length === 0) {
    log.innerHTML = '<p class="empty-state">Los comandos de voz reconocidos aparecerán aquí.</p>';
    return;
  }

  log.innerHTML = history
    .slice(0, 20)    // Mostrar los últimos 20
    .map(entry => buildVoiceEntryHTML(entry))
    .join('');
}

/**
 * Genera el HTML de una entrada de historial de voz.
 */
function buildVoiceEntryHTML(entry) {
  const { text, result, ts } = entry;
  const time    = ts ? new Date(ts).toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
  const success = result?.success !== false;
  const action  = result?.action  || '';
  const response= result?.response || '';

  return `
    <article class="voice-entry voice-entry--${success ? 'success' : 'fail'}">
      <p class="voice-entry__text">🎙️ "${escapeHTML(text)}"</p>
      ${response ? `<p class="voice-entry__response">→ ${escapeHTML(response)}</p>` : ''}
      <p class="voice-entry__meta">${time}${action ? ` · ${escapeHTML(action)}` : ''}</p>
    </article>
  `;
}

/**
 * Añade una nueva entrada al log de voz (llamado por el evento WebSocket).
 */
function addVoiceEntry(entry) {
  const log = document.getElementById('voice-log');
  if (!log) return;

  // Eliminar mensaje de "vacío" si existe
  log.querySelector('.empty-state')?.remove();

  const div = document.createElement('div');
  div.innerHTML = buildVoiceEntryHTML(entry);
  const node = div.firstElementChild;

  // Insertar al principio
  log.insertBefore(node, log.firstChild);

  // Actualizar subtítulo de la card de voz
  const sub = document.getElementById('voice-status-sub');
  if (sub) sub.textContent = `Último: "${entry.text.slice(0, 30)}${entry.text.length > 30 ? '...' : ''}"`;

  // Limitar a 20 entradas visibles
  const entries = log.querySelectorAll('.voice-entry');
  if (entries.length > 20) {
    entries[entries.length - 1].remove();
  }
}


/* ══════════════════════════════════════════════════
   INICIALIZACIÓN
   ══════════════════════════════════════════════════ */

function initSettings() {
  // Formulario de ajustes
  document.getElementById('settings-form')?.addEventListener('submit', (e) => {
    e.preventDefault();
    saveSettings();
  });

  document.getElementById('btn-save-settings')?.addEventListener('click', (e) => {
    e.preventDefault();
    saveSettings();
  });

  document.getElementById('btn-reset-settings')?.addEventListener('click', resetSettings);

  // Selector de tema
  initThemePicker();

  // Slider de velocidad TTS
  initTTSRateSlider();

  // Cargar ajustes al expandir la card de settings
  onCardExpand('card-settings', loadSettings);

  // Cargar historial de voz al expandir la card de voz
  onCardExpand('card-voice', () => {
    renderVoiceHistory(NikaState.voiceHistory);
  });

  // Reaccionar a nuevos comandos de voz
  NikaState.on('voice:command', (entry) => {
    addVoiceEntry(entry);
    NikaState.voiceHistory.unshift(entry);
    if (NikaState.voiceHistory.length > 50) NikaState.voiceHistory.pop();
  });

  // Reaccionar a cambio de tema desde otro cliente WebSocket
  NikaState.on('settings:theme', (theme) => {
    document.querySelectorAll('.theme-btn').forEach(btn => {
      btn.setAttribute('aria-pressed', btn.dataset.theme === theme ? 'true' : 'false');
    });
  });
}

document.addEventListener('DOMContentLoaded', initSettings);
