/* CotoBus — JS partagé (menu, état API, helpers) — version Vercel */
const API = '/api';

const PAGES = [
  { id: 'accueil',  href: 'index.html',    label: 'Accueil' },
  { id: 'carte',    href: 'carte.html',    label: 'Carte réseau' },
  { id: 'billet',   href: 'billet.html',   label: 'Mon billet' },
  { id: 'valideur', href: 'valideur.html', label: 'Valideur' },
  { id: 'commune',  href: 'commune.html',  label: 'Commune' },
];

function renderNav() {
  const active = document.body.dataset.page;
  const header = document.createElement('header');
  header.innerHTML = `
    <div class="nav">
      <a class="logo" href="index.html">
        <span class="logo-badge">C</span>CotoBus
        <span class="api-dot" id="apiDot" title="État de l'API"></span>
      </a>
      <nav class="nav-tabs">
        ${PAGES.map(p =>
          `<a href="${p.href}" class="${p.id === active ? 'active' : ''}">${p.label}</a>`
        ).join('')}
      </nav>
    </div>`;
  document.body.prepend(header);
}
renderNav();

async function pingApi() {
  try {
    const r = await fetch(API + '/health');
    document.getElementById('apiDot').classList.toggle('on', r.ok);
  } catch {
    document.getElementById('apiDot').classList.remove('on');
  }
}
pingApi();
setInterval(pingApi, 5000);

async function api(path, options) {
  const r = await fetch(API + path, options);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `Erreur ${r.status}`);
  return data;
}

const fmtNum = (n) => Number(n).toLocaleString('fr-FR');
const fmtTime = (iso) =>
  new Date(iso.endsWith('Z') ? iso : iso + 'Z')
    .toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
