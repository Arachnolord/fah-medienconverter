# ============================================================================
# Erdacht und programmiert von Fabian Hammer.
# (c) 2026 Fabian Hammer - Alle Rechte vorbehalten. Geistiges Eigentum.
# ============================================================================
"""Schlanke DOM-Attrappe fuer die Seitentests.

Beide Seiten (Verlauf und Startseite) fuehren ihr Skript hier wirklich aus:
`getElementById` kennt nur die IDs, die im HTML VOR dem Skript stehen, alles
andere liefert null -- genau wie im Browser. Ein Top-Level-Zugriff auf ein
spaeter definiertes Element bringt den Lauf zum Absturz, und der Test faellt.
"""
import json
import re
import shutil
import subprocess

NODE = shutil.which("node")

ATTRAPPE = r"""
const __html = %(html)s;
const __skriptStart = %(start)d;
// Nur IDs, die VOR dem Skript im HTML stehen, existieren beim Skriptlauf.
const __vorher = new Set();
for (const m of __html.slice(0, __skriptStart).matchAll(/\bid="([^"]+)"/g)) __vorher.add(m[1]);
const __alle = new Set();
for (const m of __html.matchAll(/\bid="([^"]+)"/g)) __alle.add(m[1]);

function mkEl(tag) {
  const e = {
    tagName: String(tag).toUpperCase(), children: [], style: {}, dataset: {},
    className: '', textContent: '', value: '', disabled: false, hidden: false,
    _listeners: {}, _attrs: {},
    classList: { _s: new Set(),
      add(...c) { c.forEach(x => this._s.add(x)); },
      remove(...c) { c.forEach(x => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, f) { if (f === undefined) f = !this._s.has(c); f ? this._s.add(c) : this._s.delete(c); return f; } },
    appendChild(c) { this.children.push(c); return c; },
    append(...c) { c.forEach(x => this.children.push(x)); },
    replaceChildren(...c) { this.children = c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); },
    addEventListener(t, f) { (this._listeners[t] = this._listeners[t] || []).push(f); },
    removeEventListener() {},
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return this._attrs[k] ?? null; },
    removeAttribute(k) { delete this._attrs[k]; },
    querySelector() { return null }, querySelectorAll() { return [] },
    focus() {}, blur() {}, click() {}, remove() {},
    canPlayType() { return 'probably'; },
    getBoundingClientRect() { return { width: 800, height: 400, top: 0, left: 0 }; },
  };
  Object.defineProperty(e, 'firstChild', { get() { return e.children[0] || null; } });
  Object.defineProperty(e, 'innerHTML', { get() { return ''; }, set() {} });
  return e;
}
const __byId = new Map();
const document = {
  getElementById(id) {
    if (!__vorher.has(id)) return null;             // <- die Falle
    if (!__byId.has(id)) __byId.set(id, mkEl('div'));
    return __byId.get(id);
  },
  createElement: mkEl,
  createElementNS: (ns, tag) => mkEl(tag),
  createDocumentFragment: () => mkEl('fragment'),
  addEventListener(t, f) { (this._l = this._l || {})[t] = f; },
  documentElement: mkEl('html'), body: mkEl('body'),
  querySelector() { return null }, querySelectorAll() { return [] },
  hidden: false, visibilityState: 'visible',
};
const window = { addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
                 innerWidth: 1200, requestAnimationFrame: f => 0, location: { href: '/' } };
const location = window.location;
const localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
const fetch = async () => ({ status: 200, ok: true, json: async () => (%(antwort)s) });
const Intl = globalThis.Intl;
const confirm = () => false, alert = () => {}, requestAnimationFrame = f => 0;
const setTimeout = (f) => 0, clearTimeout = () => {};
const EventSource = function () { return { addEventListener() {}, close() {} }; };

// Das Skript laeuft. Ein Fehler hier ist genau der Fehler aus dem Browser.
%(skript)s

// Nachtraeglich nachsehen: haben alle nach dem Laden gebundenen Listener ein
// echtes Element bekommen? DOMContentLoaded darf erst spaeter darauf zugreifen.
__vorher.clear(); __alle.forEach(x => __vorher.add(x));
if (document._l && document._l['DOMContentLoaded']) document._l['DOMContentLoaded']();
console.log(JSON.stringify({ ok: true }));
%(nachlauf)s
"""

# Was fetch standardmaessig liefert: ein leerer Verlauf ohne Bilanz.
LEERE_ANTWORT = {"entries": [], "runs": [], "total": 0, "pages": 1,
                 "overall": {"count": 0, "bytes_before": 0, "bytes_after": 0}}


def skript_und_start(seite):
    """HTML, groesster Skriptblock und dessen Position im HTML."""
    html = seite.read_text(encoding="utf-8")
    bloecke = list(re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S))
    assert bloecke, f"kein Skript in {seite.name}"
    haupt = max(bloecke, key=lambda m: len(m.group(1)))
    return html, haupt.group(1), haupt.start()


def fuehre_seite_aus(seite, tmp_path, antwort=None, nachlauf=""):
    """Fuehrt das Seitenskript in der Attrappe aus. Gibt das CompletedProcess zurueck.

    `antwort` ist, was jeder fetch-Aufruf als JSON bekommt; `nachlauf` ist JS, das
    nach dem Skript laeuft -- etwa um nach dem Laden den DOM-Stand auszugeben."""
    html, skript, start = skript_und_start(seite)
    code = ATTRAPPE % {"html": json.dumps(html), "start": start, "skript": skript,
                       "antwort": json.dumps(LEERE_ANTWORT if antwort is None else antwort),
                       "nachlauf": nachlauf}
    datei = tmp_path / (seite.stem + "_lauf.js")
    datei.write_text(code, encoding="utf-8")
    return subprocess.run([NODE, str(datei)], capture_output=True, text=True, timeout=30)
