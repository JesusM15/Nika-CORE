/**
 * dashboard/js/modes.js — CRUD de Modos
 * ======================================
 * Gestiona todas las operaciones sobre Modos:
 *   · Listar modos (GET /api/modes)
 *   · Renderizar grid de mode-cards con apps y acciones
 *   · Crear modo (modal con formulario)
 *   · Editar modo (pre-llenado del modal)
 *   · Eliminar modo (confirmación inline)
 *   · Activar modo (POST /api/modes/{id}/activate)
 *   · Búsqueda en tiempo real con filtro de nombre
 *   · Actualizar chips preview en la cabecera de la card
 *
 * Dependencias: app.js (NikaState, showToast, logConsole, escapeHTML)
 *               cards.js (onCardExpand)
 */

'use strict';

/* ══════════════════════════════════════════════════
   ESTADO LOCAL
   ══════════════════════════════════════════════════ */

/** Modos cargados desde la API */
let _modes = [];

/** ID del modo en edición (null = creando nuevo) */
let _editingModeId = null;

/** Número de apps en el modal */
let _appRowCount = 0;


/* ══════════════════════════════════════════════════
   API: FETCH DE MODOS
   ══════════════════════════════════════════════════ */

async function loadModes() {
  try {
    const res  = await fetch('/api/modes');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _modes = await res.json();
    NikaState.modes = _modes;
    renderModesGrid(_modes);
    renderModesChipsPreview(_modes);
  } catch (e) {
    logConsole('error', `Error cargando modos: ${e.message}`);
    showToast('No se pudieron cargar los modos.', 'error');
  }
}

async function createMode(data) {
  const res = await fetch('/api/modes', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return await res.json();
}

async function updateMode(id, data) {
  const res = await fetch(`/api/modes/${id}`, {
    method:  'PUT',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return await res.json();
}

async function deleteMode(id) {
  const res = await fetch(`/api/modes/${id}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}

async function activateMode(id) {
  const res = await fetch(`/api/modes/${id}/activate`, { method: 'POST' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return await res.json();
}


/* ══════════════════════════════════════════════════
   RENDERIZADO DEL GRID DE MODOS
   ══════════════════════════════════════════════════ */

/**
 * Renderiza todas las mode-cards en el grid.
 * @param {Array} modes - Array de objetos modo desde la API.
 */
function renderModesGrid(modes) {
  const grid  = document.getElementById('modes-grid');
  const empty = document.getElementById('modes-empty');
  const count = document.getElementById('modes-count');

  if (!grid) return;

  if (count) count.textContent = modes.length;

  if (modes.length === 0) {
    grid.innerHTML = '';
    if (empty) empty.hidden = false;
    return;
  }

  if (empty) empty.hidden = true;

  grid.innerHTML = modes.map(mode => buildModeCardHTML(mode)).join('');

  // Attachar event listeners a las acciones de cada mode-card
  modes.forEach(mode => {
    document.getElementById(`launch-mode-${mode.id}`)?.addEventListener('click', () => handleLaunchMode(mode.id, mode.name));
    document.getElementById(`edit-mode-${mode.id}`)?.addEventListener('click', () => handleEditMode(mode));
    document.getElementById(`delete-mode-${mode.id}`)?.addEventListener('click', () => handleDeleteMode(mode.id, mode.name));
  });
}

/**
 * Genera el HTML de una mode-card individual.
 * @param {Object} mode - Objeto modo desde la API.
 * @returns {string} HTML string.
 */
function buildModeCardHTML(mode) {
  const appsHTML = (mode.apps || [])
    .slice(0, 5)  // Mostrar max 5 apps en el preview
    .map(app => `<span class="mode-app-tag">${escapeHTML(app.app_name)}</span>`)
    .join('');

  const moreApps = mode.apps.length > 5
    ? `<span class="mode-app-tag">+${mode.apps.length - 5}</span>`
    : '';

  const appCount = mode.apps.length === 1
    ? '1 aplicación'
    : `${mode.apps.length} aplicaciones`;

  return `
    <article
      class="mode-card"
      style="--mode-color: ${escapeHTML(mode.color)}"
      role="listitem"
      aria-label="Modo: ${escapeHTML(mode.name)}">

      <header class="mode-card__header">
        <span class="mode-card__icon" aria-hidden="true">${escapeHTML(mode.icon || '🚀')}</span>
        <div class="mode-card__info">
          <h3 class="mode-card__name">${escapeHTML(mode.name)}</h3>
          <p class="mode-card__app-count">${appCount}</p>
        </div>
      </header>

      ${mode.apps.length > 0 ? `
        <div class="mode-card__apps" aria-label="Apps del modo">
          ${appsHTML}${moreApps}
        </div>
      ` : ''}

      <footer class="mode-card__actions">
        <button
          class="mode-action-btn mode-action-btn--launch"
          id="launch-mode-${mode.id}"
          type="button"
          title="Activar modo">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <polygon points="5 3 19 12 5 21 5 3"/>
          </svg>
          Activar
        </button>
        <button
          class="mode-action-btn mode-action-btn--edit"
          id="edit-mode-${mode.id}"
          type="button"
          aria-label="Editar modo ${escapeHTML(mode.name)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
        </button>
        <button
          class="mode-action-btn mode-action-btn--delete"
          id="delete-mode-${mode.id}"
          type="button"
          aria-label="Eliminar modo ${escapeHTML(mode.name)}">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <polyline points="3 6 5 6 21 6"/>
            <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
            <path d="M10 11v6M14 11v6"/>
          </svg>
        </button>
      </footer>
    </article>
  `;
}

/**
 * Actualiza los chips del preview en la cabecera de la card de modos.
 */
function renderModesChipsPreview(modes) {
  const container = document.getElementById('modes-chips-preview');
  if (!container) return;

  if (modes.length === 0) {
    container.innerHTML = '<span class="chip chip--placeholder">Sin modos</span>';
    return;
  }

  container.innerHTML = modes
    .slice(0, 4)
    .map(m => `
      <span class="chip" style="background: ${m.color}22; color: ${m.color}; border-color: ${m.color}44;">
        ${escapeHTML(m.icon || '')} ${escapeHTML(m.name)}
      </span>
    `)
    .join('') + (modes.length > 4 ? `<span class="chip chip--placeholder">+${modes.length - 4} más</span>` : '');
}


/* ══════════════════════════════════════════════════
   HANDLERS DE ACCIONES
   ══════════════════════════════════════════════════ */

async function handleLaunchMode(id, name) {
  try {
    const result = await activateMode(id);
    showToast(`Modo "${name}" activado — ${result.launched?.length || 0} apps`, 'success');
  } catch (e) {
    showToast(`Error activando modo: ${e.message}`, 'error');
  }
}

function handleEditMode(mode) {
  _editingModeId = mode.id;
  openModeModal(mode);
}

async function handleDeleteMode(id, name) {
  // Confirmación simple: cambiar el botón a "¿Seguro?"
  const btn = document.getElementById(`delete-mode-${id}`);
  if (!btn) return;

  if (btn.dataset.confirming === 'true') {
    // Segunda pulsación: confirmar
    try {
      await deleteMode(id);
      showToast(`Modo "${name}" eliminado.`, 'info');
      await loadModes();
    } catch (e) {
      showToast(`Error eliminando modo: ${e.message}`, 'error');
    }
  } else {
    // Primera pulsación: pedir confirmación
    btn.dataset.confirming = 'true';
    btn.style.color = 'var(--red)';
    btn.title = 'Clic de nuevo para confirmar eliminación';

    // Resetear si no confirma en 3 segundos
    setTimeout(() => {
      if (btn.dataset.confirming) {
        btn.dataset.confirming = '';
        btn.style.color = '';
        btn.title = `Eliminar modo ${name}`;
      }
    }, 3000);
  }
}


/* ══════════════════════════════════════════════════
   MODAL DE CREACIÓN / EDICIÓN
   ══════════════════════════════════════════════════ */

function openModeModal(mode = null) {
  const backdrop  = document.getElementById('modal-backdrop');
  const title     = document.getElementById('modal-title');
  const nameInput = document.getElementById('mode-name');
  const iconInput = document.getElementById('mode-icon');
  const colorInput= document.getElementById('mode-color');
  const hexDisplay= document.getElementById('color-hex-display');
  const appsList  = document.getElementById('modal-apps-list');

  if (!backdrop) return;

  // Configurar el modal para crear o editar
  _editingModeId = mode ? mode.id : null;
  if (title) title.textContent = mode ? `Editar Modo: ${mode.name}` : 'Nuevo Modo';

  // Pre-llenar campos si es edición
  if (nameInput)  nameInput.value  = mode?.name  || '';
  if (iconInput)  iconInput.value  = mode?.icon  || '🚀';
  if (colorInput) colorInput.value = mode?.color || '#7c3aed';
  if (hexDisplay) hexDisplay.textContent = mode?.color || '#7c3aed';

  // Sincronizar color hex display con el input color
  colorInput?.addEventListener('input', () => {
    if (hexDisplay) hexDisplay.textContent = colorInput.value;
  });

  // Limpiar y re-poblar la lista de apps
  _appRowCount = 0;
  if (appsList) {
    appsList.innerHTML = '';
    if (mode?.apps?.length > 0) {
      mode.apps.forEach(app => addAppRow(app));
    } else {
      addAppRow();    // Una fila vacía para empezar
    }
  }

  backdrop.hidden = false;

  // Focus en el primer campo
  setTimeout(() => nameInput?.focus(), 100);
}

function closeModeModal() {
  const backdrop = document.getElementById('modal-backdrop');
  if (backdrop) backdrop.hidden = true;
  _editingModeId = null;
}

/**
 * Añade una fila de app al formulario del modal.
 * @param {Object} [app] - Si se proporciona, pre-llena los campos.
 */
function addAppRow(app = null) {
  const list = document.getElementById('modal-apps-list');
  if (!list) return;

  const rowId = `app-row-${_appRowCount++}`;
  const idx   = _appRowCount - 1;

  // Obtener lista de dispositivos disponibles para el selector
  const deviceOptions = Object.keys(NikaState.devices).length > 0
    ? Object.keys(NikaState.devices).map(d =>
        `<option value="${escapeHTML(d)}" ${app?.device_id === d ? 'selected' : ''}>${escapeHTML(d)}</option>`
      ).join('')
    : `<option value="">Sin dispositivos (escanea primero)</option>`;

  const row = document.createElement('div');
  row.className = 'app-row';
  row.id = rowId;
  row.innerHTML = `
    <input
      class="app-row__input"
      type="text"
      placeholder="Nombre (ej. Spotify)"
      value="${escapeHTML(app?.app_name || '')}"
      data-field="app_name"
      data-idx="${idx}"
      aria-label="Nombre de la aplicación">
    <span class="app-row__sep">→</span>
    <input
      class="app-row__input"
      type="text"
      placeholder="Ruta/ejecutable"
      value="${escapeHTML(app?.app_path || '')}"
      data-field="app_path"
      data-idx="${idx}"
      aria-label="Ruta del ejecutable">
    <span class="app-row__sep">en</span>
    <select
      class="app-row__input"
      data-field="device_id"
      data-idx="${idx}"
      style="max-width: 120px;"
      aria-label="Dispositivo destino">
      ${deviceOptions}
    </select>
    <button
      type="button"
      class="app-row__remove"
      title="Eliminar esta app"
      aria-label="Eliminar fila">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
        <line x1="18" y1="6" x2="6" y2="18"/>
        <line x1="6" y1="6" x2="18" y2="18"/>
      </svg>
    </button>
  `;

  row.querySelector('.app-row__remove').addEventListener('click', () => row.remove());
  list.appendChild(row);
}

/**
 * Recopila los datos del formulario del modal y los envía a la API.
 */
async function saveModeFromModal() {
  const nameInput  = document.getElementById('mode-name');
  const iconInput  = document.getElementById('mode-icon');
  const colorInput = document.getElementById('mode-color');
  const saveBtn    = document.getElementById('modal-btn-save');

  const name  = nameInput?.value.trim();
  const icon  = iconInput?.value.trim() || '🚀';
  const color = colorInput?.value || '#7c3aed';

  if (!name) {
    nameInput?.classList.add('form-input--error');
    nameInput?.focus();
    showToast('El nombre del modo es obligatorio.', 'warn');
    return;
  }

  // Recopilar apps del formulario
  const apps = [];
  document.querySelectorAll('.app-row').forEach(row => {
    const appName  = row.querySelector('[data-field="app_name"]')?.value.trim();
    const appPath  = row.querySelector('[data-field="app_path"]')?.value.trim();
    const deviceId = row.querySelector('[data-field="device_id"]')?.value.trim();

    if (appName && deviceId) {
      apps.push({
        app_name:  appName,
        app_path:  appPath || appName.toLowerCase(),
        device_id: deviceId,
      });
    }
  });

  const payload = { name, icon, color, apps };

  // Deshabilitar botón durante la petición
  if (saveBtn) saveBtn.disabled = true;

  try {
    if (_editingModeId) {
      await updateMode(_editingModeId, payload);
      showToast(`Modo "${name}" actualizado.`, 'success');
    } else {
      await createMode(payload);
      showToast(`Modo "${name}" creado.`, 'success');
    }
    closeModeModal();
    await loadModes();
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
    logConsole('error', `Error guardando modo: ${e.message}`);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}


/* ══════════════════════════════════════════════════
   BÚSQUEDA EN TIEMPO REAL
   ══════════════════════════════════════════════════ */

function initModeSearch() {
  const input = document.getElementById('modes-search');
  if (!input) return;

  let debounceTimer = null;

  input.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      const q       = input.value.toLowerCase().trim();
      const filtered = q
        ? _modes.filter(m => m.name.toLowerCase().includes(q))
        : _modes;
      renderModesGrid(filtered);
    }, 150);
  });
}


/* ══════════════════════════════════════════════════
   INICIALIZACIÓN
   ══════════════════════════════════════════════════ */

function initModes() {
  // Botón "Nuevo Modo"
  document.getElementById('btn-new-mode')?.addEventListener('click', (e) => {
    e.stopPropagation();
    _editingModeId = null;
    openModeModal(null);
  });

  // Botón "Añadir App" en el modal
  document.getElementById('btn-add-app')?.addEventListener('click', () => addAppRow());

  // Botón "Guardar" en el modal
  document.getElementById('modal-btn-save')?.addEventListener('click', saveModeFromModal);

  // Botones de cerrar modal
  document.getElementById('modal-close-btn')?.addEventListener('click', closeModeModal);
  document.getElementById('modal-btn-cancel')?.addEventListener('click', closeModeModal);

  // Clic en el backdrop para cerrar
  document.getElementById('modal-backdrop')?.addEventListener('click', (e) => {
    if (e.target.id === 'modal-backdrop') closeModeModal();
  });

  // Escape para cerrar modal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModeModal();
  });

  // Búsqueda
  initModeSearch();

  // Registrar callback para cargar modos al expandir la card
  onCardExpand('card-modes', loadModes);

  // Recargar al recibir evento WebSocket de cambio de modo
  NikaState.on('modes:refresh', loadModes);
}

document.addEventListener('DOMContentLoaded', initModes);
