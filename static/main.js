function showOverlay(){ document.getElementById('overlay')?.classList.add('show'); }

function copyBlock(btn){
  const container = btn.closest('.p-4, .border, .rounded-lg');
  const target = container?.querySelector('[data-copy-target]');
  if(!target) return;
  const text = target.innerText;
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = 'Copiato!';
    setTimeout(()=> btn.textContent = 'Copia', 1200);
  });
}

// Drag & Drop MULTI-FILE con anteprima locale (solo per file testo)
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const localPrev = document.getElementById('local-previews');

if (dropzone && fileInput) {
  ['dragenter','dragover'].forEach(ev => dropzone.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); dropzone.classList.add('ring', 'ring-indigo-400');
  }));
  ['dragleave','drop'].forEach(ev => dropzone.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); dropzone.classList.remove('ring', 'ring-indigo-400');
  }));
  dropzone.addEventListener('drop', (e) => {
    const files = Array.from(e.dataTransfer.files || []);
    const dt = new DataTransfer();
    Array.from(fileInput.files || []).forEach(f => dt.items.add(f));
    files.forEach(f => dt.items.add(f));
    fileInput.files = dt.files;

    if (localPrev) {
      localPrev.innerHTML = '';
      files.forEach(file => {
        const wrap = document.createElement('div');
        wrap.className = 'mt-2 p-2 border rounded';
        const title = document.createElement('div');
        title.className = 'font-mono text-sm text-gray-700';
        title.textContent = file.name;
        wrap.appendChild(title);
        if (file.type.startsWith('text/') || file.name.endsWith('.md') || file.name.endsWith('.txt')) {
          const reader = new FileReader();
          reader.onload = () => {
            const pre = document.createElement('pre');
            pre.className = 'whitespace-pre-wrap text-xs text-gray-700';
            const t = String(reader.result || '');
            pre.textContent = t.slice(0, 500) + (t.length > 500 ? '…' : '');
            wrap.appendChild(pre);
          };
          reader.readAsText(file);
        }
        localPrev.appendChild(wrap);
      });
    }
  });
}

// Ticker Notizie (in fondo pagina)
async function loadNewsTicker(){
  const el = document.getElementById('news-ticker');
  if(!el) return;
  try{
    const res = await fetch('/news.json');
    const items = await res.json();
    if(!Array.isArray(items)) return;
    let i = 0;
    const render = () => {
      if(items.length === 0) return;
      const it = items[i % items.length];
      el.innerHTML = `<a class="hover:underline" href="${it.link}" target="_blank" rel="noopener">• [${it.source}] ${it.title}</a>`;
      i++;
    };
    render();
    setInterval(render, 6000);
  }catch(e){}
}
document.addEventListener('DOMContentLoaded', loadNewsTicker);
