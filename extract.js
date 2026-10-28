// Liest die Rechtsquellen und Werte je Bundesland direkt aus dem Tool (docs/index.html).
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const a = html.indexOf('const DATA = {');
const b = html.indexOf('const DATA_STAND');
if (a < 0 || b < 0) { console.error('DATA im Tool nicht gefunden'); process.exit(1); }
const DATA = new Function(html.slice(a, b) + '\nreturn DATA;')();
const out = {};
for (const [name, d] of Object.entries(DATA)) {
  const urls = [...new Set([d.gesetz && d.gesetz.url, d.grenzQuelle && d.grenzQuelle.url].filter(Boolean))];
  out[name] = { urls, flaeche: d.flaeche, hoehe: d.hoehe, grenz: d.grenzWerte || null };
}
fs.writeFileSync(process.argv[3], JSON.stringify(out, null, 1));
