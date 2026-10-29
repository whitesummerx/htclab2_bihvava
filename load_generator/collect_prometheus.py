#!/usr/bin/env python3
"""
Вивантажує з Prometheus сирі відліки метрик за час ступінчастого прогону.

Для прогону results/step_<мітка>.json бере start_epoch і end_epoch, запитує у
Prometheus (за замовчуванням http://localhost:9090) потрібні ряди разом зі
справжніми часовими мітками збору й пише results/prom_<мітка>.json.
Запускати одразу після прогону і до `docker compose down -v`: дані Prometheus
лежать у томі.

  python load_generator/collect_prometheus.py --label step_1web
"""
import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

PATH = "/appointments/{appointment_id}"

QUERIES = {
    # гістограма тривалості обробки читання за id: накопичені лічильники бакетів кожного екземпляра
    "duration_bucket": 'http_request_duration_seconds_bucket{path="%s"}' % PATH,
    "duration_count": 'http_request_duration_seconds_count{path="%s"}' % PATH,
    # лічильник запитів за кодами
    "requests_total": 'http_requests_total{path="%s"}' % PATH,
    # CPU процесу сервісу, секунди
    "cpu_seconds": "process_cpu_seconds_total",
    "resident_memory": "process_resident_memory_bytes",
    # запити в обробці
    "in_progress": "http_requests_in_progress",
    # М2: зʼєднання з базою за станом і транзакції
    "pg_connections": "pg_stat_activity_count",
    "pg_xact_commit": "pg_stat_database_xact_commit",
    "pg_xact_rollback": "pg_stat_database_xact_rollback",
    "up": "up",
}


def instant_range(base, expr, end, duration_s):
    q = f"{expr}[{duration_s}s]"
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": q, "time": f"{end:.3f}"})
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    if data.get("status") != "success":
        raise SystemExit(f"Prometheus відповів помилкою на {expr}: {data}")
    return [{"metric": r["metric"], "values": r["values"]} for r in data["data"]["result"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="мітка прогону, наприклад step_1web")
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--margin", type=float, default=30.0, help="запас до і після прогону, с")
    args = ap.parse_args()

    src = os.path.join(RESULTS_DIR, f"{args.label}.json")
    with open(src, encoding="utf-8") as f:
        run = json.load(f)
    start = run["start_epoch"] - args.margin
    end = run["end_epoch"] + args.margin
    out = {"label": args.label, "from_epoch": start, "to_epoch": end, "series": {}}
    for name, expr in QUERIES.items():
        res = instant_range(args.prom, expr, end, math.ceil(end - start))
        out["series"][name] = res
        print(f"{name}: {len(res)} рядів, {sum(len(r['values']) for r in res)} відліків")
    path = os.path.join(RESULTS_DIR, f"prom_{args.label.removeprefix('step_')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print("Збережено:", os.path.relpath(path, REPO_ROOT))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
