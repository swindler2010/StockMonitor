# Все акции: сохранённые ошибки CandlePilot

Снимок: 2026-11-22T08:25:49.601647+01:00. 59 акций, индексы исключены. Только существующие отчёты и квитанции в одной read-only транзакции; это не новый полный аудит OHLCV/индикаторов и не подтверждение торговой готовности.

Действующих отчётов gettex/gettex_chart GETTEX/EUR: 285.

## Классы состояний

| Источник | Состояние | Серий |
|---|---|---:|
| gettex | INVALID | 6 |
| gettex | UNCONFIRMED | 108 |
| gettex_chart | GAP | 26 |
| gettex_chart | READY | 3 |
| gettex_chart | SOURCE_UNAVAILABLE | 26 |
| gettex_chart | UNCONFIRMED | 116 |

## Все 59 акций

Количество диапазонов не равно числу отсутствующих свечей. «Другие» — сохранённые отчёты резервных поставщиков/площадок, не молчаливая замена GETTEX. «Без identity» требуют доказательства происхождения, а не автоматического переименования. До20.10 — диапазоны официального источника до первой даты локального файлового кэша; это ограничение доступного кэша, не утверждение о полном архиве биржи.

| Акция / ISIN | Официальный GETTEX | GETTEX chart | Другие / без identity | Диапазоны до20.10 | Квитанции восстановления |
|---|---|---|---:|---:|---|
| Apple / US0378331005 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 1m:UNCONFIRMED; 5m:UNCONFIRMED | 9 / 0 | 386 | BLOCKED×14; COMPLETED×4; SUPERSEDED×2; WAITING |
| NVIDIA Corporation / US67066G1040 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 5 / 0 | 58 | BLOCKED×16; COMPLETED; WAITING |
| AMAZON.COM INC / US0231351067 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 0 | BLOCKED×9; COMPLETED; WAITING |
| Meta Platforms Inc. Class A / US30303M1027 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 5 / 0 | 196 | BLOCKED×13; WAITING |
| Broadcom Inc. / US11135F1012 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 5 / 0 | 365 | BLOCKED×10; COMPLETED×2; SUPERSEDED; WAITING |
| PALANTIR TECHNOLOGIES INC-A / US69608A1088 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:READY; 5m:UNCONFIRMED | 9 / 0 | 168 | BLOCKED×10; COMPLETED×3; SUPERSEDED×3; WAITING |
| ADOBE INC / US00724F1012 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 277 | BLOCKED×10; COMPLETED×3; WAITING |
| ADVANCED MICRO DEVICES / US0079031078 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 143 | BLOCKED×8; COMPLETED×3; WAITING |
| AIRBUS SE / NL0000235190 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 242 | BLOCKED×11; COMPLETED; WAITING |
| ALLIANZ SE-REG / DE0008404005 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 187 | BLOCKED×11; COMPLETED×2; WAITING |
| ASML HOLDING NV / NL0010273215 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 107 | BLOCKED×18; COMPLETED; SUPERSEDED; WAITING |
| AbbVie / US00287Y1091 | 15m:INVALID; 5m:INVALID | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×22; COMPLETED; WAITING |
| Alphabet Inc. / US02079K1079 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 5 / 0 | 543 | BLOCKED×13; COMPLETED; SUPERSEDED; WAITING |
| BASF SE / DE000BASF111 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 265 | BLOCKED×11; COMPLETED×2; WAITING |
| BAYER AG-REG / DE000BAY0017 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 275 | BLOCKED×9; WAITING |
| BAYERISCHE MOTOREN WERKE AG / DE0005190003 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 231 | BLOCKED×14; COMPLETED×2; WAITING |
| Berkshire Hathaway B / US0846707026 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×12; COMPLETED; WAITING |
| CANCOM SE / DE0005419105 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 318 | BLOCKED×10; COMPLETED×4; WAITING |
| COCA-COLA CO/THE / US1912161007 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×4; WAITING |
| CONTINENTAL AG / DE0005439004 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 309 | BLOCKED×11; COMPLETED×3; WAITING |
| DEUTSCHE BANK AG-REGISTERED / DE0005140008 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 186 | BLOCKED×14; COMPLETED×3; WAITING |
| DEUTSCHE BOERSE AG / DE0005810055 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 320 | BLOCKED×7; COMPLETED×3; WAITING |
| DEUTSCHE TELEKOM AG-REG / DE0005557508 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 141 | BLOCKED×9; COMPLETED×2; SUPERSEDED; WAITING |
| DHL AG / DE0005552004 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 9 / 0 | 283 | BLOCKED×10; COMPLETED×2; WAITING |
| Duke Energy / US26441C2044 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×27; COMPLETED×3; WAITING |
| E.ON SE / DE000ENAG999 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 286 | BLOCKED×10; COMPLETED×3; SUPERSEDED; WAITING |
| Exxon Mobil / US30231G1022 | нет сохранённого отчёта | нет сохранённого отчёта | 0 / 2 | 0 | нет сохранённого отчёта |
| GENERAL MOTORS CO / US37045V1008 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 322 | BLOCKED×8; COMPLETED×3; WAITING |
| HENSOLDT AG / DE000HAG0005 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 5 / 0 | 258 | BLOCKED×14; COMPLETED; WAITING |
| Infineon Technologies AG / DE0006231004 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 5 / 0 | 124 | BLOCKED×23; WAITING |
| Johnson & Johnson / US4781601046 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×16; COMPLETED×2; SUPERSEDED×2; WAITING |
| L'OREAL / FR0000120321 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 316 | BLOCKED×11; COMPLETED×2; WAITING |
| LVMH MOET HENNESSY LOUIS VUI / FR0000121014 | нет сохранённого отчёта | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 2 | 0 | BLOCKED×8; COMPLETED×3; WAITING |
| MERCEDES-BENZ GROUP AG / DE0007100000 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 196 | BLOCKED×10; COMPLETED×2; WAITING |
| MICHELIN (CGDE) / FR001400AJ45 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 320 | BLOCKED×13; COMPLETED; WAITING |
| MICRON TECHNOLOGY INC / US5951121038 | 15m:INVALID; 5m:INVALID | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×15; SUPERSEDED; WAITING |
| MICROSOFT CORP / US5949181045 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 151 | BLOCKED×27; WAITING |
| MUENCHENER RUECKVER AG-REG / DE0008430026 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 205 | BLOCKED×9; COMPLETED; WAITING |
| Merck & Co. / US58933Y1055 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×13; COMPLETED×4; SUPERSEDED |
| NIKE INC -CL B / US6541061031 | 15m:INVALID; 5m:INVALID | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 133 | BLOCKED×12; WAITING |
| Newmont / US6516391066 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 0 / 0 | 0 | BLOCKED×12; COMPLETED×7; WAITING |
| Nordex SE / DE000A0D6554 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 6 / 0 | 575 | BLOCKED×12; COMPLETED×2; SUPERSEDED; WAITING |
| Northern Star Resources Limited / AU000000NST8 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 3 / 2 | 685 | BLOCKED×9; COMPLETED×2; WAITING |
| Novartis AG / CH0012005267 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 11 / 2 | 662 | BLOCKED×6; COMPLETED; WAITING |
| Novo Nordisk A/S / DK0062498333 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 11 / 2 | 459 | BLOCKED×13; COMPLETED×2; WAITING |
| Oracle Corporation / US68389X1054 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 5 / 0 | 270 | BLOCKED×9; COMPLETED; WAITING |
| Procter & Gamble / US7427181091 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 8 / 0 | 49 | BLOCKED×9; COMPLETED; WAITING |
| RHEINMETALL AG / DE0007030009 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 71 | BLOCKED×19; COMPLETED×5; SUPERSEDED; WAITING |
| RWE AG / DE0007037129 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 266 | BLOCKED×10; COMPLETED×2; WAITING |
| SALESFORCE INC / US79466L3024 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 270 | BLOCKED×5; COMPLETED×2; WAITING |
| SAP SE / DE0007164600 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 95 | BLOCKED×11; WAITING |
| SCHNEIDER ELECTRIC SE / FR0000121972 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 259 | BLOCKED×10; COMPLETED×5; WAITING |
| SIEMENS ENERGY AG / DE000ENER6Y0 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:READY; 5m:UNCONFIRMED | 9 / 0 | 46 | BLOCKED×20; COMPLETED; SUPERSEDED; WAITING |
| SWISS RE AG / CH0126881561 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 310 | BLOCKED×12; COMPLETED; WAITING |
| Siemens AG / DE0007236101 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 9 / 0 | 224 | BLOCKED×15; COMPLETED×3; WAITING |
| Space Exploration Technologies Corp. / US84615Q1031 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:READY; 5m:UNCONFIRMED | 5 / 0 | 93 | BLOCKED×9; COMPLETED; WAITING |
| Tesla / US88160R1014 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 9 / 0 | 352 | BLOCKED×29; COMPLETED×3; SUPERSEDED; WAITING |
| Walmart Inc. / US9311421039 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:GAP; 5m:UNCONFIRMED | 13 / 0 | 601 | BLOCKED×12; COMPLETED; WAITING |
| thyssenkrupp AG / DE0007500001 | 15m:UNCONFIRMED; 5m:UNCONFIRMED | 15m:UNCONFIRMED; 1D:SOURCE_UNAVAILABLE; 5m:UNCONFIRMED | 5 / 0 | 574 | BLOCKED×13; COMPLETED×2; WAITING |

## Что исправлено и что остаётся

Выявлен и воспроизведён общий программный дефект: terminal-запрос не замечал появления младших свечей в том же диапазоне. Реализован отдельный fingerprint восстановления, не меняющий отпечатки сверки TV. Четыре новых и два затронутых сценария опубликованных Git-объектов c8d7c23b прошли:6 PASS /1,518с; включая все11 UI TF через их базовые ряды. Старые terminal-квитанции получают baseline без сброса попыток/сроков. Новые совместимые дети разрешают повтор, посторонние источники и даты — нет.

В локальном воспроизведении на сохранённых реальных 5m GETTEX восстановлена одна15m-свеча без HTTP и пользовательской кнопки. Это изолированная проверка, не новая рабочая свеча. В текущих сохранённых BLOCKED одноинтервальных15m-запросах не найдено полного тройного5m-набора при отсутствующем родителе, поэтому массовое рабочее восстановление этим тестом не обещается.

Внешние препятствия: недостаточная глубина файлов/отсутствие полного покрытия; пустые ответы; некорректные значения самого провайдера; неподтверждённое происхождение старых отчётов. Пустой ответ не NO_TRADES. DQ/допуски/позиции/история ради удаления сообщений не меняются. Все ошибки всех акций НЕ объявлены исправленными.

Подробные квитанции и адресные примеры сохраняются локально; личные базы и счёт не публикуются.


## Установка и предел проверки

Код a02a1a6d + тест c8d7c23b установлен в Windows TEST обычным Close/Restart.
Наблюдался прежний Meta/4H/6месяцев, CP ACTIVE, Bridge TEST/running. Это не
подтверждение торгового допуска. Положительное появление новых детей после
terminal-запроса проверено на локальном сохранённом GETTEX-фрагменте в изолированной
SQLite. В момент проверки в рабочей очереди не найдено ни одного одноинтервального
BLOCKED15m с полным5m-набором и отсутствующим родителем. Поэтому новых рабочих
свечей, восстановленных именно данным исправлением, подтверждено0. Естественное
поступление таких новых детей остаётся отдельной будущей проверкой.

Холодное чтение детского отпечатка рабочего15m:среднее62,932мс/максимум175,242мс;
1D:11,216/11,766мс (по3 запуска), вне Tk. Это не общий performance PASS.
Сохраняются DQ, identity, финальные ревизии, provider cooldown и торговый риск.
Старые коды/сроки не сбрасываются при внедрении без новых данных.

Официальные INVALID6 серий у3 акций сохранены: публикации противоречат подтверждённой
остановке26.10, а не просто отсутствует ряд. Объявление биржи проверено по
[первичному документу, страница148](https://www.gettex.de/fileadmin/bulletin/bekanntmachungen.gtx.20261110.pdf#page=148).
Данные/объявление нельзя молча исправлять друг под друга без доказательства.
Существующие графики и исследования TradingView Work2 не затрагивались; массовая
визуальная сверка всех проблемных интервалов59 акций в этом этапе не проведена.

Весь запрос «исправить все ошибки всех акций» НЕ DONE: остаётся отсутствие
подтверждённых исторических данных, неполные файлы, исходные ошибки провайдера
и неустановленная identity. Отчёт фиксирует текущий охват и одно установленное
общее исправление, не выдаёт устранение программного дефекта за исчезновение
всех фактических сообщений. Проверенные прежние свечи/разрешения не менялись.


## Второе исправление: причина недоступности файлов

В7a223ae8 при отсутствии подходящих по времени опубликованных/сохранённых файлов
исторического запроса указывается SOURCE_LIMITED, без утверждения «нет EUR-сделок».
При действительно прочитанном файле без строк ISIN сохраняется отдельный результат.
Ни один вариант не является NO_TRADES без прежней проверки полного покрытия.
Дополнительные2 сценария прошли; итог8 PASS опубликованных объектов/1,507с.
Данные/разрешения/бюджет запросов/прошлые квитанции не переписываются.
Код установлен повторным обычным запуском; выбранный график Meta сохранён.



## 22.11.2026 — визуальная сверка TV и реестр принятых интервалов (IN_PROGRESS)

Общий запрос «все ошибки всех акций» не завершён. Реально открыты 59 карточек поиска GETTEX в установленном TradingView; по 58 акциям сохранены 66 новых адресных наблюдений. У Exxon проверка остановлена на identity: сохранён US30231G1022, в TV доступен преемник US30233Q1085 / 7DZ; взаимозаменять их без штатного перехода нельзя. Это не сверка всех дат/всех TF каждой акции.

Снимок 22.11.2026 12:22:05 CET: ABSENT 51, ZERO_VOLUME 8, PRESENT 4, смешанные UNCONFIRMED 3. Последние три родительских диапазона разделены на шесть точных дочерних наблюдений, поэтому эти числа нельзя складывать как количество отдельных свечей. По новым наблюдениям очередь довела 16 записей до RESOLVED_NATIVE, 50 до AWAITING_SOURCE; оставшихся RECHECK_QUEUED на снимке нет. В сохранённой политике найдены ссылки на 35 новых наблюдений; это не 35 новых свечей и не общее торговое разрешение.

Сам CandlePilot, без ручной правки OHLCV, применил независимое официальное подтверждение: General Motors 13.11 16:15→16:00, L’Oréal 13.11 18:15→18:00, Airbus 13.11 19:45→19:30 (Berlin). В каждой квитанции moved 2: по одной 5m и 15m записи. Итого 6 перемещённых временных меток в трёх случаях, added=0, changed=0; это не шесть новых исторических свечей. Сохранены original/before, официальная причина и ревизии. 13 прочих новых исходов RESOLVED_NATIVE подтверждены как пустые полными GETTEX-публикациями, а не самим отсутствием в TV.

### Реестр и активные ошибки

Исправлен общий дефект представления: действующая OWNER_ACCEPTED_TV_ABSENCE показывалась рядом со старой BLOCKED/WAITING квитанцией как неисправленная проблема. Теперь эффективное подтверждение по текущим ревизиям, identity, TF и точному диапазону выводится в «Принятые сверки TV», а не в активные ошибки. Запись по акции остаётся в общем SQLite-реестре CP с доказательствами и датой. Старый DQ не удаляется и не превращается в READY. При отзыве подтверждения/изменении значимых данных проблема возвращается в активный список. INVALID и обычный ручной риск этой маской не скрываются.

Код f4481196f7f3ac2d030025007a1948bb532d1b79 в draft PR #21. Три адресных сценария PASS /1,324 с: старый отрицательный receipt против действующей сверки, исчезновение только из активного списка с сохранением реестра и повторное появление при отзыве, смена акции/закрытие окна против запоздалого ответа. Проверены исходные свечи, read-only режим и отсутствие записи из представления. ru/en/de строки добавлены; прежние общие тесты/аудиты не повторялись.

Windows: обычные Save/Close/Restart, сохранён Meta/4H/6месяцев. В окне СР проверено раздельное отображение: Meta 1304 активных диапазона и 105 ранее и вновь принятых сверок TV; в принятом разделе — зелёная «Принято по TV», дата, источник, причина и исходный DQ. Кнопка не ставит уже принятый диапазон заново. Это диагностические диапазоны, не 1304 плохие свечи. Все 8 защищённых хешей счёта/исполнений/индикаторов/epoch/theme/выбора/рисунков совпали. MCP подтвердил TEST/Bridge running; health не выдаётся за DQ или торговую готовность, возраст снимка на проверке 277 с.

### Подтверждённые ограничения

- TV Basic показывает максимум 5000 баров: запрос NVIDIA 5m за 07.10 фактически упёрся в 12.10. Отсутствие доступа не записано как отсутствие свечи. Ограничение двух одновременных подключений также наблюдалось; Work2 не отключали и платный доступ не оформляли.
- Для Adobe/Procter & Gamble 20.11 в 09:00 TV показывает реальные сделки, а соответствующий официальный файл MUND обрывается в 08:05:51 UTC. Штатное приобретение конкретного 08:15 файла получало HTTP404, next retry 22.11 14:17:14 CET. Cooldown не сброшен, пустой файл не принят за NO_TRADES.
- Нулевая свеча TV не равна отсутствию свечи. Разница соседних OHLCV и округление экранного объёма не записаны как полное совпадение OHLCV. 6 серий AbbVie/Micron/Nike с EXCHANGE_NOTICE_CONFLICT остаются отдельными фактическими конфликтами.
- Старые 5m, широкие исторические диапазоны, неразрешённая identity Exxon и положительные расхождения не закрыты этим проходом. У каждой акции остаётся IN_PROGRESS, не «полностью проверена».

До изменения наблюдений создан SQLite backup с integrity_check=ok. Доказательства/скриншоты/нативные ссылки и scoped before/after хранятся в СМ-Архив; приватные SQLite, счёт, ключи и полные OHLCV не публикуются. Откат — только собственный патч cp_problem_view/UI translations/каталог, исходники before в СМ-Архив «CP TV accepted status ...»; не восстанавливать весь живой SQLite поверх новых feed/Work2 изменений. Старый вид может снова показать принятые диапазоны, но доказательства не теряются.

### Охват новых наблюдений по общему списку акций

Таблица показывает только новые наблюдения этого прохода, а не полноту исторической проверки. Числа «записано по политике» — ссылки в сохранённой политике; действительность каждого допуска повторно проверяет effective_accepted.

| Акция | Новых наблюдений | Результаты TV | Штатная обработка | Записано по политике TV |
|---|---:|---|---|---:|
| Tesla | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Alphabet Inc. | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Broadcom Inc. | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Infineon Technologies AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Apple | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Novartis AG | 1 | ZERO_VOLUME: 1 | AWAITING_SOURCE: 1 | 0 |
| thyssenkrupp AG | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| Northern Star Resources Limited | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Nordex SE | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| NVIDIA Corporation | 1 | PRESENT: 1 | AWAITING_SOURCE: 1 | 0 |
| Space Exploration Technologies Corp. | 1 | PRESENT: 1 | AWAITING_SOURCE: 1 | 0 |
| Novo Nordisk A/S | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Walmart Inc. | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Oracle Corporation | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| MICROSOFT CORP | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| SCHNEIDER ELECTRIC SE | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| BAYER AG-REG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| SAP SE | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| DEUTSCHE BOERSE AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| HENSOLDT AG | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| Meta Platforms Inc. Class A | 3 | ABSENT: 3 | AWAITING_SOURCE: 3 | 3 |
| Siemens AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| MICHELIN (CGDE) | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| GENERAL MOTORS CO | 3 | ABSENT: 1, UNCONFIRMED: 1, ZERO_VOLUME: 1 | AWAITING_SOURCE: 2, RESOLVED_NATIVE: 1 | 0 |
| SWISS RE AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| CANCOM SE | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| CONTINENTAL AG | 1 | ZERO_VOLUME: 1 | AWAITING_SOURCE: 1 | 0 |
| ASML HOLDING NV | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| RHEINMETALL AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| ALLIANZ SE-REG | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| SIEMENS ENERGY AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| AIRBUS SE | 3 | ABSENT: 1, UNCONFIRMED: 1, ZERO_VOLUME: 1 | AWAITING_SOURCE: 2, RESOLVED_NATIVE: 1 | 0 |
| DEUTSCHE TELEKOM AG-REG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| MUENCHENER RUECKVER AG-REG | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| DEUTSCHE BANK AG-REGISTERED | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| BASF SE | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| RWE AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| E.ON SE | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| MERCEDES-BENZ GROUP AG | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| ADVANCED MICRO DEVICES | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| PALANTIR TECHNOLOGIES INC-A | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| ADOBE INC | 1 | PRESENT: 1 | AWAITING_SOURCE: 1 | 0 |
| SALESFORCE INC | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| LVMH MOET HENNESSY LOUIS VUI | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| L'OREAL | 3 | ABSENT: 1, UNCONFIRMED: 1, ZERO_VOLUME: 1 | AWAITING_SOURCE: 2, RESOLVED_NATIVE: 1 | 0 |
| BAYERISCHE MOTOREN WERKE AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| DHL AG | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| NIKE INC -CL B | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| AMAZON.COM INC | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Johnson & Johnson | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Procter & Gamble | 1 | PRESENT: 1 | AWAITING_SOURCE: 1 | 0 |
| Berkshire Hathaway B | 1 | ABSENT: 1 | RESOLVED_NATIVE: 1 | 0 |
| Newmont | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| Exxon Mobil | 0 | ISIN не совпал с доступным листингом | identity blocked | 0 |
| Duke Energy | 1 | ZERO_VOLUME: 1 | AWAITING_SOURCE: 1 | 0 |
| Merck & Co. | 1 | ZERO_VOLUME: 1 | AWAITING_SOURCE: 1 | 0 |
| AbbVie | 1 | ZERO_VOLUME: 1 | AWAITING_SOURCE: 1 | 0 |
| MICRON TECHNOLOGY INC | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |
| COCA-COLA CO/THE | 1 | ABSENT: 1 | AWAITING_SOURCE: 1 | 1 |


## 22.11.2026 — Work: ложные активные сообщения по уже подтверждённому GETTEX

**Было:**108 183 активных сохранённых диагностических диапазона/сообщения по59 акциям (пакет чтения12:39:45–12:40:32CET). **Осталось в следующем пакете:**107 031 (12:55:56–12:57:48CET). Это строки действующего окна СР с его фильтрами, не количество испорченных свечей, DQ-серий или торговых запретов; времена для каждой акции сохранены отдельно.

**Что исправил именно Work:**одну общую программную причину, из-за которой903 уже подтверждённых GETTEX-интервала продолжали отображаться активными ошибками. Для каждого из903 receipt существовал до исходного чтения этой акции и в новом чтении прошёл штатный текущий валидатор; ни одного не было в уже принятых TV-рисках. Это исправление ложной диагностики, не903 новых/исправленных OHLCV. Прямых изменений свечей Work:0.

**Ручная новая работа Work с TV:**6 проверенных15m-пропусков, которые после наблюдения оформил штатный CandlePilot по действующей политике: Meta16.11 12:15;17.11 19:45 и20:45;18.11 07:45; Broadcom28.10 22:15 и29.10 19:15 (2026,Europe/Berlin). Все6 наблюдений вошли через guarded API, без свечных значений/новых разрешений. При последующем чтении эффективная политика приняла эти конкретные отсутствия; это не самопроизвольная сверка Desktop силами CP. Итого909 адресных диагностических результатов связано с новой правкой/ручной сверкой Work, из них903 — снятые ложные сообщения и6 — исторические пропуски после сверки. Программную причину не следует дополнительно складывать как ещё одну свечную ошибку.

**СР отдельно:**полная разница между пакетами1152 не приписывается Work. Оставшиеся243 нетто относятся к другим изменениям штатных отчётов/СР во время чтения; это не доказанное число автономно исправленных свечей. В конце904 действующих официальных подтверждения:903 были ранее,1 поступило после исходного чтения. Данных для точного общего числа новых самостоятельных исправлений CP этим проходом не собирали; ноль либо243 вместо неизвестного не подставляются.

### Причина и адресное исправление

Воспроизведено на Apple10.11 22:45 и Broadcom16.11 22:45: старый DQ сохранял UNCONFIRMED, хотя существующий _official_empty_problem_current с актуальными зависимостями возвращал true и штатный job был RESOLVED_NATIVE/NO_TRADES. Окно СР читало только старый DQ/repair receipt, не действующее официальное подтверждение.

cp_problem_view теперь использует тот же существующий валидатор: exactISIN/identity/15m/одиночный диапазон, APPLIED VERIFIED_NATIVE_EMPTY, текущая политика, доказательство полного парного покрытия и соответствие реальных рядов. Одного текста RESOLVED_NATIVE недостаточно. Подтверждение остаётся в общем реестре акции и выводится в «Подтверждено GETTEX», отдельно от активных ошибок и от принятых TV-рисков. При новых входах/отзыве политики/INVALID проблема остаётся либо возвращается. Сырые DQ, OHLCV, risk/admission/CORE и цена исполнения не меняются; чтение выполняется в прежнем фоновом read-задании, без нового writer/потоков и без сети.

Код e2ee9696c337e0df012c5b536d18edc61ff19059, PR21/draft. Проверено7 затронутых сценариев/2,295с, из них новый интеграционный сценарий с реальным официальным receipt в изолированной SQLite: до подтверждения active1; после active0/native1; отзыв/INVALID/новая реальная5m возвращают active1; чтение не меняет свечи, DQ, настройки или разрешения. Остальные6 проверок повторены потому, что менялся тот же view/paging/UI-код; общие прежние аудиты/тесты очереди не запускались.

Windows: обычные Save/Close/Restart; выбранный Meta/4H/6месяцев сохранён. Реально проверен раздел Siemens с4 официально подтверждёнными датами и подробностью первого интервала21.10 22:45; исходный UNCONFIRMED сохранён, следующий retry пуст, TV-чек не выдуман. Зелёная кнопка показывает причину и не создаёт повторный repair. Все8 защищённых хешей счёта/исполнений/индикаторов/epoch/theme/выбора/рисунков совпали. Bridge TEST/running; возраст снимка371с отдельно, это не признак торговой готовности. Чтение пакета59 сохранённых диагностик заняло46,53с до и112,37с после добавления проверок подтверждения, вне Tk; общего performance PASS нет.

### Отдельные несовпадения и ограничения

У Meta обнаружены два отличающихся соседних15m-бара:16.11 12:30 и17.11 20:30. Они сохранены как отдельные DIFFERENT-наблюдения с native-reference и скриншотами через штатную очередь, не объявлены исправленными вместе с соседними отсутствиями. Полные OHLCV и private SQLite остаются локально. Историческое принятие пропуска не подтверждает соседние цены/объёмы.

Полная задача всех акций/всех интервалов остаётся IN_PROGRESS. Доступное Desktop-подключение восстановлено владельцем; Basic5000 баров для старых5m, неполный20.11 MUND/его штатный retry и identity-преемник Exxon остаются ограничениями. Work2, Store0.15, позиции и торговая стратегия не менялись, искусственных сделок/свечей нет. Новые исправления не считаются полным допуском ко всем инструментам.

Доказательства: существующий СМ-Архив/CP TV current pass baseline.json, after.json, CP native resolution attribution.json, CP native resolved Siemens Windows.png и адресные screenshots/native-references. Откат только own diff/файлы CP native resolution ... rollback, без полной замены SQLite. Журнал предыдущего неправильного понимания Bayer сохранён с явной отменой: в СМ он восстановлен, только строка BAYN удалена из TV по уточнённому поручению.

### Сравнение того же списка акций

Числа в последнем столбце — результат ручной сверки Work с последующим штатным оформлением CP; не автономная сверка TV со стороны CP. Другие изменения пакетов не приписываются Work.

| Акция | Активных до | Активных после | Ложных сообщений снято правкой Work | Пропусков принято после новой TV-сверки Work |
|---|---:|---:|---:|---:|
| Tesla | 1598 | 1598 | 0 | 0 |
| Alphabet Inc. | 2182 | 2181 | 1 | 0 |
| Broadcom Inc. | 1478 | 1475 | 1 | 2 |
| Infineon Technologies AG | 487 | 487 | 0 | 0 |
| Apple | 2315 | 2314 | 1 | 0 |
| Novartis AG | 3282 | 3244 | 38 | 0 |
| thyssenkrupp AG | 2491 | 2478 | 13 | 0 |
| Northern Star Resources Limited | 3444 | 3359 | 85 | 0 |
| Nordex SE | 2539 | 2509 | 30 | 0 |
| NVIDIA Corporation | 165 | 165 | 0 | 0 |
| Space Exploration Technologies Corp. | 336 | 336 | 0 | 0 |
| Novo Nordisk A/S | 1882 | 1877 | 5 | 0 |
| Walmart Inc. | 2810 | 2797 | 13 | 0 |
| Oracle Corporation | 1410 | 1409 | 1 | 0 |
| MICROSOFT CORP | 1056 | 1055 | 1 | 0 |
| SCHNEIDER ELECTRIC SE | 2474 | 2471 | 3 | 0 |
| SAP SE | 797 | 795 | 2 | 0 |
| DEUTSCHE BOERSE AG | 2740 | 2729 | 11 | 0 |
| HENSOLDT AG | 2115 | 2102 | 13 | 0 |
| Meta Platforms Inc. Class A | 1304 | 1300 | 0 | 4 |
| Siemens AG | 1618 | 1614 | 4 | 0 |
| MICHELIN (CGDE) | 3323 | 3214 | 109 | 0 |
| GENERAL MOTORS CO | 3061 | 2813 | 85 | 0 |
| SWISS RE AG | 3100 | 3033 | 67 | 0 |
| CANCOM SE | 2985 | 2920 | 65 | 0 |
| CONTINENTAL AG | 2844 | 2807 | 36 | 0 |
| ASML HOLDING NV | 1168 | 1168 | 0 | 0 |
| RHEINMETALL AG | 539 | 539 | 0 | 0 |
| ALLIANZ SE-REG | 1355 | 1354 | 1 | 0 |
| SIEMENS ENERGY AG | 333 | 333 | 0 | 0 |
| AIRBUS SE | 2190 | 2179 | 11 | 0 |
| DEUTSCHE TELEKOM AG-REG | 1168 | 1165 | 3 | 0 |
| MUENCHENER RUECKVER AG-REG | 1631 | 1627 | 4 | 0 |
| DEUTSCHE BANK AG-REGISTERED | 1385 | 1382 | 3 | 0 |
| BASF SE | 2002 | 1995 | 7 | 0 |
| RWE AG | 2260 | 2248 | 12 | 0 |
| E.ON SE | 2226 | 2216 | 10 | 0 |
| MERCEDES-BENZ GROUP AG | 1634 | 1634 | 0 | 0 |
| ADVANCED MICRO DEVICES | 931 | 930 | 1 | 0 |
| PALANTIR TECHNOLOGIES INC-A | 1523 | 1523 | 0 | 0 |
| ADOBE INC | 2322 | 2314 | 8 | 0 |
| SALESFORCE INC | 2146 | 2140 | 6 | 0 |
| LVMH MOET HENNESSY LOUIS VUI | 1586 | 1582 | 4 | 0 |
| L'OREAL | 2726 | 2892 | 49 | 0 |
| BAYERISCHE MOTOREN WERKE AG | 1742 | 1733 | 9 | 0 |
| DHL AG | 2491 | 2477 | 14 | 0 |
| NIKE INC -CL B | 1641 | 1639 | 2 | 0 |
| AMAZON.COM INC | 544 | 544 | 0 | 0 |
| Johnson & Johnson | 2262 | 2255 | 6 | 0 |
| Procter & Gamble | 2043 | 2034 | 9 | 0 |
| Berkshire Hathaway B | 1860 | 1857 | 3 | 0 |
| Newmont | 2123 | 2106 | 17 | 0 |
| Exxon Mobil | 13 | 13 | 0 | 0 |
| Duke Energy | 2741 | 2660 | 81 | 0 |
| Merck & Co. | 2434 | 2402 | 32 | 0 |
| AbbVie | 2492 | 2465 | 27 | 0 |
| MICRON TECHNOLOGY INC | 92 | 92 | 0 | 0 |
| COCA-COLA CO/THE | 1040 | 1040 | 0 | 0 |
| BAYER AG-REG | 1704 | 1411 | 0 | 0 |

Дополнение13:13CET: обе отдельные Meta-сверки приняты обычной очередью как SOURCE_DISCREPANCY/TV_DIFFERENCE, не как NATIVE_ROWS_CONFIRMED. Для17.11 20:30 штатный план ждёт RETIME_INCOMPLETE_PUBLICATION; для16.11 12:30 проверенный план нормализации отсутствует. Данные не менялись. Ещё один диапазон Broadcom26.10 07:30–09:30 вручную сверён с TV и передан RECHECK_QUEUED после окончания указанного пакета; он не прибавляется к уже зафиксированным6 закрытым диапазонам до штатной проверки.


<a id="work1-closing-tick-and-review-route-20261122"></a>
## 22.11.2026 — Work1: две реальные ошибки Palantir и маршрут сохранённой сверки

**Счёт именно этого подэтапа:** было 2 доказанных ценовых дефекта 15m Palantir GETTEX/EUR:23.10 17:30 и16.11 12:00 (Берлин). После общей доработки Work штатный CandlePilot исправил оба: осталось0 из этих2. Изменены4 существующие записи (по одной15m и одной связанной5m на дефект); добавлено0, перенесено0. Это2 уникальные ценовые ошибки, не4 независимые свечные проблемы. Прямых OHLCV-записей Work0. Work реализовал новый общий маршрут восстановления и исправил отдельный дефект назначения исторического хода; обычные прочие изменения СР к своим исправлениям не приписывает. Прежние903 ложных сообщения здесь повторно не считаются. Общий новый остаток всего расширившегося списка акций в этом подэтапе не пересчитывался.

### Доказанная причина и восстановление

В обеих свечах исходный ряд пропустил ровно последнюю опубликованную запись. Полные парные файлы GETTEX и их реальные записи дают те же OHLCV, что наблюдались в TradingView Desktop по GETTEX:PTX/EUR.23.10 последняя запись была с нулевым количеством,16.11 — с количеством1. Прежний алгоритм для сохранённого родного ряда не обрабатывал этот вариант при изменении Close либо Volume.

В существующий revision_watcher.retime_plan добавлен VERIFIED_NATIVE_CLOSING_TICK. Он требует текущий точный DIFFERENT, подтверждённую identity, закрытую единственную15m-свечу, обе действующие политики, полные публикации и точное воспроизведение ВСЕХ исходных5m/15m значений из официальной ленты после исключения только её последней записи. Последняя запись остаётся в прежнем5m-слоте; набор временных отметок не меняется. Произвольная замена данных не разрешена. Цены берутся из независимо проверенных официальных данных, не из текста или картинки TV. Штатный writer ещё раз строит тот же план перед записью, сохраняет исходные OHLCV/происхождение и запрос обычной переоценки. Отзыв исходной политики отзывает актуальность receipt.

Обычная очередь Windows TEST опубликовала23.10 17:30 как RESOLVED_NATIVE22.11 в14:21:03CET;16.11 12:00 — в14:19:34CET. Итоги после записи:23.10 O175.14/H175.14/L175.02/C175.02/V10;16.11 O151.36/H151.68/L151.22/C151.22/V14. Оба официальных подтверждения повторно актуальны. Это не утверждение полного торгового допуска Palantir.

### Отдельная причина задержки

Для одного ISIN используется прежний общий feed-ключ. Если он уже ожидал как короткая свежая загрузка, выбор due_observation не менял захваченный payload, а поздняя fresh-подстановка могла снова вытеснить историческую сверку. Новый адресный сценарий воспроизвёл именно потерю текущего назначения. Планировщик теперь передаёт ISIN/job_id и время сохранённого назначения; перед выполнением существующий worker использует прежний _review_only-маршрут для совпадающего запроса не старше90с. Другие акции, отсутствующий и просроченный запрос не изменяют свежую загрузку. Нет второго writer, отмены работающего задания, изменения стратегии или продления freshness. Свежая загрузка остаётся в обычной справедливой очереди.

### Проверка

Новый интеграционный сценарий последней записи с двумя вариантами (нулевое/положительное количество), отсутствующим подтверждением, отзывом политики, изменением исходного ряда перед публикацией, неизменностью соседних значений, идемпотентностью и повторным открытием базы: PASS /2.168с. Затронутый прежний сценарий extrema тоже PASS; весь архив/старые аудиты не запускались. Для маршрута очереди1новый сценарий с4вариантами и2затрoнутые проверки53/200 задач: PASS /0.042с. Дополнительная защита отображения нового действующего TV-конфликта над старым official-empty receipt также проверена одним сценарием; фактических таких текущих конфликтов в исходном замере0, новых исправленных свечей ей не приписывается.

Штатные Save/Close/Restart выполнены. Meta/4H/6месяцев сохранились; восемь проверок счёта/исполнений/индикаторов/epoch/theme/выбора/существующих рисунков и позиций совпали. Bridge нового TEST-экземпляра работает. Read-only MCP прочитал исправленную16.11 свечу, объём14 и конечные EMA10/30, RSI, MACD, ATR без missing_values. Получение результата заняло39.454с вычисления; это не PASS общей производительности. Для нового маршрута ожидается отдельный свежий ход с новым наблюдением21.10 14:15; пока не объявляется доказанным выигрышем во времени Windows.

### Остаток и ограничения

Вручную просмотрены дополнительные пропуски/различия Palantir через Desktop, а наблюдения записаны только guarded API. Три пропуска20.11 11:45,19.11 08:45,18.11 19:45 получили самостоятельное полное подтверждение GETTEX в обычной очереди; Broadcom26.10 07:30–09:30 принят действующей политикой по сохранённой TV-сверке. Они не являются новыми свечами. Другие соседние пропуски пока остаются в штатной перепроверке.

Не исправлены отдельное различие Palantir18.11 19:30 (V91 в TV против126 в родном и официальном ряду), смешанный участок22.10 19:15–19:45 (TV-пустота, существующая нулевая свеча и отличающийся следующий бар), новые21.10 14:00/14:15. Для22.10 официальный архив уже совпадает с TV; существующий консервативный механизм пока не обрабатывает сочетание задержанной временной привязки и пропущенной последней записи. Это ограничение реализации, а не отсутствие доказательств у источника. Meta16.11 12:30 и17.11 20:30 остаются отдельными неподтверждёнными расхождениями.

Обычная повторная попытка GETTEX20.11 закончилась22.11 в14:20:46CET причиной NO_PUBLISHED_OFFICIAL_FILE_TEMPLATE; следующая штатная попытка23.11 в14:20:46CET. Cooldown не сбрасывался. Лимит Basic5000 старых5m и identity-преемник Exxon сохраняются. Освобождённое владельцем подключение TV больше не записано активным препятствием. Ни пустой ответ поставщика, ни недоступные бары не объявлены NO_TRADES.

Код:[closing tick ef25e1a](https://github.com/swindler2010/StockMonitor/commit/ef25e1a20f1c0fe71e76ba0891220b701bebb5f7),[маршрут ec1d63a](https://github.com/swindler2010/StockMonitor/commit/ec1d63a9f6ab817343ea009cd4144f1ef6b94428),[защита TV-конфликта6df4aed](https://github.com/swindler2010/StockMonitor/commit/6df4aed17bbb0627b127ac6c6a4acb97421443cb),[PR21](https://github.com/swindler2010/StockMonitor/pull/21). Опубликованы только собственные изменения, тесты и каталог; другие локальные изменения desktop014 сохранены и не подменяются целым удалённым файлом. Детальные private native/TV доказательства и SQLite остаются в СМ-Архив. Доступен адресный исходный патч и rollback ровно четырёх исправленных строк с проверкой неизменности; dry-run подтвердил оба случая, фактический откат не выполнялся.

**Полная задача всех акций остаётся IN_PROGRESS.**
