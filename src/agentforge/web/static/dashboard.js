/* SSE carries refresh hints; HTML snapshots remain authoritative. */
(() => {
  let source = null;
  let taskId = null;
  let resource = "tasks";
  let target = "#task-detail";
  let timer = null;
  let loading = false;
  let dirty = false;
  let stopped = false;
  let generation = 0;
  const status = (text) => {
    const node = document.getElementById('live-status');
    if (node) node.textContent = text;
  };
  const editing = (node) => node && (
    node.contains(document.activeElement) &&
    document.activeElement.matches('input, select, textarea, button, summary, a') ||
    node.querySelector('input[type="checkbox"]:checked, form[aria-busy="true"]')
  );
  const refresh = () => {
    dirty = true;
    if (timer || loading || !taskId) return;
    timer = setTimeout(async () => {
      timer = null;
      if (!taskId) return;
      const node = document.querySelector(target);
      if (editing(node)) {
        status('Live update pending while you use the controls.');
        return;
      }
      dirty = false;
      loading = true;
      const current = generation;
      try {
        await htmx.ajax('GET', `/${resource}/${taskId}/fragment`, {
          target: target, swap: 'outerHTML'
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
    const node = document.getElementById('task-detail') || document.getElementById('council-detail');
    if (!node) return;
    const active = ['queued', 'running'].includes(node.dataset.state);
    const id = node.dataset.taskId || node.dataset.councilId;
    resource = node.dataset.councilId ? "councils" : "tasks";
    target = node.dataset.councilId ? "#council-detail" : "#task-detail";
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
    source = new EventSource(`/${resource}/${id}/events`);
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
    source.addEventListener('unavailable', () => {
      source.close(); source = null; stopped = true;
      status('Task executor unavailable; reload to resync.');
    });
  };
  document.addEventListener('focusout', () => {
    if (dirty && !loading) refresh();
  });
  document.addEventListener('change', () => {
    if (dirty && !loading) refresh();
  });
  document.addEventListener('DOMContentLoaded', () => {
    connect();
    if (window.matchMedia('(max-width: 900px)').matches) {
      document.querySelectorAll('details[data-mobile-collapse]').forEach(node => { node.open = false; });
    }
  });
  document.addEventListener('htmx:afterSwap', connect);
  const directorDisconnected = (event) => {
    if (event.detail.elt && event.detail.elt.id === 'director-session') {
      const node = document.querySelector('.director-connection');
      if (node) node.textContent = 'Disconnected. Reload to restore the saved conversation; submitted Tasks remain at Gateway.';
    }
  };
  document.addEventListener('htmx:sendError', directorDisconnected);
  document.addEventListener('htmx:responseError', directorDisconnected);
  // Keep disclosures and scroll positions across authoritative fragment refreshes.
  const snapshots = new Map();
  const fragmentId = (node) => ['director-session', 'task-detail', 'council-detail'].includes(node?.id);
  document.addEventListener('htmx:beforeRequest', (event) => {
    const node = event.detail.elt;
    if (node?.id === 'director-session' && editing(node)) {
      event.preventDefault();
      const hint = node.querySelector('.director-connection');
      if (hint) hint.textContent = 'Updates paused while you use the controls. Move focus outside the session to resume.';
    }
  });
  document.addEventListener('htmx:afterSwap', (event) => {
    const node = event.detail.elt;
    const saved = snapshots.get(node?.id);
    if (!saved) return;
    node.querySelectorAll('details').forEach((item) => {
      const key = item.querySelector('summary')?.textContent.trim();
      if (saved.details.has(key)) item.open = saved.details.get(key);
    });
    node.querySelectorAll('pre, .director-progress ol, .long-message .director-text').forEach((item, i) => {
      item.scrollTop = saved.scroll[i] || 0;
    });
    snapshots.delete(node.id);
  });
  document.addEventListener('htmx:beforeSwap', (event) => {
    const node = event.detail.target;
    if (fragmentId(node) && event.detail.xhr.status < 400) {
      // A GET in flight must also leave focused/confirmed mutation controls intact.
      if (event.detail.requestConfig?.verb === 'get' && editing(node)) {
        event.detail.shouldSwap = false;
        dirty = node.id !== 'director-session';
        return;
      }
      snapshots.set(node.id, {
        details: new Map(Array.from(node.querySelectorAll('details'), item => [item.querySelector('summary')?.textContent.trim(), item.open])),
        scroll: Array.from(node.querySelectorAll('pre, .director-progress ol, .long-message .director-text'), item => item.scrollTop)
      });
    }
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
  // Keep native POSTs native: never remove the clicked approval decision from
  // form serialization. Prevent duplicate submissions without disabling fields.
  const submitting = new Set();
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (form.method !== 'post') return;
    if (submitting.has(form)) {
      event.preventDefault();
      return;
    }
    submitting.add(form);
    form.setAttribute('aria-busy', 'true');
  });
  document.addEventListener('htmx:afterRequest', (event) => {
    const form = event.detail.elt;
    submitting.delete(form);
    form?.removeAttribute('aria-busy');
  });
  window.addEventListener('pageshow', (event) => {
    submitting.forEach(form => form.removeAttribute('aria-busy'));
    submitting.clear();
    if (event.persisted) connect();
  });
})();
