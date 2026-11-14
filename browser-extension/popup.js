const input = document.querySelector('#base');
const status = document.querySelector('#status');

async function load() {
  const values = await chrome.storage.local.get(['giftstreamBase', 'giftstreamLastEventAt', 'giftstreamLastEvent', 'giftstreamLastError', 'giftstreamTriggerActions']);
  input.value = values.giftstreamBase || 'http://127.0.0.1:8000';
  document.querySelector('#trigger').checked = values.giftstreamTriggerActions === true;
  if (values.giftstreamLastError) status.textContent = `Ostatni błąd relay: ${values.giftstreamLastError}`;
  else if (values.giftstreamLastEventAt) status.textContent = `Ostatnie zdarzenie: ${values.giftstreamLastEvent || 'odebrano'} · ${new Date(values.giftstreamLastEventAt).toLocaleTimeString()}`;
  await check();
}

async function check() {
  const base = input.value.trim().replace(/\/+$/, '');
  try {
    const response = await fetch(`${base}/api/browser/status`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    status.textContent = `Lokalny odbiornik działa · odebrano ${data.events_received} zdarzeń`;
  } catch (error) {
    status.textContent = `Nie można połączyć z GiftStream: ${error.message}. Sprawdź port widoczny w pasku adresu aplikacji.`;
  }
}

document.querySelector('#save').addEventListener('click', async () => {
  const base = input.value.trim().replace(/\/+$/, '');
  try {
    const parsed = new URL(base);
    if (!['http:', 'https:'].includes(parsed.protocol) || !['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname)) {
      throw new Error('Dozwolony jest tylko adres localhost GiftStream.');
    }
    await chrome.storage.local.set({
      giftstreamBase: base,
      giftstreamTriggerActions: document.querySelector('#trigger').checked
    });
    await check();
  } catch (error) {
    status.textContent = error.message;
  }
});

load();
