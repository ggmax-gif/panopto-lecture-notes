// Injects a button into the Panopto viewer. Clicking it hands the current
// lecture URL to the local daemon and streams progress back into a panel.

const send = (msg) => new Promise((r) => chrome.runtime.sendMessage(msg, r));

function moduleName() {
  // Panopto titles look like "24/03/2026 @ 13:32 - BEF2014_L1/ - Financial
  // Reporting and Analysis". The middle chunk is the module code.
  const t = document.title.replace(/\s*-\s*Panopto\s*$/i, '');
  const code = t.match(/\b([A-Z]{2,4}\d{3,4})\b/);
  if (code) return code[1];
  const parts = t.split(' - ').map((s) => s.trim()).filter(Boolean);
  return parts.length > 1 ? parts[parts.length - 1] : 'Unsorted';
}

// Just enough markdown for model output: headings, bold, bullets, code.
function render(md) {
  const esc = (s) =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');

  const out = [];
  let list = false;
  for (const raw of md.split('\n')) {
    const line = raw.trimEnd();
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const li = line.match(/^\s*[-*]\s+(.*)$/) || line.match(/^\s*\d+\.\s+(.*)$/);
    if (li) {
      if (!list) { out.push('<ul>'); list = true; }
      out.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    if (list) { out.push('</ul>'); list = false; }
    if (h) out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (list) out.push('</ul>');
  return out.join('');
}

function buildPanel() {
  const el = document.createElement('div');
  el.id = 'ls-panel';
  el.innerHTML = `
    <header>
      <span class="ls-title">Lecture Notes</span>
      <button class="ls-close" title="Close">&times;</button>
    </header>
    <div class="ls-log"></div>
    <div class="ls-notes"></div>
    <footer>
      <button class="ls-copy">Copy notes</button>
      <span class="ls-path"></span>
    </footer>`;
  document.body.appendChild(el);
  el.querySelector('.ls-close').onclick = () => el.remove();
  el.querySelector('.ls-copy').onclick = () => {
    navigator.clipboard.writeText(el.dataset.notes || '');
    el.querySelector('.ls-copy').textContent = 'Copied';
  };
  return el;
}

async function run(withSlides) {
  const panel = document.getElementById('ls-panel') || buildPanel();
  const log = panel.querySelector('.ls-log');
  const notes = panel.querySelector('.ls-notes');
  log.textContent = '';
  notes.innerHTML = '';

  const line = (t) => {
    log.textContent += t + '\n';
    log.scrollTop = log.scrollHeight;
  };

  const health = await send({ type: 'health' });
  if (!health || !health.ok) {
    line('Can’t reach the daemon on 127.0.0.1:8420.');
    line('Start it with:  ./lecturescrape.py serve');
    return;
  }
  line(`daemon up — analysis via ${health.data.model}`);

  const started = await send({
    type: 'start',
    url: location.href,
    module: moduleName(),
    analyse: true,
    slides: withSlides,
  });
  if (!started.ok) return line('error: ' + started.error);

  const id = started.data.id;
  let shown = 0;

  const tick = async () => {
    const res = await send({ type: 'poll', id });
    if (!res.ok) return line('error: ' + res.error);
    const job = res.data;

    for (; shown < job.log.length; shown++) line(job.log[shown]);

    if (job.state === 'done') {
      panel.dataset.notes = job.notes || '';
      notes.innerHTML = render(job.notes || '');
      panel.querySelector('.ls-path').textContent = job.title || '';
      return;
    }
    if (job.state === 'error') return line('failed: ' + (job.error || 'unknown'));
    setTimeout(tick, 1500);
  };
  tick();
}

function addButton() {
  if (document.getElementById('ls-launch')) return;
  const wrap = document.createElement('div');
  wrap.id = 'ls-launch';
  wrap.innerHTML = `
    <button class="ls-go" title="Transcript + slides, then notes">Notes</button>
    <button class="ls-go ls-alt" title="Include slide images (vision models)">+slides</button>`;
  wrap.querySelector('.ls-go').onclick = () => run(false);
  wrap.querySelector('.ls-alt').onclick = () => run(true);
  document.body.appendChild(wrap);
}

addButton();
// Panopto is a SPA; re-add the button if it redraws over us.
new MutationObserver(addButton).observe(document.body, { childList: true });
