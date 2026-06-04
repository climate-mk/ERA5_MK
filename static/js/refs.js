/**
 * refs.js — Citation reference system for podnebnik.org
 *
 * Content editors tag locale strings with [ref:key] tokens.
 * This module replaces them with superscript numbers and shows
 * a floating tooltip with the full citation on hover/tap/focus.
 *
 * Usage:
 *   await window.Refs.load();
 *   window.Refs.reset();
 *   window.Refs.initTooltip();
 *   // then call t() or Refs.resolve() — tokens are expanded automatically
 */

window.Refs = (() => {
  let _db        = {};    // loaded citation data  { key → {authors,year,title,…} }
  let _counter   = 0;     // sequential number for this page render
  let _index     = {};    // { key → number } for deduplication across the page
  let _tooltip   = null;  // single floating tooltip DOM element
  let _hideTimer = null;  // delayed-hide timer so mouse can travel to tooltip

  // ── Load references.json once ─────────────────────────────────────────────

  async function load() {
    if (Object.keys(_db).length > 0) return;
    try {
      const r = await fetch('/locales/references.json');
      if (!r.ok) throw new Error(r.status);
      const raw = await r.json();
      delete raw._comment;
      _db = raw;
    } catch (e) {
      console.warn('refs: could not load references.json', e);
    }
  }

  // ── Reset counter (call before each full page render) ────────────────────

  function reset() {
    _counter = 0;
    _index   = {};
  }

  // ── Replace [ref:key] tokens in a string with <sup class="ref-mark"> ─────

  function resolve(text) {
    if (!text || typeof text !== 'string') return text;
    return text.replace(/\[ref:([^\]]+)\]/g, (match, key) => {
      if (!_db[key]) return '';
      if (_index[key] === undefined) {
        _counter++;
        _index[key] = _counter;
      }
      const n = _index[key];
      return `<sup class="ref-mark" data-ref="${key}" tabindex="0" aria-label="Reference ${n}">${n}</sup>`;
    });
  }

  // ── Floating tooltip ──────────────────────────────────────────────────────

  function initTooltip() {
    if (_tooltip) return;
    _tooltip = document.createElement('div');
    _tooltip.id = 'ref-tooltip';
    _tooltip.setAttribute('role', 'tooltip');
    _tooltip.style.cssText = [
      'position:fixed',
      'z-index:9999',
      'max-width:320px',
      'background:#1a1a1a',
      'color:#f0f0f0',
      'border-radius:6px',
      'padding:10px 14px',
      'font-size:12px',
      'line-height:1.5',
      'box-shadow:0 4px 20px rgba(0,0,0,0.35)',
      'pointer-events:auto',    // always clickable — link must be reachable
      'opacity:0',
      'transition:opacity 0.15s ease',
      'display:none',
    ].join(';');
    document.body.appendChild(_tooltip);

    // Keep tooltip alive while mouse is over it (so the link is clickable)
    _tooltip.addEventListener('mouseenter', () => {
      clearTimeout(_hideTimer);
    });
    _tooltip.addEventListener('mouseleave', () => {
      _scheduleHide();
    });

    // Delegated events on .ref-mark elements
    document.addEventListener('mouseenter', _onEnter, true);
    document.addEventListener('mouseleave', _onLeave, true);
    document.addEventListener('touchstart', _onTouch, { passive: false });
    document.addEventListener('focusin',   _onEnter, true);
    document.addEventListener('focusout',  _onLeave, true);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') _hideNow(); });
  }

  function _buildContent(key) {
    const ref = _db[key];
    if (!ref) return '';
    const parts = [];
    if (ref.authors) parts.push(`<strong>${ref.authors}</strong>`);
    if (ref.year)    parts.push(`(${ref.year})`);
    if (ref.title)   parts.push(`<em>${ref.title}</em>`);
    if (ref.source)  parts.push(ref.source);
    let html = parts.join(' · ');
    if (ref.url) {
      const domain = ref.url.replace(/^https?:\/\//, '').split('/')[0];
      // Use onclick for iOS: open link programmatically so tap registers
      html += `<br><a href="${ref.url}" target="_blank" rel="noopener"
        style="color:#7eb8f7;font-size:11px;display:inline-block;padding:4px 0"
        onclick="window.open(this.href,'_blank');event.stopPropagation();return false"
        >→ ${domain}</a>`;
    }
    return html;
  }

  function _show(el) {
    clearTimeout(_hideTimer);
    const key     = el.dataset.ref;
    const content = _buildContent(key);
    if (!content) return;
    _tooltip.innerHTML     = content;
    _tooltip.style.display = 'block';
    requestAnimationFrame(() => {
      _tooltip.style.opacity = '1';
      _position(el);
    });
  }

  // Delayed hide — gives mouse time to travel from .ref-mark into tooltip
  function _scheduleHide(delay = 200) {
    clearTimeout(_hideTimer);
    _hideTimer = setTimeout(_hideNow, delay);
  }

  function _hideNow() {
    clearTimeout(_hideTimer);
    if (!_tooltip) return;
    _tooltip.style.opacity = '0';
    setTimeout(() => {
      if (_tooltip && _tooltip.style.opacity === '0') _tooltip.style.display = 'none';
    }, 160);
  }

  function _position(el) {
    const rect = el.getBoundingClientRect();
    const tw   = _tooltip.offsetWidth;
    const th   = _tooltip.offsetHeight;
    let left   = rect.left + rect.width / 2 - tw / 2;
    let top    = rect.top  - th - 8;
    left = Math.max(8, Math.min(left, window.innerWidth  - tw - 8));
    if (top < 8) top = rect.bottom + 8;
    _tooltip.style.left = left + 'px';
    _tooltip.style.top  = top  + 'px';
  }

  function _onEnter(e) {
    if (e.target.classList?.contains('ref-mark')) _show(e.target);
  }
  function _onLeave(e) {
    // Schedule hide with delay so mouse can travel to tooltip
    if (e.target.classList?.contains('ref-mark')) _scheduleHide(200);
  }

  function _onTouch(e) {
    const el = e.target;

    // ── Touch on a link INSIDE the tooltip → let it through ─────────────────
    if (_tooltip?.contains(el)) {
      // Don't intercept — let the browser handle the link tap naturally
      return;
    }

    // ── Touch on a .ref-mark → show/hide tooltip ─────────────────────────────
    if (el.classList?.contains('ref-mark')) {
      const alreadyShowing =
        _tooltip?.style.display === 'block' &&
        _tooltip?.style.opacity  === '1'    &&
        _tooltip.innerHTML.includes(`data-ref="${el.dataset.ref}"`);
      if (alreadyShowing) {
        _hideNow();
      } else {
        _show(el);
      }
      e.preventDefault(); // prevent ghost click on the mark itself
      return;
    }

    // ── Touch anywhere else → hide ────────────────────────────────────────────
    _hideNow();
  }

  return { load, reset, resolve, initTooltip };
})();
