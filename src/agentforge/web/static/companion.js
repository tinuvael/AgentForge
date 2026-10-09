/* Safe hints reload authoritative HTML. No raw execution payload enters the DOM. */
(() => {
  let source = null, identity = null, timer = null, loading = false, dirty = false, stopped = false;
  const openDetails = new Map();
  document.addEventListener('htmx:beforeSwap', () => {
    document.querySelectorAll('details[data-detail-key]').forEach(node => {
      openDetails.set(node.dataset.detailKey, node.open);
    });
  });
  const status = text => {
    const node = document.getElementById('live-status');
    if (node) node.textContent = text;
  };
  const refresh = () => {
    dirty = true;
    if (timer || loading) return;
    timer = setTimeout(async () => {
      timer = null; loading = true; dirty = false;
      try {
        await htmx.ajax('GET', `${identity}/fragment`, {target:'#companion-detail', swap:'outerHTML'});
      } catch (_) { status('Snapshot unavailable; reload to reconnect.'); }
      finally { loading = false; if (dirty) refresh(); }
    }, 200);
  };
  const connect = () => {
    document.querySelectorAll('details[data-detail-key]').forEach(node => {
      if (openDetails.has(node.dataset.detailKey)) node.open = openDetails.get(node.dataset.detailKey);
    });
    document.querySelectorAll('[data-elapsed]').forEach(node => {
      node.dataset.observedAt = String(Date.now());
    });
    const node = document.getElementById('companion-detail');
    if (!node) return;
    if (node.dataset.terminal === 'yes') {
      if (source) source.close(); source = null;
      status('Terminal snapshot'); return;
    }
    const path = `/companion/${node.dataset.resource}/${node.dataset.id}`;
    if (identity === path && (source || stopped)) return;
    if (source) source.close();
    identity = path;
    stopped = false;
    source = new EventSource(`${path}/events`);
    source.onopen = () => status('Live progress connected');
    source.onerror = () => status('Live progress interrupted; reconnecting…');
    ['resync','refresh'].forEach(kind => source.addEventListener(kind, refresh));
    source.addEventListener('terminal', () => { source.close(); source=null; stopped=true; refresh(); });
    source.addEventListener('shutdown', () => {
      source.close(); source=null; stopped=true; status('AgentForge stopped; reload to reconnect.');
    });
    source.addEventListener('unavailable', () => {
      source.close(); source=null; stopped=true; status('Task executor unavailable; reload to resync.');
    });
  };
  setInterval(() => document.querySelectorAll('[data-elapsed]').forEach(node => {
    if (node.dataset.running !== 'yes') return;
    const value = Number(node.dataset.elapsed) + (Date.now()-Number(node.dataset.observedAt))/1000;
    node.textContent = `${Math.floor(value)} s`;
  }), 1000);
  document.addEventListener('DOMContentLoaded', connect);
  document.addEventListener('htmx:afterSwap', connect);
  ['htmx:responseError','htmx:sendError'].forEach(event => document.addEventListener(event, () => {
    status('AgentForge unavailable; reload to reconnect.');
    const node = document.querySelector('.connection');
    if (node) node.textContent = 'Connection interrupted';
  }));
  window.addEventListener('pagehide', () => {
    if (source) source.close(); source=null; stopped=false;
    if (timer) clearTimeout(timer); timer=null;
  });
  window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
})();
