const DEFAULT_BASE = 'http://127.0.0.1:8000';

async function currentBase() {
  const { giftstreamBase } = await chrome.storage.local.get('giftstreamBase');
  return String(giftstreamBase || DEFAULT_BASE).replace(/\/+$/, '');
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'giftstream-event' || !message.event) return false;
  (async () => {
    const base = await currentBase();
    const { giftstreamTriggerActions } = await chrome.storage.local.get('giftstreamTriggerActions');
    const response = await fetch(`${base}/api/browser/event`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event: message.event, trigger_actions: giftstreamTriggerActions === true }),
      cache: 'no-store'
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `GiftStream HTTP ${response.status}`);
    await chrome.storage.local.set({
      giftstreamLastEventAt: Date.now(),
      giftstreamLastEvent: `${message.event.type}: ${message.event.gift_name || message.event.comment || message.event.nickname || message.event.user || ''}`,
      giftstreamLastError: ''
    });
    sendResponse({ ok: true, result: body });
  })().catch(async error => {
    await chrome.storage.local.set({ giftstreamLastError: String(error.message || error) });
    sendResponse({ ok: false, error: String(error.message || error) });
  });
  return true;
});
