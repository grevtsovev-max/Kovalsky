# Модули Kovalsky

- `core.py`, `workflow.py`: сбор, сохранение версий, передача прошедших первый фильтр материалов в редакцию.
- `keyword_filter.py`, `topic_registry.py`: тематический отбор по таблице.
- `edition/model.py`: группировка, написание и независимая проверка; единые правила и референсы, без инструментов интернет-поиска.
- `edition/store.py`: снимки, очередь, локальный индекс, ограниченный поиск и попытки.
- `edition/formatting.py`: форматирование Telegram, сворачиваемые подробности и измеримые ограничения.
- `edition/pipeline.py`: конечный процесс и одна доработка.
- `edition/publication.py`: проверка допуска и отправка через журнал доставки.
- `edition/worker.py`: редактор параллельно сбору и восстановление после перезапуска.
- `edition/views.py`, `dashboard.py`: исходники, версии, проверки, остановки и явный повтор.
- `agent_control.py`: постоянный запрет работы по команде владельца.

- `retrospective.py`: независимый ретроспективный аудит всех поступлений за период, включая отказы; неизменяемые отчёты, доказательства и путь до доставки. Кабинет → «Аудит»; команда `retrospective`.

- `source_reading.py`, `telegram_reader.py`: чтение статей и авторизованный транспорт Telegram; исходники сохраняются до редакции.
- `edition/semantic_filter.py`, `edition/freshness.py`: смысловой отбор и проверка давности до подготовки.
- `edition/short_release.py`, `edition/fragments.py`: короткий этап по подтверждённым фрагментам.
- `edition/layout_review.py`, `edition/layout_metrics.py`: безопасные правки оформления и их учёт.
- `edition/publication_history.py`: поиск опубликованных кандидатов для проверки повторов.
- `edition/source_translation.py`, `edition/language.py`: русский текст и проверка перевода резерва.
- `edition/corrections.py`: сохранённые уточнения владельца для новой редакции.
- `jobs/`, `vacancy_fields.py`, `vacancy_view.py`: независимая обработка вакансий и представление сохранённых объявлений.
- `filter_audit.py`, `post_metrics.py`: аудит первого фильтра и показатели публикаций.
- `cabinet_page.py`, `cabinet_server.py`, `cabinet_pipeline.py`: действующий кабинет; `dashboard.py` сохраняет публичные импорты страницы и запуска.
- `ops/check_release.py`: сравнение исходников выпуска с Git без чтения рабочих настроек и баз.
