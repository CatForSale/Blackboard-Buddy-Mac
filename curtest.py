from playwright.sync_api import sync_playwright
from pathlib import Path
URL = Path(r"C:\Users\minnk\OneDrive\Desktop\Blackboard Buddy\output\dashboard.html").as_uri()
STYLES = ['live', 'ring', 'dot', 'off']
bad = []
with sync_playwright() as pw:
    b = pw.chromium.launch(channel="msedge")
    for style in STYLES:
        for theme in ('light', 'dark'):
            pg = b.new_page()
            # the saved setting, exactly as the app stores it - no introSeen shortcut this time
            pg.add_init_script("localStorage.look=JSON.stringify({cursor:%r,theme:%r})" % (style, theme))
            pg.goto(URL)
            pg.wait_for_timeout(7000)          # past the opening screen
            seen = pg.evaluate("""() => {
              const h = getComputedStyle(document.documentElement), b = getComputedStyle(document.body);
              return {html: h.display, body: b.display,
                      header: !!document.querySelector('header')?.offsetHeight,
                      main: !!document.querySelector('main')?.offsetHeight};
            }""")
            ok = seen['html'] != 'none' and seen['body'] != 'none' and seen['header'] and seen['main']
            print(f"  {style:5} {theme:5} {'OK  ' if ok else 'BLANK'} {seen}")
            if not ok:
                bad.append((style, theme))
            pg.close()
    b.close()
print(("FAILED: " + str(bad)) if bad else "all pointer styles render in both themes")
