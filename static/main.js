// Spinner overlay
window.showOverlay = function () {
  const o = document.getElementById('overlay');
  if (o) o.classList.add('show');
};

// Drag & Drop multi-file + anteprime (client-side)
(function () {
  const drop = document.getElementById('dropzone');
  const input = document.getElementById('file-input');
  const list = document.getElementById('file-list');
  const btn = document.getElementById('btn-choose');
  if (!drop || !input || !list) return;

  const renderItem = (file, snippet) => {
    const el = document.createElement('div');
    el.className = 'p-3 rounded-lg border bg-white';
    el.innerHTML = `
      <div class="font-medium">${file.name}</div>
      <pre class="mt-2 text-sm whitespace-pre-wrap text-gray-700">${snippet}</pre>
    `;
    list.appendChild(el);
  };

  const readSnippet = (file) => {
    return new Promise((resolve) => {
      const ext = (file.name || '').toLowerCase();
      if (ext.endsWith('.pdf') || ext.endsWith('.docx')) {
        resolve('(Anteprima veloce: il testo verrà estratto dopo il caricamento)');
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const text = reader.result ? String(reader.result) : '';
        resolve(text ? (text.slice(0, 400) + (text.length > 400 ? '…' : '')) : '(Anteprima non disponibile)');
      };
      reader.onerror = () => resolve('(Anteprima non disponibile)');
      reader.readAsText(file);
    });
  };

  const handleFiles = async (files) => {
    if (!files || files.length === 0) return;
    list.innerHTML = ''; // reset visivo
    for (const f of files) {
      if (f.size > 16 * 1024 * 1024) {
        renderItem(f, '(File grande >16MB: anteprima veloce disattivata, verrà elaborato dopo il caricamento)');
        continue;
      }
      const snip = await readSnippet(f);
      renderItem(f, snip);
    }
  };

  const onOver = (e) => { e.preventDefault(); drop.classList.add('ring-2','ring-indigo-400'); };
  const onLeave = () => drop.classList.remove('ring-2','ring-indigo-400');

  drop.addEventListener('dragover', onOver);
  drop.addEventListener('dragleave', onLeave);
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    onLeave();
    const files = e.dataTransfer.files;
    // Collega i file all'input del form (necessario per l'invio)
    const dt = new DataTransfer();
    for (const f of files) dt.items.add(f);
    input.files = dt.files;
    handleFiles(files);
  });

  input.addEventListener('change', (e) => handleFiles(e.target.files));
  if (btn) btn.addEventListener('click', () => input.click());
})();

// Accessibilità extra: controllo dimensioni testo A-/A+ (persistente su device)
(function () {
  const key = 'voxup-font-scale';
  const inc = document.getElementById('font-inc');
  const dec = document.getElementById('font-dec');
  const apply = (val) => {
    // scala base 16px; limiti tra 14 e 20
    const clamped = Math.max(14, Math.min(20, val));
    document.documentElement.style.fontSize = clamped + 'px';
    try { localStorage.setItem(key, String(clamped)); } catch {}
  };
  let current = 16;
  try {
    const saved = parseInt(localStorage.getItem(key) || '16', 10);
    if (!isNaN(saved)) current = saved;
  } catch {}
  apply(current);
  if (inc) inc.addEventListener('click', () => { current += 1; apply(current); });
  if (dec) dec.addEventListener('click', () => { current -= 1; apply(current); });
})();
