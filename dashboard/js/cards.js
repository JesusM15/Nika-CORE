/**
 * dashboard/js/cards.js — Animación y Comportamiento de Cards
 * ============================================================
 * Gestiona la expansión/contracción de cards al hacer clic.
 *
 * Lógica de expansión:
 *   1. El usuario hace clic (o Enter/Space) en una card.
 *   2. Si la card no está expandida:
 *      a. Cualquier otra card expandida se contrae.
 *      b. La card clickeada se expande con clase CSS .card--expanded.
 *      c. El cuerpo expandido (.card__expanded-body) se hace visible.
 *      d. La card toma grid-column: 1/-1 (todo el ancho) via CSS.
 *   3. Si la card ya está expandida, se contrae.
 *
 * Dependencias: app.js (NikaState, logConsole) debe cargarse primero.
 */

'use strict';

/* ══════════════════════════════════════════════════
   ESTADO LOCAL DE CARDS
   ══════════════════════════════════════════════════ */

/** ID de la card actualmente expandida, o null. */
let expandedCardId = null;

/** Mapa de card-id → callback a ejecutar al expandir (para lazy-load). */
const onExpandCallbacks = {};


/* ══════════════════════════════════════════════════
   API PÚBLICA
   ══════════════════════════════════════════════════ */

/**
 * Registra un callback que se ejecuta cuando una card específica se expande.
 * Usado por los módulos (modes.js, devices.js, etc.) para cargar datos al abrir.
 *
 * @param {string}   cardId - ID del elemento section.card (ej. 'card-modes').
 * @param {Function} fn     - Callback async o sync a ejecutar al expandir.
 */
function onCardExpand(cardId, fn) {
  if (!onExpandCallbacks[cardId]) onExpandCallbacks[cardId] = [];
  onExpandCallbacks[cardId].push(fn);
}

/**
 * Expande programáticamente una card (sin click del usuario).
 * @param {string} cardId
 */
function expandCard(cardId) {
  const card = document.getElementById(cardId);
  if (card && !card.classList.contains('card--expanded')) {
    _expandCard(card);
  }
}

/**
 * Contrae programáticamente la card actualmente expandida.
 */
function collapseCurrentCard() {
  if (expandedCardId) {
    const card = document.getElementById(expandedCardId);
    if (card) _collapseCard(card);
  }
}


/* ══════════════════════════════════════════════════
   LÓGICA INTERNA
   ══════════════════════════════════════════════════ */

function _expandCard(card) {
  const cardId       = card.id;
  const bodyId       = card.querySelector('.card__expanded-body')?.id;
  const expandedBody = bodyId ? document.getElementById(bodyId) : null;

  // Contraer cualquier card actualmente expandida
  if (expandedCardId && expandedCardId !== cardId) {
    const prevCard = document.getElementById(expandedCardId);
    if (prevCard) _collapseCard(prevCard);
  }

  // Expandir la nueva card
  card.classList.add('card--expanded');
  card.setAttribute('aria-expanded', 'true');
  card.setAttribute('aria-label', card.getAttribute('aria-label')?.replace('Panel', 'Panel (expandido)') || '');

  if (expandedBody) {
    expandedBody.hidden = false;
  }

  expandedCardId = cardId;

  // Ejecutar callbacks registrados para esta card
  const callbacks = onExpandCallbacks[cardId] || [];
  callbacks.forEach(fn => {
    try { fn(); }
    catch (e) { console.error(`[Cards] Error en onExpand callback de ${cardId}:`, e); }
  });

  // Scroll suave hacia la card
  setTimeout(() => {
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, 50);

  logConsole('info', `Card expandida: ${cardId.replace('card-', '')}`);
}

function _collapseCard(card) {
  const bodyId       = card.querySelector('.card__expanded-body')?.id;
  const expandedBody = bodyId ? document.getElementById(bodyId) : null;

  card.classList.remove('card--expanded');
  card.setAttribute('aria-expanded', 'false');

  if (expandedBody) {
    // Pequeño retraso para que la animación CSS de salida se vea
    // antes de ocultar con hidden
    setTimeout(() => {
      expandedBody.hidden = true;
    }, 100);
  }

  if (expandedCardId === card.id) {
    expandedCardId = null;
  }
}


/* ══════════════════════════════════════════════════
   INICIALIZACIÓN: LISTENERS EN CADA CARD
   ══════════════════════════════════════════════════ */

function initCards() {
  const cards = document.querySelectorAll('.card');

  cards.forEach(card => {
    // Clic en la cabecera de la card (no en el cuerpo expandido)
    card.addEventListener('click', (event) => {
      // No propagar si el clic fue en un botón o input dentro de la card
      const isInteractive = event.target.closest(
        'button, input, select, textarea, a, label, .mode-card, .device-card'
      );

      if (isInteractive) return;

      // Verificar que el clic fue en la cabecera (no en el cuerpo expandido)
      const expandedBody = card.querySelector('.card__expanded-body');
      const clickedInBody = expandedBody && expandedBody.contains(event.target);

      if (clickedInBody && !expandedBody.hidden) return;

      // Toggle
      if (card.classList.contains('card--expanded')) {
        _collapseCard(card);
      } else {
        _expandCard(card);
      }
    });

    // Soporte teclado: Enter y Space para accesibilidad
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (card.classList.contains('card--expanded')) {
          _collapseCard(card);
        } else {
          _expandCard(card);
        }
      }
      // Escape para contraer
      if (event.key === 'Escape' && card.classList.contains('card--expanded')) {
        _collapseCard(card);
        card.focus();
      }
    });
  });

  // Escape global para contraer cualquier card abierta
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && expandedCardId) {
      const card = document.getElementById(expandedCardId);
      if (card) {
        _collapseCard(card);
        card.focus();
      }
    }
  });
}

/* ── Inicialización ─────────────────────────────── */
document.addEventListener('DOMContentLoaded', initCards);
