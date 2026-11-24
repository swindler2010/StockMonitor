"""Dedicated Scalable Capital portfolio window for V1."""
from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from tkinter import Menu, StringVar, Toplevel, simpledialog, ttk
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


def show(app):
    existing = getattr(app, "_scalable_v1_window", None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        return existing
    window = Toplevel(app.root)
    window.title(tr("V1 · Scalable Capital"))
    window.geometry("980x630")
    window.minsize(770, 500)
    app._scalable_v1_window = window
    frame = ttk.Frame(window, padding=(16, 12))
    frame.pack(fill="both", expand=True)
    Dashboard(app, frame)
    return window


class Dashboard:
    POSITIONS = ("name", "quantity", "value", "pnl", "decision")
    CANDIDATES = ("name", "price", "entry", "decision")

    def __init__(self, app, frame):
        self.app, self.frame = app, frame
        self.report = None
        self.portfolios = []
        self.selected = None
        self.future = None
        self.section = "positions"
        frame.bind("<Destroy>", self.destroy, add="+")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v1-scalable")

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        self.status = StringVar(frame, value=tr("Подключение Scalable Capital…"))
        ttk.Label(toolbar, textvariable=self.status).pack(side="left", fill="x", expand=True)
        self.sync = ttk.Button(toolbar, text=tr("Обновить"), command=lambda: self.start(True))
        self.sync.pack(side="right")
        self.picker = ttk.Combobox(toolbar, state="readonly", width=22)
        self.picker.bind("<<ComboboxSelected>>", self.select_portfolio)

        overview = ttk.Frame(frame)
        overview.pack(fill="x", pady=(10, 6))
        self.summary = ttk.Label(overview)
        self.summary.pack(side="left")
        self.analysis = ttk.Menubutton(overview, text=tr("Анализ"), state="disabled")
        self.analysis.pack(side="right")
        actions = Menu(self.analysis, tearoff=False)
        actions.add_command(label=tr("Позиции SC"), command=lambda: self.show_section("positions"))
        actions.add_command(label=tr("Кандидаты"), command=lambda: self.show_section("candidates"))
        actions.add_separator()
        actions.add_command(label=tr("Риск портфеля"), command=self.show_risk)
        actions.add_command(label=tr("Сценарий для выбранной бумаги…"), command=self.what_if)
        self.analysis.configure(menu=actions)

        self.section_label = ttk.Label(frame, text=tr("Позиции SC"))
        self.section_label.pack(anchor="w", pady=(2, 6))
        panes = ttk.Panedwindow(frame, orient="vertical")
        panes.pack(fill="both", expand=True)
        table = ttk.Frame(panes)
        panes.add(table, weight=3)
        columns = (
            ("name", "Инструмент", 180), ("quantity", "Количество", 84),
            ("value", "Стоимость", 112), ("pnl", "P/L", 112),
            ("decision", "Рекомендация V1", 186), ("price", "Цена", 112),
            ("entry", "Зона покупки", 116),
        )
        self.rows = ttk.Treeview(table, columns=[column for column, _, _ in columns],
                                 displaycolumns=self.POSITIONS, show="headings", selectmode="browse")
        for column, title, width in columns:
            self.rows.heading(column, text=tr(title))
            self.rows.column(column, width=width, minwidth=width - 20,
                             stretch=column in ("name", "decision"),
                             anchor="w" if column in ("name", "decision") else "e")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.rows.yview)
        self.rows.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.rows.pack(fill="both", expand=True)
        self.rows.bind("<<TreeviewSelect>>", self.detail)
        self.details = ScrolledText(panes, height=8, wrap="word", state="disabled")
        panes.add(self.details, weight=2)
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
            for row in report["positions"] + report["candidates"]:
                row.update(decision="Обновить данные", reason="Показан последний доступный снимок Scalable Capital.",
                           news_reason=None)
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
        self.status.set(tr("Обновление Scalable Capital…"))
        self.sync.state(["disabled"])
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
        selection = self.rows.selection()
        self.report = None
        self.sync.state(["!disabled"])
        self.analysis.state(["disabled"])
        self.summary.configure(text="")
        if report and "selection" in report:
            self.show_section("positions")
            self.portfolios = report["selection"]
            self.picker.configure(values=[p.get("displayName") or f'{tr("Портфель")} {index}'
                                          for index, p in enumerate(self.portfolios, 1)])
            self.picker.set("")
            self.picker.pack(side="right", padx=8)
            self.status.set(tr("Выберите портфель Scalable Capital"))
            return
        self.picker.pack_forget()
        status = {
            "SC_DEPENDENCIES_MISSING": "Подключение SC пока недоступно в этой сборке.",
            "SC_AUTH_REQUIRED": "Подключите Scalable Capital, чтобы увидеть позиции.",
            "auth_required": "Подключите Scalable Capital, чтобы увидеть позиции.",
            "auth_invalid": "Войдите в Scalable Capital ещё раз.",
            "SC_AUTH_CANCELLED": "Вход не завершён.",
        }.get(error, "Связь со Scalable Capital недоступна." if error else "")
        self.sync.configure(text=tr("Подключить SC" if not report or error else "Обновить"))
        if not report:
            self.status.set(tr(status or "Нет данных Scalable Capital."))
            self.show_section("positions")
            return
        self.report = report
        self.selected = report["portfolio_id"]
        try:
            date_time = datetime.fromisoformat(report["synced_at"]).astimezone()
            updated = date_time.strftime("%d.%m.%Y %H:%M")
        except (TypeError, ValueError):
            updated = "—"
        prefix = "Последние данные SC" if error or not report["current"] else "Обновлено"
        self.status.set(f"{tr(prefix)} {updated}")
        self.analysis.state(["!disabled"])
        self.summary.configure(text=f'{tr("Стоимость портфеля")} {money(report["total"])}    ·    '
                                    f'{tr("Доступно без кредита")} {money(report["cash"])}')
        self.show_section(self.section, selection[0] if selection else None)

    def show_section(self, section, selected=None):
        self.section = section
        self.rows.delete(*self.rows.get_children())
        positions = section == "positions"
        self.section_label.configure(text=tr("Позиции SC" if positions else "Кандидаты"))
        self.rows.configure(displaycolumns=self.POSITIONS if positions else self.CANDIDATES)
        for row in self.report.get(section, ()) if self.report else ():
            zone = row.get("zone")
            quantity = row.get("quantity")
            self.rows.insert("", "end", iid=row["isin"], values=(
                row["name"], f"{quantity:g}" if quantity is not None else "—",
                money(row.get("value")), money(row.get("pnl")), tr(row["decision"]),
                money(row.get("price")), money(zone["buy_below"] if zone else None)))
        if selected and self.rows.exists(selected):
            self.rows.selection_set(selected)
            self.rows.see(selected)
            self.detail()
        elif self.rows.get_children():
            self.write(tr("Выберите бумагу, чтобы увидеть причину рекомендации и новости."))
        elif self.report:
            self.write(tr("Нет открытых позиций в SC." if positions else "Нет кандидатов."))
        else:
            self.write("")

    def show_risk(self):
        if self.report:
            self.write(tr("Риск портфеля") + "\n\n" + scenario_lines(self.report["scenarios"]))

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
        if row.get("news_reason"):
            lines.append(f'{tr("До новости")} {tr(row["baseline"])} → {tr(row["decision"])}')
        if "basis_status" in row:
            lines.append(f'{tr("Вес в портфеле")} {percent(row.get("weight"))} · '
                         f'{tr("Цена SC")} {money(row.get("price"))}')
            statuses = {
                "TRANSFER_ESTIMATE": "Оценка FIFO по операциям SC. Для переводов порядок считается по дате записи у брокера.",
                "FIFO_ESTIMATE": "Оценка FIFO по суммам исполненных операций SC. Порядок по дате записи у брокера.",
                "MISSING_LOT_COST": "Нет стоимости одной или нескольких открытых покупок либо корпоративных операций.",
                "QUANTITY_MISMATCH": "Количество из истории операций не сошлось с текущей позицией SC.",
                "INCOMPLETE_OR_AMBIGUOUS": "В истории есть события, которые нельзя однозначно восстановить.",
                "SC_TRANSACTION_HISTORY_UNAVAILABLE": "История операций SC временно недоступна.",
            }
            lines.append(f'{tr("Средняя цена")} {money(row["average"])} · '
                         f'{tr("Нереализованный P/L")} {money(row["pnl"])} ({percent(row["pnl_pct"])})')
            lines.append(tr(statuses.get(row["basis_status"], "Среднюю цену пока нельзя сверить с историей операций.")))
        if zone:
            lines.extend([f'{tr("Оценка по денежному потоку")} '
                          f'{money(zone["low"])} / {money(zone["base"])} / {money(zone["high"])}',
                          f'{tr("Зона покупки до")} {money(zone["buy_below"])}',
                          tr("Исследовательская модель. Доходность денежного потока 10% / 7,5% / 5%.")])
        lines.extend(str(value) for value in (facts.get("source_url"), facts.get("updated")) if value)
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
            trust = tr("Источник компании") if item["verified"] else tr("Публикация СМИ · требует проверки")
            self.details.insert("end", f'{item["date"]} · {item["source"] or ""} · {trust}\n{tr(item["impact"])}\n\n')
        if not row["articles"]:
            self.details.insert("end", tr("Свежие новости в СМ отсутствуют"))
        self.details.configure(state="disabled")

    def what_if(self):
        row = self.active()
        if not row:
            self.write(tr("Выберите бумагу, чтобы рассчитать сценарий."))
            return
        value = simpledialog.askstring(tr("Сценарий"), tr("Сумма в EUR (+ покупка, − продажа)"),
                                       parent=self.frame)
        if value is None:
            return
        try:
            amount = float(value.strip().replace(",", "."))
            outcome = simulate(self.report, row["isin"], amount)
        except ValueError:
            self.write(tr("Проверьте сумму, свежесть данных, размер позиции и доступные средства без кредита."))
            return
        started = scenario_lines(self.report["scenarios"])
        changed = scenario_lines(outcome["scenarios"])
        self.write(f'{row["name"]} · {money(amount)}\n'
                   f'{tr("Вес до")} {percent(outcome["before"])} → {tr("Вес после")} {percent(outcome["after"])}\n\n'
                   f'{tr("До")}\n{started}\n\n{tr("После")}\n{changed}')
