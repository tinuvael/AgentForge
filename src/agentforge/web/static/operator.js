(() => {
  const russian = {
    "Live update pending while you use the controls.": "Обновление отложено, пока вы работаете с формой.",
    "Snapshot refresh unavailable; reload to reconnect.": "Не удалось обновить данные. Обновите страницу.",
    "Terminal snapshot": "Итоговое состояние",
    "Live updates connected": "Обновления подключены",
    "Live updates interrupted; reconnecting…": "Связь прервалась. Подключаемся снова…",
    "Server stopped; reload to reconnect.": "Сервер остановлен. Обновите страницу.",
    "Task executor unavailable; reload to resync.": "Исполнитель недоступен. Обновите страницу.",
    "Snapshot unavailable; reload to reconnect.": "Данные недоступны. Обновите страницу.",
    "Live progress connected": "Обновления подключены",
    "Live progress interrupted; reconnecting…": "Связь прервалась. Подключаемся снова…",
    "AgentForge stopped; reload to reconnect.": "AgentForge остановлен. Обновите страницу.",
    "AgentForge unavailable; reload to reconnect.": "AgentForge недоступен. Обновите страницу.",
    "Waiting for live snapshot…": "Ожидаем обновление…",
    "Sending request…": "Отправляем запрос…",
    "Connection interrupted": "Связь прервалась"
  };
  document.addEventListener('htmx:beforeRequest', event => {
    const form = event.detail.elt;
    if (!form.matches('.operator-compose, .director-compose')) return;
    form.setAttribute('aria-busy', 'true');
    const status = form.querySelector('[data-send-status]');
    if (status) status.textContent = window.agentforgeText('Sending request…');
  });
  document.addEventListener('htmx:afterRequest', event => {
    const form = event.detail.elt;
    if (!form.matches('.operator-compose, .director-compose')) return;
    form.removeAttribute('aria-busy');
    const status = form.querySelector('[data-send-status]');
    if (status) status.textContent = '';
  });
  window.agentforgeText = key => document.documentElement.lang === 'ru' ? (russian[key] ?? key) : key;
})();
