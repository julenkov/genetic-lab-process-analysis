-- =====================================================================
-- Оценка эффекта улучшений: сравнение сценариев
-- База: analysis/scenarios.db (таблицы event_log, cases, scenarios, представление ev).
-- В каждой таблице есть колонка scenario: S0 = как сейчас, S1...S7 = сценарии улучшений.
-- Описание сценариев: запрос 0 или docs/improvements.md.
-- Выполняй запросы по одному и записывай цифры в таблицу «до и после».
-- =====================================================================

-- 0. Какие сценарии есть
SELECT scenario, title FROM scenarios ORDER BY scenario;

-- ---------------------------------------------------------------------
-- 1. Срок выполнения (TAT) по сценариям: среднее, медиана, P90
-- ---------------------------------------------------------------------
WITH t AS (
    SELECT scenario, tat_days,
           ROW_NUMBER() OVER (PARTITION BY scenario ORDER BY tat_days) AS rn,
           COUNT(*) OVER (PARTITION BY scenario) AS n
    FROM cases
    WHERE status = 'Выдан результат'
)
SELECT scenario,
       COUNT(*) AS cases,
       ROUND(AVG(tat_days), 1) AS avg_tat,
       ROUND(MAX(CASE WHEN rn = (n + 1) / 2 THEN tat_days END), 1) AS median_tat,
       ROUND(MAX(CASE WHEN rn = CAST(0.9 * n AS INTEGER) THEN tat_days END), 1) AS p90_tat
FROM t
GROUP BY scenario
ORDER BY scenario;

-- ---------------------------------------------------------------------
-- 2. До и после: на сколько дней и процентов сократился средний срок относительно S0
-- ---------------------------------------------------------------------
WITH s AS (
    SELECT scenario, AVG(tat_days) AS avg_tat
    FROM cases
    WHERE status = 'Выдан результат'
    GROUP BY scenario
)
SELECT s.scenario, sc.title,
       ROUND(s.avg_tat, 1) AS avg_tat,
       ROUND(s.avg_tat - b.avg_tat, 1) AS diff_days,
       ROUND(100.0 * (s.avg_tat - b.avg_tat) / b.avg_tat, 1) AS diff_pct
FROM s
JOIN s AS b ON b.scenario = 'S0'
JOIN scenarios sc ON sc.scenario = s.scenario
ORDER BY s.scenario;

-- ---------------------------------------------------------------------
-- 3. Где изменилось ожидание: перед секвенированием и перед врачом (в днях на случай)
-- ---------------------------------------------------------------------
WITH w AS (
    SELECT scenario, case_id,
           SUM(wait_hours) AS wait_all,
           SUM(CASE WHEN activity = 'Секвенирование' THEN wait_hours ELSE 0 END) AS wait_seq,
           SUM(CASE WHEN activity = 'Интерпретировать варианты' THEN wait_hours ELSE 0 END) AS wait_interp
    FROM ev
    GROUP BY scenario, case_id
)
SELECT w.scenario,
       ROUND(AVG(w.wait_seq) / 24, 1) AS wait_seq_days,
       ROUND(AVG(w.wait_interp) / 24, 1) AS wait_interp_days,
       ROUND(AVG(w.wait_all) / 24, 1) AS wait_all_days
FROM w
JOIN cases c ON c.scenario = w.scenario AND c.case_id = w.case_id
WHERE c.status = 'Выдан результат'
GROUP BY w.scenario
ORDER BY w.scenario;

-- ---------------------------------------------------------------------
-- 4. Плановые и срочные случаи по сценариям (средний срок, дни)
-- ---------------------------------------------------------------------
SELECT scenario, priority,
       COUNT(*) AS cases,
       ROUND(AVG(tat_days), 1) AS avg_tat
FROM cases
WHERE status = 'Выдан результат'
GROUP BY scenario, priority
ORDER BY scenario, priority;

-- ---------------------------------------------------------------------
-- 5. Повторные секвенирования по сценариям (механизм сценария 5)
-- ---------------------------------------------------------------------
SELECT scenario,
       SUM(reseq) AS reseq_cases,
       ROUND(100.0 * SUM(reseq) / COUNT(*), 1) AS reseq_pct
FROM cases
GROUP BY scenario
ORDER BY scenario;
