#!/usr/bin/env python3
"""Prüft die Rechtsquellen des Baugenehmigungshelfers und schreibt das Ergebnis
in das Tool (docs/index.html) sowie einen internen Bericht (docs/pruefbericht.html).

Aufruf:  python check.py            (Quellen und Werte kommen aus docs/index.html)
"""
import argparse
import concurrent.futures as cf
import difflib
import hashlib
import html
import io
import json
import re
import subprocess
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from pypdf import PdfReader
except Exception:  # noqa: BLE001
    PdfReader = None

ROOT = Path(__file__).parent
TOOL = ROOT / "docs" / "index.html"
REPORT = ROOT / "docs" / "pruefbericht.html"
SNAP = ROOT / "data" / "snapshots"
STATE = ROOT / "data" / "state.json"
TZ = ZoneInfo("Europe/Berlin")
HINWEIS_TAGE = 7      # so lange sehen Nutzer den Hinweis "Quelle geändert"
MIN_TEXT = 1500       # weniger Text = Seite wahrscheinlich nur per JavaScript lesbar
UA = "Mozilla/5.0 (compatible; baugenehmigungshelfer-pruefung/1.0)"


def now():
    return datetime.now(TZ)


def de(iso):
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y um %H:%M Uhr") if iso else None


def normalize(text):
    lines = [re.sub(r"[ \t\u00a0]+", " ", l).strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if l)


# ---------- Abruf ----------
from html.parser import HTMLParser  # noqa: E402

BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
         "section", "article", "table", "ul", "ol", "dd", "dt", "pre"}
SKIP = {"script", "style", "noscript", "svg"}


class Extractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and self.skip:
            self.skip -= 1
        if tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def fetch(url):
    last = None
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9",
                "Accept": "text/html,application/pdf,*/*"})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read()
                charset = r.headers.get_content_charset() or "utf-8"
            break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise last
    if raw[:4] == b"%PDF":
        if PdfReader is None:
            return {"text": "PDF-Prüfsumme: " + hashlib.sha256(raw).hexdigest(), "searchable": False}
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return {"text": normalize(text), "searchable": True}
    ex = Extractor()
    ex.feed(raw.decode(charset, errors="replace"))
    return {"text": normalize("".join(ex.parts)), "searchable": True}


def check_url(url, e, stamp):
    """Prüft eine Quelle. Gibt (text, searchable) zurück oder (None, False)."""
    e["last_checked"] = stamp
    try:
        res = fetch(url)
    except Exception as ex:  # noqa: BLE001
        e["status"], e["error"] = "fehler", str(ex)[:200]
        return None, False
    text = res["text"]
    if res["searchable"] and len(text) < MIN_TEXT:
        e["status"] = "unlesbar"
        e["error"] = "Seite liefert kaum Text (vermutlich nur per JavaScript lesbar)"
        return None, False
    e.pop("error", None)
    key = hashlib.sha1(url.encode()).hexdigest()[:10]
    snap = SNAP / f"{key}.txt"
    h = hashlib.sha256(text.encode()).hexdigest()
    if not snap.exists():
        e["status"], e["hash"], e["first_seen"] = "neu", h, stamp
    elif e.get("hash") != h:
        old = snap.read_text(encoding="utf-8")
        lines = list(difflib.unified_diff(old.splitlines(), text.splitlines(),
                                          "alt", "neu", lineterm="", n=1))
        if len(lines) > 150:
            lines = lines[:150] + ["… (Rest gekürzt)"]
        e["status"], e["last_changed"], e["hash"] = "geändert", stamp, h
        e["changes"].insert(0, {"date": stamp, "diff": "\n".join(lines)})
        e["changes"] = e["changes"][:10]
    else:
        e["status"] = "unverändert"
    snap.write_text(text, encoding="utf-8")
    return text, res["searchable"]


# ---------- Gegenprobe ----------
def zahl(n):
    return str(int(n)) if float(n).is_integer() else str(n).replace(".", ",")


def gegenprobe(cfg, texts):
    """Sucht die im Tool hinterlegten Werte im Umfeld des Wortes 'Garage'."""
    focus = []
    for t in texts:
        for m in re.finditer(r"garage", t, re.I):
            focus.append(t[m.start(): m.start() + 600])
    focus = "\n".join(focus)
    checks = []

    def add(label, value, unit_re):
        if value is None:
            return
        n = zahl(value)
        frac = r"(?:,0)?" if float(value).is_integer() else ""
        rx = rf"(?<![\d,.]){re.escape(n)}{frac}\s*(?:{unit_re})"
        checks.append({"label": label, "ok": bool(re.search(rx, focus, re.I))})

    sq = r"m²|m2|m\s*2|qm|Quadratmeter"
    ln = r"m\b|Meter"
    add(f"{zahl(cfg['flaeche'])} m²", cfg.get("flaeche"), sq) if cfg.get("flaeche") else None
    add(f"{zahl(cfg['hoehe'])} m", cfg.get("hoehe"), ln) if cfg.get("hoehe") else None
    g = cfg.get("grenz") or {}
    if g.get("proSeite"):
        add(f"{zahl(g['proSeite'])} m je Grenze", g["proSeite"], ln)
    if g.get("gesamt"):
        add(f"{zahl(g['gesamt'])} m gesamt", g["gesamt"], ln)
    return checks


# ---------- Auswertung ----------
def aggregate(name, cfg, state, texts, stamp):
    es = [state[u] for u in cfg["urls"]]
    stats = [e.get("status") for e in es]
    limit = now() - timedelta(days=HINWEIS_TAGE)
    changed = [e["last_changed"] for e in es
               if e.get("last_changed") and datetime.fromisoformat(e["last_changed"]) > limit]
    bad = ("fehler", "unlesbar")
    if changed:
        status = "aenderung"
    elif all(s in bad for s in stats):
        status = "nicht_pruefbar"
    elif any(s in bad for s in stats):
        status = "teilweise"
    elif all(s in ("neu", *bad) for s in stats):
        status = "basis"
    else:
        status = "ok"
    gp = gegenprobe(cfg, texts) if texts else None
    return {"status": status, "geprueft": stamp,
            "geaendert": max(changed) if changed else None, "gegenprobe": gp}


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laender", help="Testmodus: fertige laender.json statt Auslesen aus dem Tool")
    args = ap.parse_args()
    SNAP.mkdir(parents=True, exist_ok=True)
    if args.laender:
        laender = json.loads(Path(args.laender).read_text(encoding="utf-8"))
    else:
        tmp = ROOT / "data" / "laender.json"
        subprocess.run(["node", str(ROOT / "extract.js"), str(TOOL), str(tmp)], check=True)
        laender = json.loads(tmp.read_text(encoding="utf-8"))

    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    urls = sorted({u for c in laender.values() for u in c["urls"]})
    for u in urls:
        state.setdefault(u, {"changes": []})
    stamp = now().isoformat(timespec="minutes")

    texts = {}
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(check_url, u, state[u], stamp): u for u in urls}
        for f in cf.as_completed(futs):
            text, searchable = f.result()
            texts[futs[f]] = text if (text and searchable) else None
    state = {u: v for u, v in state.items() if u in urls}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    results = {}
    for name, cfg in laender.items():
        t = [texts[u] for u in cfg["urls"] if texts.get(u)]
        results[name] = aggregate(name, cfg, state, t, stamp)

    public = {"stand_text": de(stamp), "laender": {}}
    for name, r in results.items():
        gp = r["gegenprobe"]
        public["laender"][name] = {
            "status": r["status"],
            "geprueft_text": de(r["geprueft"]),
            "geaendert_text": de(r["geaendert"]),
            # Nutzer sehen die Gegenprobe nur, wenn sie vollständig bestanden ist
            "gegenprobe": [c["label"] for c in gp] if gp and all(c["ok"] for c in gp) else None,
        }
    embed(public)
    write_report(laender, state, results, stamp)
    print("Fertig:", len(urls), "Quellen,", len(results), "Bundesländer")


def embed(public):
    s = TOOL.read_text(encoding="utf-8")
    payload = json.dumps(public, ensure_ascii=False).replace("</", "<\\/")
    new, n = re.subn(r"/\*PRUEF_START\*/.*?/\*PRUEF_END\*/",
                     lambda m: "/*PRUEF_START*/" + payload + "/*PRUEF_END*/", s, flags=re.S)
    if n != 1:
        raise SystemExit("Marker PRUEF_START/PRUEF_END im Tool nicht gefunden")
    TOOL.write_text(new, encoding="utf-8")


# ---------- Interner Bericht ----------
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--tx:#1d2330;--mut:#667085;--bd:#e2e5ea;--ok:#1a7f4b;--chg:#c2410c;--err:#b42318;--new:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--tx:#e8ebf1;--mut:#9aa3b2;--bd:#2a3140;--ok:#4ade80;--chg:#fb923c;--err:#f87171;--new:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.5 system-ui,sans-serif;padding:16px;max-width:780px;margin-inline:auto}
h1{font-size:22px;margin:8px 0 4px}.sub{color:var(--mut);margin:0 0 16px;font-size:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin-bottom:12px}
.card.hot{border:2px solid var(--chg)}.top{display:flex;justify-content:space-between;gap:8px}
.name{font-weight:600}.badge{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap;height:fit-content}
.ok,.unverändert{color:var(--ok)}.aenderung,.geändert{color:var(--chg)}.nicht_pruefbar,.fehler,.unlesbar{color:var(--err)}.basis,.neu,.teilweise{color:var(--new)}
.meta{color:var(--mut);font-size:13px;margin-top:6px;word-break:break-all}a{color:var(--new)}
details{margin-top:8px}summary{cursor:pointer;font-size:14px}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:8px;overflow-x:auto;font-size:12px;white-space:pre-wrap}
.add{color:var(--ok)}.del{color:var(--err)}
"""
LABEL = {"ok": "keine Änderung", "basis": "Ausgangsstand", "aenderung": "Änderung erkannt",
         "teilweise": "teilweise lesbar", "nicht_pruefbar": "nicht lesbar"}


def diff_html(diff):
    out = []
    for l in diff.splitlines():
        c = "add" if l.startswith("+") and not l.startswith("+++") else \
            "del" if l.startswith("-") and not l.startswith("---") else ""
        out.append(f'<span class="{c}">{html.escape(l)}</span>')
    return "\n".join(out)


def write_report(laender, state, results, stamp):
    order = sorted(results, key=lambda n: (results[n]["status"] != "aenderung",
                                           results[n]["status"] == "ok", n))
    n_chg = sum(r["status"] == "aenderung" for r in results.values())
    cards = []
    for name in order:
        r = results[name]
        rows = []
        for u in laender[name]["urls"]:
            e = state[u]
            st = e.get("status", "neu")
            line = (f'<div class="meta"><span class="badge {st}">{st}</span> '
                    f'<a href="{html.escape(u)}" target="_blank" rel="noopener">{html.escape(u)}</a>')
            if e.get("error"):
                line += f'<br>Hinweis: {html.escape(e["error"])}'
            rows.append(line + "</div>")
            for c in e.get("changes", [])[:2]:
                rows.append(f'<details><summary>Änderung vom {de(c["date"])}</summary>'
                            f'<pre>{diff_html(c["diff"])}</pre></details>')
        gp = r["gegenprobe"]
        if gp is not None:
            txt = " · ".join(("✓ " if c["ok"] else "✗ ") + c["label"] for c in gp)
            rows.append(f'<div class="meta">Gegenprobe: {html.escape(txt)}</div>')
        cards.append(
            f'<div class="card{" hot" if r["status"] == "aenderung" else ""}">'
            f'<div class="top"><span class="name">{html.escape(name)}</span>'
            f'<span class="badge {r["status"]}">{LABEL[r["status"]]}</span></div>'
            + "".join(rows) + "</div>")
    head = f"{n_chg} Bundesland/Bundesländer mit Änderung" if n_chg else "Keine neuen Änderungen"
    REPORT.write_text(
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Prüfbericht</title><style>{CSS}</style></head><body>"
        f'<h1>Prüfbericht Baugenehmigungshelfer</h1><p class="sub">{head} · Stand {de(stamp)}</p>'
        + "".join(cards) + "</body></html>", encoding="utf-8")


if __name__ == "__main__":
    run()
