#!/usr/bin/env python3
"""
Сценарии «что будет, если» для учебной модели лаборатории.

Что делает скрипт:
  1. Запускает генератор для каждого сценария (меняется только одно-два правила,
     остальное и seed одинаковые, значит, поток образцов тот же).
  2. Собирает все результаты в одну базу analysis/scenarios.db
     (таблицы event_log и cases с колонкой scenario, представление ev).
  3. С ключом --robustness повторяет сценарии на нескольких seed и показывает,
     насколько результат устойчив к случайности.

Запуск (из корня репозитория):
    python scripts/run_scenarios.py
    python scripts/run_scenarios.py --robustness
"""
import argparse
import copy
import csv
import os
import sqlite3
import statistics as st
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_event_log as gen

ALL_DAYS = [0, 1, 2, 3, 4]   # пн-пт

# id, название, что меняем относительно базового процесса
SCENARIOS = [
    ("S0", "Базовый (как сейчас)", {}),
    ("S1", "Партии 2 раза в неделю", {"planned_run_weekdays": [1, 3]}),
    ("S2", "Партии 3 раза в неделю (пн, ср, пт), срочные ежедневно",
     {"planned_run_weekdays": [0, 2, 4], "urgent_run_weekdays": ALL_DAYS}),
    ("S3", "Партии ежедневно",
     {"planned_run_weekdays": ALL_DAYS, "urgent_run_weekdays": ALL_DAYS}),
    ("S4", "Седьмой врач-генетик", {"n_geneticists": 7}),
    ("S5", "Контроль библиотеки (брак данных 4% -> 2%)", {"p_run_qc_fail": 0.02}),
    ("S6", "Рекомендуемый: S2 + S5",
     {"planned_run_weekdays": [0, 2, 4], "urgent_run_weekdays": ALL_DAYS, "p_run_qc_fail": 0.02}),
    ("S7", "Проверка риска: партии ежедневно, но врачей 5",
     {"planned_run_weekdays": ALL_DAYS, "urgent_run_weekdays": ALL_DAYS, "n_geneticists": 5}),
]


def run(overrides, seed, cases=1500):
    """Запустить генератор с параметрами сценария. Возвращает список случаев."""
    with tempfile.TemporaryDirectory() as tmp:
        return gen.generate(cases, seed, tmp, overrides), tmp


def build_db(path, seed):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE scenarios(scenario TEXT, title TEXT);
    CREATE TABLE event_log(scenario TEXT, case_id TEXT, activity TEXT, start_time TEXT, end_time TEXT,
                           resource TEXT, study_type TEXT, priority TEXT);
    CREATE TABLE cases(scenario TEXT, case_id TEXT, study_type TEXT, priority TEXT, received_at TEXT,
                       finished_at TEXT, status TEXT, tat_days REAL, sample_reject INTEGER, dna_redo INTEGER,
                       library_redo INTEGER, reseq INTEGER, report_rework INTEGER);
    """)
    for sid, title, over in SCENARIOS:
        with tempfile.TemporaryDirectory() as tmp:
            gen.generate(1500, seed, tmp, over)
            ev = list(csv.reader(open(os.path.join(tmp, "event_log.csv"), encoding="utf-8")))[1:]
            cs = list(csv.reader(open(os.path.join(tmp, "cases.csv"), encoding="utf-8")))[1:]
        con.execute("INSERT INTO scenarios VALUES (?,?)", (sid, title))
        con.executemany("INSERT INTO event_log VALUES (?,?,?,?,?,?,?,?)", [[sid] + r for r in ev])
        con.executemany("INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [[sid] + r for r in cs])
        print("  %s: %d событий, %d случаев" % (sid, len(ev), len(cs)))
    con.executescript("""
    CREATE VIEW ev AS
    SELECT scenario, case_id, activity, start_time, end_time, resource, study_type, priority,
        LAG(end_time) OVER (PARTITION BY scenario, case_id ORDER BY start_time, end_time) AS prev_end,
        (julianday(start_time) - julianday(LAG(end_time) OVER (PARTITION BY scenario, case_id
                                                                ORDER BY start_time, end_time))) * 24 AS wait_hours
    FROM event_log;
    """)
    con.commit()
    con.close()


def mean_tat(cases):
    vals = []
    for c in cases:
        if c.cancelled:
            continue
        end = max(e for _, _, e, _ in c.events)
        vals.append((end - c.received).total_seconds() / 86400)
    return st.mean(vals)


def robustness(seeds):
    """Средний срок каждого сценария на нескольких seed и разница с базовым на тех же seed."""
    res = {sid: [] for sid, _, _ in SCENARIOS}
    for seed in seeds:
        for sid, _, over in SCENARIOS:
            cases, _ = run(over, seed)
            res[sid].append(mean_tat(cases))
    print("\nУстойчивость: средний срок (дни) на %d seed" % len(seeds))
    print("%-4s %-55s %8s %10s %18s" % ("", "сценарий", "срок", "изменение", "мин...макс изменения"))
    base = res["S0"]
    for sid, title, _ in SCENARIOS:
        diffs = [a - b for a, b in zip(res[sid], base)]
        print("%-4s %-55s %8.1f %+10.1f %+9.1f ... %+.1f" %
              (sid, title[:55], st.mean(res[sid]), st.mean(diffs), min(diffs), max(diffs)))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default="analysis/scenarios.db")
    ap.add_argument("--robustness", action="store_true", help="повторить на seed 1..10")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.db) or ".", exist_ok=True)
    print("Строим базу сценариев (seed %d)..." % a.seed)
    build_db(a.db, a.seed)
    print("Готово:", a.db)
    if a.robustness:
        robustness(range(1, 11))
