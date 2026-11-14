(() => {
  if (!/^\/@[^/]+\/live\/?$/i.test(location.pathname)) return;

  const seen = new Map();
  const MAX_TEXT = 500;
  const candidates = [
    '[data-e2e*="comment" i]', '[data-e2e*="message" i]',
    '[data-e2e*="gift" i]', '[data-e2e*="follow" i]',
    '[data-e2e*="share" i]', '[data-e2e*="like" i]',
    '[class*="chatroom" i]', '[class*="comment" i]',
    '[class*="gift" i]', '[class*="toast" i]', '[class*="activity" i]'
  ].join(',');

  function clean(value) {
    return String(value || '').replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT);
  }

  function emit(type, fields) {
    const payload = { type, source: 'browser', user: fields.user || 'unknown',
      nickname: fields.user || '', ...fields };
    const signature = JSON.stringify(payload);
    const now = Date.now();
    if (now - (seen.get(signature) || 0) < 4000) return;
    seen.set(signature, now);
    for (const [key, at] of seen) if (now - at > 30000) seen.delete(key);
    chrome.runtime.sendMessage({ type: 'giftstream-event', event: payload });
  }

  function parseVisibleNotice(text) {
    const value = clean(text);
    if (!value || value.length > MAX_TEXT) return null;
    let match;
    if ((match = value.match(/^(.{1,40}?)\s+(?:sent|gave)\s+(?:you\s+)?(?:(?:a|an)\s+)?(.+?)(?:\s+[x×]\s*(\d+))?[.!]?$/i)) ||
        (match = value.match(/^(.{1,40}?)\s+(?:wysłał|wysłała|podarował|podarowała)\s+(?:ci\s+)?(.+?)(?:\s+[x×]\s*(\d+))?[.!]?$/i))) {
      return { type: 'gift', fields: { user: clean(match[1]), nickname: clean(match[1]),
        gift_name: clean(match[2]), repeat_count: Math.max(1, Number(match[3]) || 1), repeat_end: true } };
    }
    const patterns = [
      ['follow', /^(.{1,40}?)\s+(?:followed|started following|is now following|obserwuje|zaczął obserwować|zaczęła obserwować)\b/i],
      ['share', /^(.{1,40}?)\s+(?:shared|udostępnił|udostępniła)\b/i],
      ['like', /^(.{1,40}?)\s+(?:liked|polubił|polubiła)\b/i],
      ['join', /^(.{1,40}?)\s+(?:joined|entered|dołączył|dołączyła)\b/i],
      ['subscribe', /^(.{1,40}?)\s+(?:subscribed|became a subscriber|zasubskrybował|zasubskrybowała)\b/i]
    ];
    for (const [type, pattern] of patterns) {
      match = value.match(pattern);
      if (match) return { type, fields: { user: clean(match[1]), nickname: clean(match[1]) } };
    }
    return null;
  }

  function parseComment(element, text) {
    const marker = `${element.getAttribute('data-e2e') || ''} ${element.className || ''}`.toLowerCase();
    if (!/(comment|message|chat)/.test(marker)) return null;
    const userNode = element.querySelector('[data-e2e*="user" i], [class*="username" i], [class*="user-name" i]');
    if (!userNode) return null;
    const user = clean(userNode.textContent);
    let comment = clean(text);
    if (user && comment.toLowerCase().startsWith(user.toLowerCase())) comment = clean(comment.slice(user.length).replace(/^\s*[:·-]\s*/, ''));
    if (!user || !comment || user === comment) return null;
    return { type: 'comment', fields: { user, nickname: user, comment } };
  }

  function inspect(element) {
    if (!(element instanceof Element) || !element.isConnected) return;
    const text = clean(element.innerText || element.textContent);
    if (!text || text.length > MAX_TEXT || element.children.length > 8) return;
    const notice = parseVisibleNotice(text) || parseComment(element, text);
    if (notice) emit(notice.type, notice.fields);
  }

  function scan(node) {
    if (!(node instanceof Element)) return;
    inspect(node);
    for (const element of node.querySelectorAll(candidates)) inspect(element);
  }

  const observer = new MutationObserver(records => {
    for (const record of records) {
      if (record.type === 'childList') for (const node of record.addedNodes) scan(node);
      else if (record.type === 'characterData') scan(record.target.parentElement);
    }
  });
  observer.observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  console.info('[GiftStream] Browser relay nasłuchuje widocznych wpisów LIVE; nie czyta haseł ani ramek sieciowych.');
})();
