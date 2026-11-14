# GiftStream TikTok Browser Relay (experimental)

This unpacked Chrome/Edge extension is a second, local-only source of visible TikTok LIVE events. It watches newly rendered text in the open `tiktok.com/@name/live` page and forwards recognized gift, follow, like, share, join, subscribe and comment notices to the GiftStream instance on loopback. Triggering game rules is **off by default** in the popup; first verify the received events, then opt in if desired. It does **not** inspect WebSocket frames or collect login credentials, cookies, or browser storage.

## Install

1. Start GiftStream and open `Creator Studio → Stream connection` (or the dashboard) so you can see the local address/port.
2. In Chrome or Edge, open `chrome://extensions` or `edge://extensions` and enable **Developer mode**.
3. Choose **Load unpacked** and select this `browser-extension` folder.
4. Open the extension popup, enter GiftStream's local address (default `http://127.0.0.1:8000`) and choose **Zapisz i sprawdź**. If desktop GiftStream picked another port, use that port.
5. Open the authorized creator's public TikTok LIVE page in the same browser. The extension icon should be available; the popup shows the local receiver's count and last browser event.

The listener only runs on a LIVE URL. Its small set of English/Polish message patterns is heuristic and may not match TikTok's current wording or DOM. A successful popup health check confirms only that the local receiver is reachable, not that TikTok notifications are being recognized. The ordinary TikTokLive connector remains available and is usually more complete, but uses an unofficial library and may encounter its signing provider's limits.

## Privacy and safety

- Browser events go to the local GiftStream API only; the receiver refuses non-loopback requests.
- No third-party event server, TikTok password, cookie, or token is used or transmitted by this extension.
- The extension observes only rendered DOM updates. It deliberately does not monkey-patch `WebSocket`, inspect network traffic, or call TikTok's private endpoints.
- TikTok may hide events or change the page structure. Verify event counts before relying on this source for a live show.
- If TikTokLive and Browser relay are both active, the same visible action can be received twice. Disable duplicate event rules or run one source at a time until you have validated the stream behavior.
