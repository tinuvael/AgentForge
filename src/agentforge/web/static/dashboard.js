/* SSE carries refresh hints; HTML snapshots remain authoritative. */
(() => {
  let source = null;
  let taskId = null;
  let timer = null;
  let loading = false;
  let dirty = false;
  let stopped = false;
  let generation = 0;
  const status = (text) => {
    const node = document.getElementById('live-status');
    if (node) node.textContent = text;
  };
  const refresh = () => {
    dirty = true;
    if (timer || loading || !taskId) return;
    timer = setTimeout(async () => {
      timer = null;
      if (!taskId) return;
      dirty = false;
      loading = true;
      const current = generation;
      try {
        await htmx.ajax('GET', `/tasks/${taskId}/fragment`, {
          target: '#task-detail', swap: 'outerHTML'
        });
      } catch (_) {
        status('Snapshot refresh unavailable; reload to reconnect.');
      } finally {
        loading = false;
        if (current === generation && dirty) refresh();
      }
    }, 200);
  };
  const connect = () => {
    const node = document.getElementById('task-detail');
    if (!node) return;
    const active = ['queued', 'running'].includes(node.dataset.state);
    const id = node.dataset.taskId;
    if (!active) {
      if (source) source.close();
      source = null;
      stopped = true;
      status('Terminal snapshot');
      return;
    }
    if (taskId === id && (source || stopped)) {
      status(source && source.readyState === EventSource.OPEN
        ? 'Live updates connected' : 'Waiting for live snapshot…');
      return;
    }
    if (source) source.close();
    taskId = id;
    generation += 1;
    stopped = false;
    source = new EventSource(`/tasks/${id}/events`);
    source.onopen = () => status('Live updates connected');
    source.onerror = () => status('Live updates interrupted; reconnecting…');
    ['refresh', 'resync'].forEach((kind) => source.addEventListener(kind, refresh));
    source.addEventListener('terminal', () => {
      source.close(); source = null; stopped = true; refresh();
    });
    source.addEventListener('shutdown', () => {
      source.close(); source = null; stopped = true;
      status('Server stopped; reload to reconnect.');
    });
  };
  document.addEventListener('DOMContentLoaded', connect);
  document.addEventListener('htmx:afterSwap', connect);
  document.addEventListener('htmx:beforeSwap', (event) => {
    if (event.detail.xhr.status >= 400) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });
  window.addEventListener('pagehide', () => {
    if (source) source.close();
    source = null;
    stopped = false;
    if (timer) clearTimeout(timer);
    timer = null;
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) connect();
  });
})();
