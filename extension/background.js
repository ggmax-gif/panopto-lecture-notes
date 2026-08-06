// The content script can't reach http://127.0.0.1 from an https page without
// tripping mixed-content and private-network rules. The service worker can,
// because host_permissions covers it. So all network calls funnel through here.

const PORT = 8420;
const base = () => `http://127.0.0.1:${PORT}`;

async function call(path, options) {
  const res = await fetch(base() + path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok && !body.id) {
    throw new Error(body.error || `daemon returned ${res.status}`);
  }
  return body;
}

chrome.runtime.onMessage.addListener((msg, _sender, respond) => {
  (async () => {
    try {
      if (msg.type === 'health') {
        respond({ ok: true, data: await call('/health') });
      } else if (msg.type === 'start') {
        respond({
          ok: true,
          data: await call('/jobs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              url: msg.url,
              module: msg.module,
              analyse: msg.analyse,
              slides: msg.slides,
            }),
          }),
        });
      } else if (msg.type === 'poll') {
        respond({ ok: true, data: await call('/jobs/' + msg.id) });
      }
    } catch (e) {
      respond({ ok: false, error: e.message });
    }
  })();
  return true; // keep the message channel open for the async respond
});
