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
    if (node) node.textContent = window.agentforgeText(text);
  };
  const editing = (node) => node && (
    node.contains(document.activeElement) &&
    document.activeElement.matches('input, select, textarea, button, summary, a, [tabindex="0"]') ||
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
    const session = document.getElementById('director-session');
    const conversationState = document.querySelector('.director-conversation[aria-current="page"] small');
    if (session && conversationState) conversationState.textContent = session.dataset.stateLabel;
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
  const directorStatus = (text) => {
    const node = document.querySelector('.director-connection');
    if (node) node.textContent = window.agentforgeText(text);
  };
  const directorPoll = (event) => event.detail.elt?.id === 'director-session' &&
    event.detail.requestConfig?.verb === 'get';
  let directorInterrupted = false;
  ['htmx:sendError', 'htmx:responseError', 'htmx:timeout'].forEach(kind => {
    document.addEventListener(kind, (event) => {
      if (!directorPoll(event)) return;
      directorInterrupted = true;
      directorStatus('Live updates interrupted; reconnecting…');
    });
  });
  // Keep disclosures and scroll positions across authoritative fragment refreshes.
  const snapshots = new Map();
  const fragmentId = (node) => ['director-session', 'task-detail', 'council-detail'].includes(node?.id);
  const detailKey = (item) => {
    const summary = item.querySelector('summary');
    return summary?.id || summary?.textContent.trim();
  };
  document.addEventListener('htmx:beforeRequest', (event) => {
    if (directorPoll(event) && event.detail.elt.querySelector('form[aria-busy="true"]')) {
      event.preventDefault();
    }
  });
  document.addEventListener('htmx:afterSwap', (event) => {
    const node = event.detail.elt;
    const saved = snapshots.get(node?.id);
    if (!saved) return;
    node.querySelectorAll('details').forEach((item) => {
      const key = detailKey(item);
      if (saved.details.has(key)) item.open = saved.details.get(key);
    });
    node.querySelectorAll('pre, .director-progress ol, .long-message .director-text').forEach((item, i) => {
      item.scrollTop = saved.scroll[i] || 0;
    });
    if (saved.focused) {
      const control = document.getElementById(saved.focused) || document.getElementById('director-heading');
      control?.focus({preventScroll: true});
    }
    snapshots.delete(node.id);
  });
  document.addEventListener('htmx:beforeSwap', (event) => {
    const node = event.detail.target;
    if (directorPoll(event)) {
      if (event.detail.xhr.status !== 200) {
        // A failed refresh leaves the live poller and the last snapshot intact.
        event.detail.shouldSwap = false;
        return;
      }
      if (directorInterrupted) {
        directorInterrupted = false;
        directorStatus('Live updates connected');
      }
      if (node.querySelector('form[aria-busy="true"]') ||
          event.detail.xhr.getResponseHeader('X-AgentForge-Director-Snapshot') === node.dataset.snapshot) {
        event.detail.shouldSwap = false;
        return;
      }
    }
    if (fragmentId(node) && event.detail.xhr.status < 400) {
      // A GET in flight must also leave focused/confirmed mutation controls intact.
      if (!directorPoll(event) && event.detail.requestConfig?.verb === 'get' && editing(node)) {
        event.detail.shouldSwap = false;
        dirty = true;
        return;
      }
      snapshots.set(node.id, {
        details: new Map(Array.from(node.querySelectorAll('details'), item => [detailKey(item), item.open])),
        focused: directorPoll(event) && node.contains(document.activeElement) ? document.activeElement.id || 'director-heading' : null,
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
