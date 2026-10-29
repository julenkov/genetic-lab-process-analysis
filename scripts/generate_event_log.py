#!/usr/bin/env python3
"""
Генератор СИНТЕТИЧЕСКОГО журнала событий (event log) для учебной модели процесса
«Обработка образцов в генетической лаборатории (NGS)».

ВАЖНО: все данные выдуманы. Реальные данные лабораторий не использовались.
Цель — показать метод анализа процесса, а не описать конкретную компанию.

Что заложено в модель намеренно (узкие места, которые нужно найти анализом):
  1. Секвенирование идёт партиями: плановые образцы — раз в неделю (вторник 09:00),
     срочные — дважды в неделю (вторник и пятница). Образцы ждут своей партии.
  2. Ограниченное число врачей-генетиков: после каждой партии результаты
     приходят волной, и перед интерпретацией образуется очередь.
  3. Повторы и брак: брак образца при приёме, повтор выделения ДНК,
     повтор подготовки библиотеки, пересеквенирование, доработка заключения.

Запуск:
    python scripts/generate_event_log.py
    python scripts/generate_event_log.py --cases 1500 --seed 42 --out data

Используется только стандартная библиотека Python 3.8+.

Выходные файлы:
    data/event_log.csv — по одной строке на выполненное действие (activity)
    data/cases.csv     — по одной строке на случай (case)
"""
import argparse
import csv
import heapq
import os
import random
from datetime import datetime, timedelta

# ------------------------- Параметры модели (меняй и сравнивай!) -------------------------
PARAMS = {
    "start_date": datetime(2026, 1, 12),    # начало поступления образцов
    "end_date": datetime(2026, 6, 30),      # конец поступления образцов
    "mean_cases_per_workday": None,         # считается из --cases
    "share_exome": 0.30,                    # доля экзомов (остальное — панели генов)
    "share_urgent": 0.12,                   # доля срочных случаев
    "p_reject_sample": 0.04,                # брак образца при приёме
    "p_cancel_after_reject": 0.25,          # не предоставили повторный образец
    "p_dna_qc_fail": 0.06,                  # брак по контролю ДНК
    "p_library_fail": 0.03,                 # брак подготовки библиотеки
    "p_run_qc_fail": 0.04,                  # брак данных секвенирования
    "p_report_rework": 0.08,                # доработка заключения после проверки
    "n_geneticists": 6,                     # врачей-генетиков
    "planned_run_weekdays": [1],            # дни партий для плановых (0=пн, 1=вт ...)
    "urgent_run_weekdays": [1, 4],          # дни партий для срочных
    "run_start_hour": 9,                    # старт партии
    "cutoff_hours_before_run": 15,          # приём в партию закрывается за N часов до старта
}

BASE_P_RUN_QC_FAIL = 0.04                     # брак данных в базовом процессе (для сценариев)
WORK_START, WORK_END = 9, 18                 # рабочий день 09:00–18:00, пн–пт

RES = {
    "registrar": ["Регистратор-1", "Регистратор-2"],
    "lab": ["Лаборант-1", "Лаборант-2", "Лаборант-3", "Лаборант-4"],
    "bio": ["Биоинформатик-1", "Биоинформатик-2"],
    "senior": ["Ст. врач-генетик"],
}

# ------------------------- Работа со временем -------------------------
def next_work_time(dt):
    """Ближайший рабочий момент не раньше dt."""
    while True:
        if dt.weekday() >= 5:
            dt = (dt + timedelta(days=1)).replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        elif dt.hour < WORK_START:
            dt = dt.replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        elif dt.hour >= WORK_END:
            dt = (dt + timedelta(days=1)).replace(hour=WORK_START, minute=0, second=0, microsecond=0)
        else:
            return dt


def add_work_hours(dt, hours):
    """Прибавить рабочие часы, пропуская ночи и выходные."""
    dt = next_work_time(dt)
    remaining = hours
    while remaining > 1e-9:
        day_end = dt.replace(hour=WORK_END, minute=0, second=0, microsecond=0)
        available = (day_end - dt).total_seconds() / 3600
        if remaining <= available:
            return dt + timedelta(hours=remaining)
        remaining -= available
        dt = next_work_time(day_end)
    return dt


# ------------------------- Журнал событий -------------------------
class Case:
    def __init__(self, case_id, study_type, priority, received):
        self.id = case_id
        self.study = study_type
        self.priority = priority
        self.received = received
        self.events = []
        self.t = received            # время готовности к следующему шагу
        self.cancelled = False
        self.rework = set()          # какие повторы были

    def add(self, activity, start, end, resource):
        self.events.append((activity, start, end, resource))
        self.t = end

    def work(self, rng, activity, resource, hours, queue_hours=(0.0, 0.0), calendar=False):
        """Выполнить действие: возможная задержка в очереди, затем сам шаг."""
        ready = self.t
        if calendar:                            # машина работает круглосуточно
            start, end = ready, ready + timedelta(hours=hours)
        else:
            q = rng.uniform(*queue_hours)
            if self.priority == "Срочный":
                q *= 0.3
            start = add_work_hours(ready, q)
            end = add_work_hours(start, hours)
        self.add(activity, start, end, resource)


def next_run_start(ready, weekdays, p):
    """Первая партия, в которую образец успевает до закрытия приёма."""
    day = ready.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(30):
        if day.weekday() in weekdays:
            run = day.replace(hour=p["run_start_hour"])
            if run - timedelta(hours=p["cutoff_hours_before_run"]) >= ready:
                return run
        day += timedelta(days=1)
    raise RuntimeError("Не нашли партию")


# ------------------------- Фаза 1: от приёма до биоинформатики -------------------------
def phase1(case, rng, p):
    pick = lambda key: rng.choice(RES[key])
    exome = case.study == "Экзом"

    case.work(rng, "Получить образец", pick("registrar"), 0.25)
    case.work(rng, "Зарегистрировать образец", pick("registrar"), rng.uniform(0.3, 0.7), (0, 2))

    # контроль качества образца
    case.work(rng, "Проверить качество образца", pick("lab"), rng.uniform(0.3, 0.6), (0, 3))
    if rng.random() < p["p_reject_sample"]:
        case.rework.add("sample_reject")
        case.work(rng, "Отклонить образец", pick("lab"), 0.2)
        case.work(rng, "Запросить повторный забор", pick("registrar"), 0.3, (0, 2))
        wait_days = rng.uniform(1, 5)
        if rng.random() < p["p_cancel_after_reject"]:
            case.t = case.t + timedelta(days=wait_days)
            case.work(rng, "Закрыть случай: образец не предоставлен", pick("registrar"), 0.2)
            case.cancelled = True
            return
        case.t = case.t + timedelta(days=wait_days)
        case.work(rng, "Получить повторный образец", pick("registrar"), 0.25)
        case.work(rng, "Проверить качество образца", pick("lab"), rng.uniform(0.3, 0.6), (0, 2))

    # выделение ДНК и контроль
    case.work(rng, "Выделить ДНК", pick("lab"), rng.uniform(2, 4), (0, 6))
    case.work(rng, "Проверить качество ДНК", pick("lab"), rng.uniform(0.5, 1.0), (0, 2))
    if rng.random() < p["p_dna_qc_fail"]:
        case.rework.add("dna_redo")
        case.work(rng, "Повторить выделение ДНК", pick("lab"), rng.uniform(2, 4), (0, 6))
        case.work(rng, "Проверить качество ДНК", pick("lab"), rng.uniform(0.5, 1.0), (0, 2))

    # подготовка библиотеки
    lib_h = (7, 10) if exome else (5, 7)
    case.work(rng, "Подготовить библиотеку", pick("lab"), rng.uniform(*lib_h), (0, 8))
    if rng.random() < p["p_library_fail"]:
        case.rework.add("library_redo")
        case.work(rng, "Повторить подготовку библиотеки", pick("lab"), rng.uniform(*lib_h), (0, 8))

    sequence_and_analyze(case, rng, p, first_time=True)


def sequence_and_analyze(case, rng, p, first_time):
    pick = lambda key: rng.choice(RES[key])
    exome = case.study == "Экзом"
    weekdays = p["urgent_run_weekdays"] if case.priority == "Срочный" else p["planned_run_weekdays"]

    # постановка в партию, дальше ожидание старта партии (узкое место №1)
    case.work(rng, "Поставить в партию секвенирования", pick("lab"), 0.25)
    run_start = next_run_start(case.t, weekdays, p)
    seq_hours = rng.uniform(40, 50) if exome else rng.uniform(20, 26)
    case.events.append(("Секвенирование", run_start, run_start + timedelta(hours=seq_hours), "Секвенатор"))
    case.t = run_start + timedelta(hours=seq_hours)

    case.work(rng, "Проверить качество данных секвенирования", pick("bio"), rng.uniform(1, 2), (0, 4))
    if first_time:
        u = rng.random()
        if u < p["p_run_qc_fail"]:
            case.rework.add("reseq")
            sequence_and_analyze(case, rng, p, first_time=False)
            return
        if u < BASE_P_RUN_QC_FAIL:
            # Сценарий с меньшим браком: базовый процесс здесь отправил бы образец на пересеквенирование.
            # Чтобы поток образцов в сценарии остался тем же, что в базовом, «проигрываем» эту ветку
            # на копии случая (расходуются те же случайные числа) и результат отбрасываем.
            clone = Case(case.id, case.study, case.priority, case.received)
            clone.events, clone.t = list(case.events), case.t
            sequence_and_analyze(clone, rng, p, first_time=False)
            analyze(case, random.Random(int(u * 1e9)), exome)
            return
    analyze(case, rng, exome)


def analyze(case, rng, exome):
    """Биоинформатический анализ (автоматический, идёт круглосуточно)."""
    ph = rng.uniform(10, 20) if exome else rng.uniform(4, 8)
    case.work(rng, "Выполнить биоинформатический анализ", "Пайплайн", ph, calendar=True)


# ------------------------- Фаза 2: очередь к врачам-генетикам -------------------------
def phase2(cases, rng, p):
    """Очередь на интерпретацию: N врачей, срочные впереди плановых."""
    pending = sorted((c for c in cases if not c.cancelled), key=lambda c: c.t)
    n = p["n_geneticists"]
    gfree = [(pending[0].t if pending else datetime(2026, 1, 1)) for _ in range(n)]
    queue, i = [], 0
    names = ["Врач-генетик-%d" % (k + 1) for k in range(n)]

    while i < len(pending) or queue:
        g = min(range(n), key=lambda k: gfree[k])
        t = next_work_time(gfree[g])
        while i < len(pending) and pending[i].t <= t:
            c = pending[i]
            heapq.heappush(queue, (0 if c.priority == "Срочный" else 1, c.t, c.id, c))
            i += 1
        if not queue:
            gfree[g] = pending[i].t
            continue
        _, _, _, c = heapq.heappop(queue)
        exome = c.study == "Экзом"
        start = max(t, c.t)
        interp = rng.uniform(3, 6) if exome else rng.uniform(1.5, 3)
        end1 = add_work_hours(start, interp)
        c.add("Интерпретировать варианты", start, end1, names[g])
        draft = rng.uniform(1, 2) if exome else rng.uniform(0.5, 1)
        end2 = add_work_hours(end1, draft)
        c.add("Подготовить заключение", end1, end2, names[g])
        gfree[g] = end2

        # проверка заключения
        senior = RES["senior"][0]
        c.work(rng, "Проверить заключение", senior, rng.uniform(0.5, 1.0), (0, 8))
        if rng.random() < p["p_report_rework"]:
            c.rework.add("report_rework")
            c.work(rng, "Доработать заключение", names[g], rng.uniform(1, 2), (0, 4))
            c.work(rng, "Проверить заключение", senior, rng.uniform(0.3, 0.6), (0, 3))

        c.work(rng, "Выдать результат", rng.choice(RES["registrar"]), 0.25, (0, 2))


# ------------------------- Генерация и запись -------------------------
def generate(n_cases, seed, out_dir, overrides=None):
    rng = random.Random(seed)
    p = dict(PARAMS)
    if overrides:
        p.update(overrides)          # параметры сценария (см. scripts/run_scenarios.py)

    workdays = []
    d = p["start_date"]
    while d <= p["end_date"]:
        if d.weekday() < 5:
            workdays.append(d)
        d += timedelta(days=1)
    mean = n_cases / len(workdays)

    # распределяем образцы по рабочим дням (случайный разброс вокруг среднего)
    arrivals = []
    for day in workdays:
        k = max(0, int(rng.gauss(mean, mean ** 0.5)))
        for _ in range(k):
            arrivals.append(day.replace(hour=WORK_START) + timedelta(hours=rng.uniform(0, WORK_END - WORK_START)))
    while len(arrivals) < n_cases:           # добираем до нужного числа случаев
        day = rng.choice(workdays)
        arrivals.append(day.replace(hour=WORK_START) + timedelta(hours=rng.uniform(0, WORK_END - WORK_START)))
    arrivals.sort()
    arrivals = arrivals[:n_cases]

    cases = []
    for idx, ts in enumerate(arrivals, start=1):
        study = "Экзом" if rng.random() < p["share_exome"] else "Панель генов"
        prio = "Срочный" if rng.random() < p["share_urgent"] else "Плановый"
        c = Case("C%05d" % idx, study, prio, ts)
        phase1(c, rng, p)
        cases.append(c)

    phase2(cases, rng, p)
    write_csv(cases, out_dir)
    return cases


def write_csv(cases, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(out_dir, "event_log.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "activity", "start_time", "end_time", "resource", "study_type", "priority"])
        for c in cases:
            for act, s, e, r in sorted(c.events, key=lambda x: (x[1], x[2])):
                w.writerow([c.id, act, fmt(s), fmt(e), r, c.study, c.priority])

    with open(os.path.join(out_dir, "cases.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["case_id", "study_type", "priority", "received_at", "finished_at",
                    "status", "tat_days", "sample_reject", "dna_redo", "library_redo", "reseq", "report_rework"])
        for c in cases:
            end = max(e for _, _, e, _ in c.events)
            status = "Отменён" if c.cancelled else "Выдан результат"
            tat = round((end - c.received).total_seconds() / 86400, 2)
            flags = [int(k in c.rework) for k in
                     ("sample_reject", "dna_redo", "library_redo", "reseq", "report_rework")]
            w.writerow([c.id, c.study, c.priority, fmt(c.received), fmt(end), status, tat] + flags)


def parse_days(text):
    """'0,2,4' -> [0, 2, 4] (0 = понедельник ... 4 = пятница)"""
    return [int(x) for x in text.split(",")]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Генератор синтетического журнала событий")
    ap.add_argument("--cases", type=int, default=1500, help="число случаев")
    ap.add_argument("--seed", type=int, default=42, help="seed для воспроизводимости")
    ap.add_argument("--out", default="data", help="папка для CSV")
    # параметры для сценариев «что будет, если» (по умолчанию = базовый процесс AS-IS)
    ap.add_argument("--planned-days", type=parse_days, help="дни партий для плановых, например 0,2,4 (0 = пн)")
    ap.add_argument("--urgent-days", type=parse_days, help="дни партий для срочных, например 0,1,2,3,4")
    ap.add_argument("--geneticists", type=int, help="число врачей-генетиков")
    ap.add_argument("--p-run-qc-fail", type=float, help="доля брака данных после секвенирования, например 0.02")
    a = ap.parse_args()
    over = {}
    if a.planned_days: over["planned_run_weekdays"] = a.planned_days
    if a.urgent_days: over["urgent_run_weekdays"] = a.urgent_days
    if a.geneticists: over["n_geneticists"] = a.geneticists
    if a.p_run_qc_fail is not None: over["p_run_qc_fail"] = a.p_run_qc_fail
    res = generate(a.cases, a.seed, a.out, over)
    print("Готово: %d случаев → %s/event_log.csv, %s/cases.csv" % (len(res), a.out, a.out))
