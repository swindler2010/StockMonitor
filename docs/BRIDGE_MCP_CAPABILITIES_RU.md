# Bridge/MCP: реализованные возможности и границы

GitHub publication audit — Bridge/MCP, 10.11.2026
Область: только чтение текущих исходников Stock Monitor 0.16 TEST и прежних технических результатов Work2. Рабочая программа, процессы, конфигурация, базы, позиции и исходники не изменены. Новые тесты в этом подзадании НЕ запускались; ниже перечислены доступные безопасные наборы.

АКТУАЛЬНЫЕ КОМПОНЕНТЫ
В stock_monitor/: mcp_server.py (MCP stdio, API revision 2), bridge.py (loopback HTTP), bridge_queue.py (instance-bound очередь/квитанции), mcp_routes.py (совместимые read-only маршруты), mcp_projection.py (проекции/каталог/архив), mcp_pages.py (ограниченные leases страниц), analytical_read.py (расчёты сохранённых данных), mcp_chart_export.py (вывод существующих индикаторов Desktop), revision_diagnostics.py, freshness_view.py, snapshot_worker.py, commands.py, tunnel_runtime.py. Интеграция: desktop014.py, features.py, compute_process.py, storage.py, paper_account.py и существующие DQ/CORE/HistoryService.
Фактически работающий TunnelRuntime — stock_monitor/tunnel_runtime.py. СМ/Связь/tunnel_runtime.py является старой копией: нет submit через Dispatcher и более новой проверки health_ready. Не публиковать её как действующую реализацию.
Исходники MCP используют stdlib. calculated read зависит от общих requirements.txt (pandas/numpy/matplotlib и остальной продукт). Отдельного обязательного FastMCP/uvicorn нет.
Локальный MCP запускается командой Python: python stock_monitor/mcp_server.py; приложение должно быть открыто. Разработка: python stock_monitor/app.py. paths.py для development использует соседний test-data; Store 0.15 не импортирует. Личный Open_Test_016.cmd привязан к локальному окружению/runtime-tcl/copy_current_data, не является переносимой инструкцией установки.

ВСЕ 16 ОБЪЯВЛЕННЫХ MCP TOOLS
1. stock_monitor_health — чтение instance_id, версии/scope, capabilities, диагностического pipeline, CP summary, PAPER summary, состояния транспорта. Токен не выводится.
2. stock_monitor_snapshot — read-only секции snapshot, candlepilot_health, persisted, runtime, freshness. JSON Pointer path, offset, limit<=50, expected_revision. Публикация и current selection/freshness разделены.
3. stock_monitor_command_status — read-only квитанция из указанного instance, пагинация крупных результатов. После INDETERMINATE читать ту же квитанцию, не создавать повторную команду. Для выполненной PAPER-операции предусмотрен read-back из paper_fills.
4. stock_monitor_list_instruments — read-only полный сохранённый каталог независимо от ANALYSIS READY; ISIN/источник/площадка/валюта/TF, storage_table, количество/границы архива, сохранённые DQ/identity. Наличие ряда не выдаётся за подтверждённую пригодность.
5. stock_monitor_revision_diagnostics — read-only события Revision Watcher, journal_id, изменения, затронутые TF, наблюдения пересчёта индикаторов/DQ/сигнала, rejected publications. Отсутствующее наблюдение остаётся NOT_OBSERVED. Без загрузки/пересчёта/записи.
6. stock_monitor_read_analytics — read-only, асинхронный расчёт уже сохранённых данных без переключения UI/загрузки. Все 11 TF, exact identity, UTC from/to для calculated view, closed_only, limit<=500, offset/revision, include_chart_series. Возвращает OHLCV/CORE-индикаторы/provenance/DQ/настройки/рисунки и при запросе конфигурируемые индикаторы существующим методом Desktop. Volume Profile явно OHLCV-аппроксимация по запрошенной истории, не tick-by-price/не текущая область viewport. view=stored_archive требует точные isin/source/venue/currency/symbol и candles/series; raw timestamps и closed status не объявляются подтверждёнными; UTC range/closed_only отклоняются для raw view.
7. stock_monitor_decision_journal — read-only сохранённые рекомендации/оценки качества, не брокерские исполнения; фильтр isin, limit<=500.
8. stock_monitor_refresh — изменение состояния: запрос штатного обновления рыночных данных.
9. stock_monitor_select — выбор существующей акции, одного из 11 TF и периода; не меняет источник. Последние/month/year/Max проходят действующие aliases.
10. stock_monitor_set_levels — предложение support/resistance/stop. Локальное подтверждение, актуальность proposal/source/currency и отдельная кнопка «Сохранить». До commit статус AWAITING_SAVE, после сверки COMPLETED/SUPERSEDED.
11. stock_monitor_add_instrument — добавление проверяемого инструмента в watchlist; ISIN/name/symbol/currency и допустимые listing fields. После сохранения Desktop автоматически start_feed; брокерской заявки нет.
12. stock_monitor_remove_instrument — удаление из watchlist, сохранение истории/аудита; не действие у брокера.
13. stock_monitor_update_position — локальные quantity/average_price/stop/target/support/resistance при совместимой валюте. Это запись пользовательской позиции, не PAPER fill и не реальная сделка.
14. stock_monitor_record_decision — рекомендация BUY/HOLD/SELL/ADD/REDUCE/STOP с причиной и необязательным сценарием. model_quantity относится к отдельной аналитической модели следующего доступного полного bar open без комиссий; её нельзя путать с основным PAPER ledger или двумя V1/V2 shadow books.
15. stock_monitor_review_decision — добавление аргументированной оценки рекомендации: correct/false/late/early_exit/bad_stop/missed_opportunity/unassessed; оригинал не перезаписывается, оценка не объявляется проверенным финансовым результатом.
16. stock_monitor_paper_trade — виртуальный EUR BUY/SELL по request_id/isin/quantity, исполнение по закрытой 5m/15m. Никакой связи с реальным брокерским счётом. Устаревшее описание tool упоминает пять акций; actual paper_account.instrument_allowed уже допускает зарегистрированные EUR-акции при выполнении всех остальных условий. Автоматическая V1 по-прежнему определяется отдельной политикой owner approval; MCP-операция не включает V2.

ЗАЩИТЫ
Loopback 127.0.0.1 с динамическим портом, случайный bearer, запрет Origin, ограничение тела запроса, allowlist методов/типов, без shell/arbitrary code tool. Adapter проверяет test/0.16-dev/mcp_api_revision=2 и адрес loopback.
Изменяющие/асинхронные запросы привязаны к instance_id, request_id и абсолютному deadline<=120s. Идентичный retry возвращает существующую квитанцию; тот же ID с другими аргументами отклоняется. Очередь ограничена 1024 receipts; просроченная queued работа не исполняется. read_analytics также использует envelope, но не требует разрешения на изменение.
Изменения требуют mcp_allow_edits и повторной проверки в момент исполнения; пользовательские несохранённые черновики блокируют конфликтующие edits/PAPER. Временные HTTP-неопределённости не трактуются как успех.
PAPER повторно проверяет input revisions/identity/listing, policy/account epoch, срок расчётного окна, DQ и явно разрешённый исторический контекст, закрытие/валидность OHLCV, EUR, свежесть цены<=900s, открытую сессию, наличие средств/позиции, deadline. Комиссия/спред по существующей политике; транзакция и persistent request id исключают дублирование. Нельзя выдать «все акции торгуются» только по инструментальному allowlist.
Read-page lease максимум120s/64MiB/16entries относится только к транспортной публикации; не продлевает ANALYSIS/SIGNAL/TRADE READY. Секретные настройки не входят в persisted allowlist.
Совместимые read-only snapshot aliases: /@catalog, /@persisted, /@freshness, /@receipt/{instance_id}/{command_id}. Это не новый мост и не повторная отправка операции.

НЕ ПУБЛИКОВАТЬ
СМ/Связь/private/runtime-key.txt; оба profiles/stock-monitor-managed*.yaml (личные tunnel IDs/пути/подключение); generated test-data/bridge.json (bearer/instance/local address); bridge-command-audit.jsonl, runtime locks, пользовательские SQLite/WAL/SHM, stock_monitor.log, snapshots/receipts/financial data. Не публиковать tunnel-client.exe/cloudflared.exe или личные shortcut/config profiles; при необходимости дать инструкцию официальной установки и обезличенный example.
work2-support-20261028 содержит личные пути, экземпляры, screenshots, живую финансовую диагностику и backup manifests. Использовать для хронологии, не копировать целиком в публичный репозиторий. Личный config.json требует отдельной sanitization-проверки root agent; значения в этом аудите не печатались.

ДОСТУПНЫЕ ИЗОЛИРОВАННЫЕ ТЕСТЫ
test_mcp_instance.py —16: instance/deadline/idempotency/permission/expiry/capacity/safe audit/authenticated local fake bridge/schema. Использует свои temp dirs и loopback server, не живой bridge.
test_mcp_readback.py —4: selection invalidation, pending/current readback, одна UI timer/worker, temp bridge.
test_mcp_diagnostics.py —5: причины DQ/TF, no legacy false signal/no mutations, pipeline stage/sanitized errors, temp bridge/mocks.
test_store_stale_bridge.py —2: ложный занятый порт и настоящий duplicate-instance; temp fixtures.
test_snapshot_interleaved.py —6: изолированный temp Store, interleaved snapshots, targeted repair/bounded retries/current revision, no providers.
test_snapshot_error_stage.py —2: bounded diagnostic location, no local secret/path leakage.
test_ui_snapshot_cost.py —15 (смежный performance набор): reserve/fairness, deferred card/news, bounded snapshot data, dependency key/partial indicators. Перед запуском root должен проверить целый файл на sandbox-пути.
Для storage policy направлять tempfile в существующий СМ-Архив и PYTHONDONTWRITEBYTECODE=1; некоторые тесты явно используют dir=Path(__file__).parent, поэтому запускать из контролируемого исходного checkout, а не переносить live data.
Исторический Work2 report 28Oct фиксирует86 combined+17 transport/revision+3 errors и12 installer/migration PASS, но группы пересекаются. Это сохранённое доказательство, не количество вновь проведённых тестов. Полный API transport tests из review не следует считать присутствующими в root 0.16, если не перенесены отдельно.

ИЗВЕСТНЫЕ ОГРАНИЧЕНИЯ
1. Крупная секция CP может превысить64MiB и вернуть PAGE_SESSION_TOO_LARGE ещё до JSON pointer expansion. Это подтверждено checkpoint10Nov; nested paging само по себе не устраняет initial lease size. Во время этой ревизии не исправлялось.
2. Старый клиент ChatGPT может иметь кешированную неполную schema; новые fields/catalog/revision tool подтверждены исходниками, но доступ всех команд конкретного внешнего клиента требует отдельного подтверждения. /@ aliases смягчают различие доступных read schemas.
3. Смена/expiry input revisions честно отзывают допуск, даже если публикация snapshot/receipt ещё доступна. Транспортная AVAILABLE/COMPLETED не равна ANALYSIS/TRADE READY.
4. Старые command receipts после restart не все сохраняются; фактически выполненные PAPER fills читаются устойчиво. NOT_FOUND не разрешает автоматический повтор сделки.
5. V2 и независимые shadow books пока не имеют отдельного MCP API; доступны локальному UI/своей SQLite. Не описывать основной paper_trade как управление V2.
6. Старая фраза five approved stocks в tool description не соответствует текущему universe; отдельная документационная проблема, исходники не правились.
7. Обычный source launch переносим через app.py; официальный туннель требует собственного законного подключения/секрета на целевой машине. Из публикации локальных secret profiles это не восстанавливать.

ХРОНОЛОГИЯ ПО СОХРАНЁННЫМ СВИДЕТЕЛЬСТВАМ
28Oct: Work2 интеграция 37 уникальных файлов, API2/16tools, catalog/archive/provenance/chart export, revision diagnostics, paging/прочтение ошибок; Windows и cloud tests отмечены с конкретными ограничениями. Источник work2-support-20261028/LIVE016_INTEGRATION_REPORT_RU.txt.
После28Oct: совместимые snapshot read aliases и pinned read leases позволяют доступ через старую schema, latest publication/freshness явны. Точная дата каждого изменения по текущему файлу без Git не доказана.
03Nov: физический перенос в постоянное СМ; не использовать старые пути из исторических инструкций rollback.
10Nov: диагностический snapshot остаётся долгим (~5мин), CP detail PAGE_SESSION_TOO_LARGE остаётся известным ограничением; новая V2 добавлена без изменения Bridge/MCP. Новая GitHub ревизия не должна считать эти ограничения устранёнными.

ДОПОЛНЕНИЕ: ТОЧНЫЙ ЗАПУСК / РЕАЛЬНАЯ SCHEMA ПОДКЛЮЧЁННОГО КЛИЕНТА
STDIO launcher example: command="C:/Path/To/Python/python.exe", args=["C:/Path/To/StockMonitor/stock_monitor/mcp_server.py"]. Это форма настройки клиента, не новый универсальный формат config.json. cwd произвольный, PYTHONPATH не требуется. Сервер не требует API-key env или bridge-url env, он читает generated test-data/bridge.json от открытой программы. paths.py development сам задаёт STOCK_MONITOR_DATA_DIR и игнорирует внешний override этой переменной; для отдельной копии программы используется отдельный sibling test-data. Стандартный Python с Tk не требует личных TCL_LIBRARY/TK_LIBRARY, необходимых существующему локальному окружению.
Безопасный пример сохранённой настройки chatgpt_tunnel: {"enabled":false,"directory":"C:/StockMonitorConnection","profile":"stock-monitor-managed","alias":"stock-monitor"}. Это только конфигурация Stock Monitor для ранее настроенного официального runtime, а не готовая авторизация. private API key, tunnel_id и официальный profile остаются локальными, secret хранится по file: reference. Не создавать enabled:true без действующего собственного профиля.
10Nov при просмотре доступной tools metadata текущего ChatGPT plugin найдено15 команд: все перечисленные кроме list_instruments. READ_ONLY надпись в общем описании plugin не доказывает запрета mutations: metadata также содержит add/remove/update/record/review/refresh/select/set_levels/paper_trade. Фактическое право исполнения не проверялось, ни одна команда plugin этим аудитом не вызывалась.
Доказан schema lag: remote read_analytics не принимает view, closed_only, include_chart_series, storage_table, хотя локальный сервер поддерживает; remote snapshot принимает только snapshot/runtime/candlepilot_health. Через read-only /@catalog, /@persisted, /@freshness доступны предусмотренные совместимые read routes. Этот список различает реализованный protocol от фактически выставленных в клиенте параметров. Обновлять внешнюю интеграцию или схему в этом подзадании не поручено.
