"""Shared reference layout sizing; presentation only, bounded and DPI aware."""


def dimensions(screen_width, screen_height, width, height, minimum=(600, 480)):
    available_w, available_h = max(320, screen_width - 60), max(300, screen_height - 100)
    w, h = min(width, available_w), min(height, available_h)
    return w, h, min(minimum[0], w), min(minimum[1], h)


def fit_window(win, width, height, minimum=(600, 480)):
    w, h, mw, mh = dimensions(win.winfo_screenwidth(), win.winfo_screenheight(), width, height, minimum)
    win.geometry(f'{w}x{h}')
    win.minsize(mw, mh)


def status_text(row):
    from i18n import tr
    tier = row.get('tier', 6)
    prefix = '✓' if tier in (1, 2) else '!' if tier == 5 else '◷'
    return prefix + '  ' + row.get('label', tr('Ожидается оценка'))
