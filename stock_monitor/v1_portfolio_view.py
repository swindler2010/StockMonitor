"""V1 dashboard inside the Stock Monitor portfolio window."""
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from tkinter import StringVar, ttk
from tkinter.scrolledtext import ScrolledText
from urllib.request import urlopen
from xml.etree import ElementTree
import webbrowser

from fundamentals_view import cache_matches
from i18n import tr
from v1_portfolio import analyze, simulate
from v1_scalable import SyncError, read_local, refresh

ECB_RATES = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def eur_rates():
    try:
        with urlopen(ECB_RATES, timeout=5) as response:
            data = response.read(65536)
        nodes = list(ElementTree.fromstring(data).iter())
        dates = [date.fromisoformat(node.attrib["time"]) for node in nodes if "time" in node.attrib]
        if len(dates) != 1 or not 0 <= (datetime.now(timezone.utc).date() - dates[0]).days <= 5:
            return {}
        return {n.attrib["currency"]: float(n.attrib["rate"]) for n in nodes if "rate" in n.attrib}
    except (OSError, ValueError, ElementTree.ParseError):
        return {}


def money(value):
    return "—" if value is None else f"{value:,.2f} EUR"


def percent(value):
    return "—" if value is None else f"{value:.1%}"


def scenario_lines(items):
    lines = []
    for item in items:
        top = ", ".join(f"{name} {money(loss)}" for name, loss in item["contributors"][:3])
        lines.append(f'{tr(item["name"])}  {money(item["loss"])} ({percent(item["pct"])})  '
                     f'{tr("Покрытие")} {percent(item["coverage"])}  {tr(item["basis"])}')
        if top:
            lines.append(top)
    return "\n".join(lines)


class Dashboard:
    def __init__(self, app, frame, tabs):
        self.app, self.frame, self.tabs = app, frame, tabs
        self.report = None
        self.portfolios = []
        self.selected = None
        self.future = None
        frame.bind("<Destroy>", self.destroy, add="+")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v1-scalable")

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", padx=12, pady=8)
        self.status = StringVar(value=tr("Подключение Scalable Capital"))
        ttk.Label(toolbar, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Button(toolbar, text=tr("Синхронизировать SC"), command=lambda: self.start(True)).pack(side="right")
        self.picker = ttk.Combobox(toolbar, state="readonly")
        self.picker.bind("<<ComboboxSelected>>", self.select_portfolio)

        columns = ("name", "quantity", "value", "weight", "decision", "entry")
        self.rows = ttk.Treeview(frame, columns=columns, show="headings", height=9)
        headers = ("Инструмент", "Количество", "Стоимость", "Вес", "Рекомендация", "Зона входа")
        for column, title, width in zip(columns, headers, (220, 85, 135, 80, 220, 120)):
            self.rows.heading(column, text=tr(title))
            self.rows.column(column, width=width, stretch=column in ("name", "decision"))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.rows.pack(fill="x", padx=12)
        self.rows.bind("<<TreeviewSelect>>", self.detail)
        self.summary = ttk.Label(frame, justify="left")
        self.summary.pack(fill="x", padx=12, pady=6)

        controls = ttk.Frame(frame)
        controls.pack(fill="x", padx=12)
        ttk.Label(controls, text=tr("Модель сделки в EUR (+ покупка, − продажа)")).pack(side="left")
        self.amount = ttk.Entry(controls, width=12)
        self.amount.pack(side="left", padx=6)
        ttk.Button(controls, text=tr("До и после"), command=self.what_if).pack(side="left")
        self.details = ScrolledText(frame, height=12, wrap="word", state="disabled")
        self.details.pack(fill="both", expand=True, padx=12, pady=8)
        self.timer = frame.after(50, self.start)

    def destroy(self, event):
        if event.widget is self.frame:
            if self.timer:
                self.frame.after_cancel(self.timer)
            self.pool.shutdown(wait=False, cancel_futures=True)

    def load(self, watchlist, interactive, selected):
        store = self.app.store
        previous = read_local(store.directory)
        if selected is None and previous:
            selected = previous["holdings"]["portfolioId"]
        if previous and selected != previous["holdings"]["portfolioId"]:
            previous = None
        status = None
        try:
            snapshot = refresh(store.directory, selected, interactive)
        except SyncError as exc:
            snapshot, status = previous, str(exc)
        except Exception:
            snapshot, status = previous, "SC_TRANSPORT_UNAVAILABLE"
        if not snapshot or "selection" in snapshot:
            return snapshot, status

        def financials(info):
            if not info:
                return {}
            data = store.setting("fundamentals:" + info["isin"], {})
            return data if cache_matches(data, info) and data.get("status") != "IDENTITY_MISMATCH" else {}

        def market_price(info, now):
            from paper_hourly import fresh_quote
            from pandas import Timestamp
            receipt = store.setting("gettex_chart:" + info["isin"] + ":15m", {}) or {}
            identity = receipt.get("identity") or {}
            if any(identity.get(key) != value for key, value in (
                ("source", "gettex_chart"), ("venue", "GETTEX"), ("currency", "EUR"),
                ("timezone", "Europe/Berlin"),
            )) or not identity.get("symbol"):
                return None
            try:
                quote = fresh_quote(store, info, identity, Timestamp(now))
            except (KeyError, ValueError):
                return None
            return (quote.get("bars") or [{}])[-1].get("close")

        report = analyze(snapshot, watchlist, financials, lambda isin: store.setting("news:" + isin, {}) or {},
                         currency_rates=eur_rates(), load_quote=market_price)
        if status:
            report["current"] = False
            for row in report["positions"]:
                row.update(decision="Обновить данные", reason="Показан последний доступный снимок Scalable Capital.")
        report["portfolio_id"] = snapshot["holdings"]["portfolioId"]
        return report, status

    def select_portfolio(self, _event):
        index = self.picker.current()
        if index >= 0:
            self.selected = self.portfolios[index]["portfolioId"]
            self.start(True)

    def start(self, interactive=False):
        if self.future is not None:
            return
        if self.timer:
            self.frame.after_cancel(self.timer)
        if not interactive and self.tabs.select() != str(self.frame):
            self.timer = self.frame.after(3000, self.start)
            return
        self.status.set(tr("Обновление Scalable Capital…"))
        self.future = self.pool.submit(self.load, [dict(row) for row in self.app.watchlist], interactive, self.selected)
        self.timer = self.frame.after(200, self.poll)

    def poll(self):
        if not self.future.done():
            self.timer = self.frame.after(200, self.poll)
            return
        try:
            report, error = self.future.result()
        except Exception:
            report, error = None, "V1_DATA_UNAVAILABLE"
        self.future = None
        self.timer = self.frame.after(15 * 60 * 1000, self.start)
        self.render(report, error)

    def render(self, report, error):
        self.report = None
        self.rows.delete(*self.rows.get_children())
        self.summary.configure(text="")
        self.write("")
        if report and "selection" in report:
            self.portfolios = report["selection"]
            self.picker.configure(values=[p.get("displayName") or f'{tr("Портфель")} {index}'
                                          for index, p in enumerate(self.portfolios, 1)])
            self.picker.set("")
            self.picker.pack(side="right", padx=8)
            self.status.set(tr("Выберите портфель Scalable Capital"))
            return
        self.picker.pack_forget()
        status = {
            "SC_DEPENDENCIES_MISSING": "Установите зависимости подключения V1",
            "SC_AUTH_REQUIRED": "Нажмите «Синхронизировать SC» для входа",
            "SC_AUTH_CANCELLED": "Вход не завершён",
        }.get(error, "Связь со Scalable Capital недоступна" if error else "")
        if not report:
            self.status.set(tr(status or "Нет данных Scalable Capital") + (f" · {error}" if error else ""))
            return
        self.report = report
        self.selected = report["portfolio_id"]
        self.status.set((tr(status) + " · " if status else "") + str(report["synced_at"]))
        for row in report["positions"] + report["candidates"]:
            zone = row.get("zone")
            self.rows.insert("", "end", iid=row["isin"], values=(
                row["name"], row.get("quantity", "—"), money(row.get("value")),
                percent(row.get("weight")), tr(row["decision"]), money(zone["buy_below"] if zone else None)))
        value_coverage = report["valued"] / report["total"] if report["total"] else None
        self.summary.configure(text=f'{tr("Оценка SC")} {money(report["total"])} · '
                                    f'{tr("Средства без кредита")} {money(report["cash"])} · '
                                    f'{tr("Оценены позиции")} {report["covered"]}/{len(report["positions"])} '
                                    f'({percent(value_coverage)} {tr("от оценки SC")})')
        self.write(tr("Стресс-тесты портфеля") + "\n" + scenario_lines(report["scenarios"]))

    def write(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def active(self):
        if self.report and self.rows.selection():
            isin = self.rows.selection()[0]
            return next((row for row in self.report["positions"] + self.report["candidates"] if row["isin"] == isin), None)
        return None

    def detail(self, _event=None):
        row = self.active()
        if not row:
            return
        facts, zone = row["facts"], row.get("zone")
        lines = [f'{row["name"]} · {row["isin"]}', tr(row["decision"]), tr(row.get("reason", ""))]
        if zone:
            lines.extend([f'{tr("Оценка по денежному потоку")} '
                          f'{money(zone["low"])} / {money(zone["base"])} / {money(zone["high"])}',
                          f'{tr("Зона покупки до")} {money(zone["buy_below"])}',
                          tr("Исследовательская модель. Доходность денежного потока 10% / 7,5% / 5%.")])
        lines.extend([str(facts.get("source_url") or ""), str(facts.get("updated") or "")])
        for key, title in (("TotalRevenue", "Выручка"), ("FreeCashFlow", "Свободный денежный поток"),
                           ("OperatingIncomeMargin", "Операционная маржа"), ("TotalDebt", "Долг")):
            values = facts["series"].get(key, [])[:3]
            if values:
                numbers = ", ".join(f"{period} {percent(value) if key.endswith('Margin') else f'{value:,.0f}'}"
                                    for period, value in values)
                lines.append(f"{tr(title)}: {numbers}")
        self.write("\n".join(lines))
        self.details.configure(state="normal")
        self.details.insert("end", "\n\n" + tr("Новости") + "\n")
        for index, item in enumerate(row["articles"]):
            tag = f"url{index}"
            self.details.insert("end", item["title"] + "\n", tag)
            self.details.tag_configure(tag, foreground="#2563eb", underline=True)
            self.details.tag_bind(tag, "<Button-1>", lambda _event, url=item["url"]: webbrowser.open(url))
            self.details.insert("end", f'{item["date"]} · {item["source"] or ""}\n{tr(item["impact"])}\n\n')
        if not row["articles"]:
            self.details.insert("end", tr("Свежие новости в СМ отсутствуют"))
        self.details.configure(state="disabled")

    def what_if(self):
        row = self.active()
        if not row:
            return
        try:
            amount = float(self.amount.get().strip().replace(",", "."))
            outcome = simulate(self.report, row["isin"], amount)
        except ValueError:
            self.write(tr("Проверьте сумму, свежесть данных, размер позиции и доступные средства без кредита."))
            return
        started = scenario_lines(self.report["scenarios"])
        changed = scenario_lines(outcome["scenarios"])
        self.write(f'{row["name"]} · {money(amount)}\n'
                   f'{tr("Вес до")} {percent(outcome["before"])} → {tr("Вес после")} {percent(outcome["after"])}\n\n'
                   f'{tr("До")}\n{started}\n\n{tr("После")}\n{changed}')
