"""Explicit headline word heuristic, not a price forecast or trading input.

Original source titles are used so UI translation cannot change classification.
Mixed wording, negation and unreadable titles remain unclassified.
"""
import re

WORDS = {
    'positive': r'\b(soars?|surges?|rall(?:y|ies)|rises?|rose|rising|gains?|gained|steigt|steigen|gestiegen|растут|выросли|record profit|beats? estimates|raises? (?:outlook|forecast)|upgrades?|rekordgewinn|gewinne steigen|повышает прогноз|рекордная прибыль|превысил[аи]? ожидания|акции растут)\b',
    'negative': r'\b(plunges?|tumbles?|slumps?|falls?|fell|falling|drops?|dropped|declines?|slips?|slid|fällt|fallen|gefallen|падают|упали|misses? estimates|cuts? (?:outlook|forecast)|downgrades?|gewinnwarnung|verluste|снижает прогноз|убыток|убытки|акции падают)\b',
}


def classify(article):
    from financial_sentiment import evaluate
    return evaluate(article)['label']


def summary(articles):
    result = dict(positive=0, negative=0, neutral=0, unknown=0)
    for article in articles:
        result[classify(article)] += 1
    total = sum(result.values())
    return dict(counts=result, total=total,
                percentages={k: v / total * 100 if total else 0 for k, v in result.items()},
                method='FINANCIAL_PHRASES_V2')


def draw(parent, articles, background):
    import tkinter as tk
    from tkinter import ttk
    from i18n import tr
    data = summary(articles)
    canvas = tk.Canvas(parent, height=104, background=background, highlightthickness=0)
    canvas.pack(fill='x', pady=5)
    colors = {'positive': '#1ddd67', 'neutral': '#8b9caf', 'unknown': '#45576b', 'negative': '#ff5966'}
    labels = {'positive': 'Позитивные', 'negative': 'Негативные', 'neutral': 'Нейтральные', 'unknown': 'Без оценки'}
    def paint(event=None):
        canvas.delete('all')
        width = max(120, canvas.winfo_width())
        left = 0
        for key in ('positive', 'neutral', 'unknown', 'negative'):
            extent = data['percentages'][key] * 1.8
            if extent:
                canvas.create_arc(8, 8, width-8, 2*(width-16)/3, start=180-left-extent,
                                  extent=extent, style='arc', outline=colors[key], width=17)
            left += extent
        canvas.create_text(width/2, 87, text=tr('Заголовков: {count}', count=data['total']), fill='#d8e5f4', font=('Segoe UI', 10, 'bold'))
    canvas.bind('<Configure>', paint)
    for key in ('positive', 'negative', 'neutral', 'unknown'):
        line = ttk.Frame(parent, style='Desk.TFrame');line.pack(fill='x', pady=2)
        ttk.Label(line, text=tr(labels[key]), foreground=colors[key], style='Desk.TLabel').pack(side='left')
        ttk.Label(line, text=f"{data['counts'][key]} · {data['percentages'][key]:.0f}%", style='Desk.TLabel').pack(side='right')
    ttk.Label(parent, text=tr('По финансовым фразам исходного заголовка. Учитываются расходы, прибыль, отрицания и смешанные факты. Не прогноз цены.'),
              style='DeskMuted.TLabel', wraplength=280, justify='left').pack(anchor='w', pady=(8, 0))
