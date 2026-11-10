#!/usr/bin/env python3
"""Лаб. 2, крок 3: посекундний запис навколо `docker stop` і код завершення контейнера.

Вхід (results/): step_stop_check.json (прогін генератора), stop_check_meta.json (час і код завершення,
пише scripts/run-stop-check.ps1). Вихід: results/stop_check_window.md і друк у консоль.
Запуск:  python analysis/stop_check.py
"""
import json, math, sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "results"
run_file = root / "stop_check.json" if (root / "stop_check.json").exists() else root / "step_stop_check.json"
run = json.load(open(run_file, encoding="utf-8"))
meta = json.load(open(root / "stop_check_meta.json", encoding="utf-8-sig"))
t0 = run["start_epoch"]
b, e = float(meta["stop_begin_epoch"]), float(meta["stop_end_epoch"])
s_b, s_e = math.floor(b - t0), math.floor(e - t0)
lo, hi = s_b - 5, s_b + 5          # 10 секунд навколо зупинки: 5 до секунди початку docker stop і 5 після
rows = [r for r in run["seconds"] if lo <= r["second"] < hi]
L = [f"Контейнер: {meta['container']}",
     f"Команда: docker inspect --format '{{{{.State.ExitCode}}}}' {meta['container']}",
     f"Вивід: {meta['exit_code']}",
     f"docker stop виконувався {e - b:.2f} с: від секунди {s_b} до секунди {s_e} прогону (start_epoch = {t0}).",
     "",
     f"Посекундний запис генератора, секунди {lo}–{hi - 1} прогону (рядок з позначкою ◄ — секунда початку docker stop):", "",
     "| секунда | задано | надіслано | 2xx | не-2xx | помилки зʼєднання | p99 клієнта, мс |", "|---|---|---|---|---|---|---|"]
for r in rows:
    mark = " ◄ docker stop" if r["second"] == s_b else ""
    p99 = "—" if r["p99_ms"] is None else f"{r['p99_ms']:.1f}".replace(".", ",")
    L.append(f"| {r['second']}{mark} | {r['target_rps']} | {r['sent_rps']} | {r['n_2xx']} | {r['n_non2xx']} | {r['n_conn_errors']} | {p99} |")
L += ["", f"Разом за {len(rows)} с: 2xx = {sum(r['n_2xx'] for r in rows)}, не-2xx = {sum(r['n_non2xx'] for r in rows)}, "
      f"помилок зʼєднання = {sum(r['n_conn_errors'] for r in rows)}.",
      f"За весь прогін ({len(run['seconds'])} с): не-2xx = {sum(r['n_non2xx'] for r in run['seconds'])}, "
      f"помилок зʼєднання = {sum(r['n_conn_errors'] for r in run['seconds'])}."]
text = "\n".join(L)
(root / "stop_check_window.md").write_text(text, encoding="utf-8")
print(text)
