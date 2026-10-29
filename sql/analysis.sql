-- =====================================================================
-- Анализ процесса обработки образцов (синтетические данные)
-- Таблицы: event_log (события) и cases (итоги по случаям)
-- СУБД: SQLite. Выполняй запросы по одному и записывай выводы.
-- Время ожидания считается в календарных часах (ночи и выходные входят).
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. Подготовка: представление ev (добавляет ожидание перед каждым действием)
--    Ожидание = начало действия минус конец предыдущего действия в том же случае
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS ev;
CREATE VIEW ev AS
SELECT
    case_id, activity, start_time, end_time, resource, study_type, priority,
    LAG(end_time) OVER (PARTITION BY case_id ORDER BY start_time, end_time) AS prev_end,
    (julianday(start_time) -
     julianday(LAG(end_time) OVER (PARTITION BY case_id ORDER BY start_time, end_time))) * 24 AS wait_hours
FROM event_log;

-- ---------------------------------------------------------------------
-- 1. Проверка загрузки данных (сколько строк и случаев)
-- ---------------------------------------------------------------------
SELECT COUNT(*) AS events, COUNT(DISTINCT case_id) AS cases FROM event_log;
SELECT status, COUNT(*) AS cases FROM cases GROUP BY status;

-- ---------------------------------------------------------------------
-- 2. Сроки выполнения (TAT) по выданным результатам: среднее, мин, макс, медиана, P90
-- ---------------------------------------------------------------------
WITH t AS (
    SELECT CAST(tat_days AS REAL) AS tat,
           ROW_NUMBER() OVER (ORDER BY CAST(tat_days AS REAL)) AS rn,
           COUNT(*) OVER () AS n
    FROM cases
    WHERE status = 'Выдан результат'
)
SELECT 'Среднее' AS metric, ROUND(AVG(tat), 1) AS days FROM t
UNION ALL SELECT 'Минимум', ROUND(MIN(tat), 1) FROM t
UNION ALL SELECT 'Максимум', ROUND(MAX(tat), 1) FROM t
UNION ALL SELECT 'Медиана', ROUND(tat, 1) FROM t WHERE rn = (n + 1) / 2
UNION ALL SELECT 'P90 (90% случаев быстрее)', ROUND(tat, 1) FROM t WHERE rn = CAST(0.9 * n AS INTEGER);

-- ---------------------------------------------------------------------
-- 3. Сроки по типу исследования и срочности
-- ---------------------------------------------------------------------
SELECT study_type, priority,
       COUNT(*) AS cases,
       ROUND(AVG(CAST(tat_days AS REAL)), 1) AS avg_tat_days,
       ROUND(MAX(CAST(tat_days AS REAL)), 1) AS max_tat_days
FROM cases
WHERE status = 'Выдан результат'
GROUP BY study_type, priority
ORDER BY study_type, priority;

-- ---------------------------------------------------------------------
-- 4. ГЛАВНЫЙ ЗАПРОС: где образцы ждут дольше всего (ожидание перед каждым действием)
--    share_pct = доля этого ожидания во всём суммарном ожидании
-- ---------------------------------------------------------------------
SELECT activity,
       COUNT(wait_hours) AS times,
       ROUND(AVG(wait_hours), 1) AS avg_wait_h,
       ROUND(MAX(wait_hours), 0) AS max_wait_h,
       ROUND(100.0 * SUM(wait_hours) / (SELECT SUM(wait_hours) FROM ev), 1) AS share_pct
FROM ev
WHERE wait_hours IS NOT NULL
GROUP BY activity
ORDER BY avg_wait_h DESC;

-- ---------------------------------------------------------------------
-- 5. Ожидание партии секвенирования: срочные и плановые
-- ---------------------------------------------------------------------
SELECT priority,
       COUNT(*) AS times,
       ROUND(AVG(wait_hours) / 24, 1) AS avg_wait_days,
       ROUND(MAX(wait_hours) / 24, 1) AS max_wait_days
FROM ev
WHERE activity = 'Секвенирование'
GROUP BY priority;

-- 5б. Зависит ли ожидание от дня недели, когда образец был готов (0 = воскресенье, 1 = понедельник, ...)
SELECT strftime('%w', prev_end) AS weekday_ready,
       COUNT(*) AS times,
       ROUND(AVG(wait_hours) / 24, 1) AS avg_wait_days
FROM ev
WHERE activity = 'Секвенирование' AND priority = 'Плановый'
GROUP BY weekday_ready
ORDER BY weekday_ready;

-- ---------------------------------------------------------------------
-- 6. Очередь к врачам-генетикам (ожидание перед интерпретацией)
-- ---------------------------------------------------------------------
SELECT priority,
       COUNT(*) AS times,
       ROUND(AVG(wait_hours), 1) AS avg_wait_h,
       ROUND(MAX(wait_hours), 0) AS max_wait_h
FROM ev
WHERE activity = 'Интерпретировать варианты'
GROUP BY priority;

-- 6б. По месяцам: растёт ли очередь
SELECT substr(start_time, 1, 7) AS month,
       COUNT(*) AS interpretations,
       ROUND(AVG(wait_hours), 1) AS avg_wait_h
FROM ev
WHERE activity = 'Интерпретировать варианты'
GROUP BY month
ORDER BY month;

-- 6в. Нагрузка по врачам (сколько случаев на каждого)
SELECT resource, COUNT(*) AS interpretations
FROM event_log
WHERE activity = 'Интерпретировать варианты'
GROUP BY resource
ORDER BY resource;

-- ---------------------------------------------------------------------
-- 7. Брак и повторы: как часто и как влияют на срок
-- ---------------------------------------------------------------------
SELECT 'Брак образца' AS event, SUM(CAST(sample_reject AS INTEGER)) AS cases,
       ROUND(100.0 * SUM(CAST(sample_reject AS INTEGER)) / COUNT(*), 1) AS pct FROM cases
UNION ALL SELECT 'Повтор выделения ДНК', SUM(CAST(dna_redo AS INTEGER)),
       ROUND(100.0 * SUM(CAST(dna_redo AS INTEGER)) / COUNT(*), 1) FROM cases
UNION ALL SELECT 'Повтор подготовки библиотеки', SUM(CAST(library_redo AS INTEGER)),
       ROUND(100.0 * SUM(CAST(library_redo AS INTEGER)) / COUNT(*), 1) FROM cases
UNION ALL SELECT 'Пересеквенирование', SUM(CAST(reseq AS INTEGER)),
       ROUND(100.0 * SUM(CAST(reseq AS INTEGER)) / COUNT(*), 1) FROM cases
UNION ALL SELECT 'Доработка заключения', SUM(CAST(report_rework AS INTEGER)),
       ROUND(100.0 * SUM(CAST(report_rework AS INTEGER)) / COUNT(*), 1) FROM cases;

-- 7б. Насколько повторы удлиняют процесс
SELECT CASE WHEN CAST(sample_reject AS INTEGER) + CAST(dna_redo AS INTEGER) + CAST(library_redo AS INTEGER)
                 + CAST(reseq AS INTEGER) + CAST(report_rework AS INTEGER) > 0
            THEN 'были повторы' ELSE 'без повторов' END AS grp,
       COUNT(*) AS cases,
       ROUND(AVG(CAST(tat_days AS REAL)), 1) AS avg_tat_days
FROM cases
WHERE status = 'Выдан результат'
GROUP BY grp;

-- 7в. Повтор по каждому типу отдельно: средний срок с повтором и без
SELECT 'Пересеквенирование' AS event, CAST(reseq AS INTEGER) AS happened,
       COUNT(*) AS cases, ROUND(AVG(CAST(tat_days AS REAL)), 1) AS avg_tat_days
FROM cases WHERE status = 'Выдан результат' GROUP BY reseq
UNION ALL
SELECT 'Доработка заключения', CAST(report_rework AS INTEGER),
       COUNT(*), ROUND(AVG(CAST(tat_days AS REAL)), 1)
FROM cases WHERE status = 'Выдан результат' GROUP BY report_rework;

-- ---------------------------------------------------------------------
-- 8. Самые долгие случаи и главная причина задержки
-- ---------------------------------------------------------------------
WITH w AS (
    SELECT case_id, activity, wait_hours,
           ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY wait_hours DESC) AS rn
    FROM ev
    WHERE wait_hours IS NOT NULL
)
SELECT c.case_id, c.study_type, c.priority,
       ROUND(CAST(c.tat_days AS REAL), 1) AS tat_days,
       w.activity AS longest_wait_before,
       ROUND(w.wait_hours / 24, 1) AS wait_days
FROM cases c
JOIN w ON w.case_id = c.case_id AND w.rn = 1
WHERE c.status = 'Выдан результат'
ORDER BY CAST(c.tat_days AS REAL) DESC
LIMIT 10;

-- 8б. Перед каким действием чаще всего самое долгое ожидание в случае
WITH w AS (
    SELECT case_id, activity, wait_hours,
           ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY wait_hours DESC) AS rn
    FROM ev
    WHERE wait_hours IS NOT NULL
)
SELECT activity, COUNT(*) AS cases_where_longest_wait
FROM w
WHERE rn = 1
GROUP BY activity
ORDER BY cases_where_longest_wait DESC;

-- ---------------------------------------------------------------------
-- 6г. Очередь к врачам: в какой день недели образцы приходят в очередь
--     и сколько там ждут (0 = воскресенье, 1 = понедельник, ... 5 = пятница)
--     Ожидание в пятницу включает выходные: это календарные часы
-- ---------------------------------------------------------------------
SELECT strftime('%w', prev_end) AS weekday_arrived,
       COUNT(*) AS cases,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM ev WHERE activity = 'Интерпретировать варианты'), 1) AS share_pct,
       ROUND(AVG(wait_hours), 1) AS avg_wait_h
FROM ev
WHERE activity = 'Интерпретировать варианты'
GROUP BY weekday_arrived
ORDER BY weekday_arrived;

-- ---------------------------------------------------------------------
-- 9. Какую часть срока образец просто ждёт (по выданным результатам)
--    avg_wait_days = среднее суммарное ожидание на случай, avg_tat_days = средний срок
-- ---------------------------------------------------------------------
WITH w AS (
    SELECT case_id,
           SUM(wait_hours) AS wait_all,
           SUM(CASE WHEN activity = 'Секвенирование' THEN wait_hours ELSE 0 END) AS wait_seq,
           SUM(CASE WHEN activity = 'Интерпретировать варианты' THEN wait_hours ELSE 0 END) AS wait_interp
    FROM ev
    GROUP BY case_id
)
SELECT ROUND(AVG(CAST(c.tat_days AS REAL)), 1) AS avg_tat_days,
       ROUND(AVG(w.wait_all) / 24, 1) AS avg_wait_days,
       ROUND(100.0 * SUM(w.wait_all) / 24 / SUM(CAST(c.tat_days AS REAL)), 1) AS wait_pct_of_tat,
       ROUND(AVG(w.wait_seq) / 24, 1) AS avg_wait_seq_days,
       ROUND(AVG(w.wait_interp) / 24, 1) AS avg_wait_interp_days,
       ROUND(100.0 * SUM(w.wait_seq + w.wait_interp) / 24 / SUM(CAST(c.tat_days AS REAL)), 1) AS seq_interp_pct_of_tat
FROM cases c
JOIN w ON w.case_id = c.case_id
WHERE c.status = 'Выдан результат';
