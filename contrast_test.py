import os
import sys
from pathlib import Path

APP = r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy"
os.chdir(APP)
sys.path.insert(0, APP)
from playwright.sync_api import sync_playwright  # noqa: E402

page = Path("output/dashboard.html").resolve()
STUB = """window.pywebview={api:new Proxy({}, {get:(_, k) => async () => {
  if (k === 'state') return {};
  if (k === 'nb_list') return [];
  return null;
}})};"""

JS = r"""
(() => {
  const lum = c => { const f = c.map(v => { v /= 255; return v <= .03928 ? v/12.92 : Math.pow((v+.055)/1.055, 2.4); });
    return .2126*f[0] + .7152*f[1] + .0722*f[2]; };
  const parse = s => { const m = (s||'').match(/[\d.]+/g); return m ? m.slice(0,3).map(Number).concat(m[3] === undefined ? 1 : +m[3]) : null; };
  const bgOf = el => {  // walk up for the first opaque-enough background
    let n = el, acc = null;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c[3] > 0.5) { acc = c; break; }
      n = n.parentElement;
    }
    return acc || [255,255,255,1];
  };
  const view = [...document.querySelectorAll('.mod-view')].find(x => !x.hidden);
  const bad = [], seen = new Set();
  view.querySelectorAll('*').forEach(el => {
    const own = [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) return;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || +st.opacity === 0) return;
    const fg = parse(st.color), bg = bgOf(el);
    if (!fg) return;
    const a = fg[3];
    const mix = fg.slice(0,3).map((v,i) => v*a + bg[i]*(1-a));
    const L1 = lum(mix), L2 = lum(bg.slice(0,3));
    const ratio = (Math.max(L1,L2)+.05) / (Math.min(L1,L2)+.05);
    const size = parseFloat(st.fontSize), bold = +st.fontWeight >= 700;
    const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
    if (ratio < need) {
      const key = st.color + '|' + bg.join(',') + '|' + el.className;
      if (seen.has(key)) return; seen.add(key);
      bad.push({tag: el.tagName, cls: String(el.className).slice(0,40), color: st.color,
                size: st.fontSize, ratio: +ratio.toFixed(2), need,
                text: el.textContent.trim().slice(0, 50)});
    }
  });
  return bad;
})()
"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    for scheme in ("light", "dark"):
        pg = b.new_page(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
        pg.add_init_script("sessionStorage.introSeen='1';localStorage.tab='courses';" + STUB)
        pg.goto(page.as_uri())
        pg.wait_for_timeout(1500)
        pg.evaluate("document.documentElement.dataset.theme = %r" % scheme)
        print("=" * 20, scheme)
        for mod in ("Artificial intelligence", "Operating systems", "BSC (HONS)"):
            pg.locator("#courses button.course", has_text=mod).first.click()
            pg.wait_for_timeout(400)
            pg.evaluate("""(() => { const v = [...document.querySelectorAll('.mod-view')].find(x => !x.hidden);
                            v.querySelectorAll('details').forEach(x => x.open = true); })()""")
            pg.wait_for_timeout(500)
            for row in pg.evaluate(JS):
                print(f"  [{mod[:12]}] {row['ratio']} (need {row['need']}) {row['tag']}.{row['cls']} "
                      f"{row['size']} {row['color']} :: {row['text']!r}")
            pg.locator(".mod-view:not([hidden]) .back").first.click()
            pg.wait_for_timeout(300)
        pg.close()
    b.close()
