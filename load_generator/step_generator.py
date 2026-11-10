#!/usr/bin/env python3
"""
Ступінчастий генератор навантаження (лабораторна робота 2).

Профіль: прогрів 10 с на рівні 200 запитів за секунду, далі сходинки по 20 с
від 200 запитів за секунду з приростом 200. Прогін зупиняється, коли на двох
сходинках поспіль досягнута інтенсивність нижча за 0,9 заданої, або після
сходинки, що досягла межі генератора G (параметр --g-limit).

Відкритий контур: моменти відправки обчислено наперед від спільного старту,
затримка рахується від запланованого моменту, тож генератор не «підлаштовується»
під повільний сервіс. Запити виконує асинхронний клієнт: кожен запит у польоті
займає лише корутину, а не потік, тому фактична інтенсивність надсилання
дорівнює заданій і при великих чергах.

Тайм-аут запиту 5 с відраховується від запланованого моменту; запит, що його
перевищив, рахується як відповідь із кодом не 2xx (у записах код -2).
Помилки зʼєднання (скидання, відмова, обрив до відповіді) рахуються окремо
(код 0).

Лише стандартна бібліотека (Python 3.11+).

Приклади (з кореня репозиторію):
  python load_generator/step_generator.py run --target noop --instances 0
  python load_generator/step_generator.py run --instances 1 --g-limit 3000
  python load_generator/step_generator.py run --instances 4 --g-limit 3000

Результати: results/step_<мітка>.json і results/step_<мітка>_seconds.csv
(посекундний запис).
"""
import argparse
import asyncio
import csv
import http.client
import json
import os
import platform
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from multiprocessing import get_context
from urllib.parse import urlparse

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RESULTS_DIR = os.path.join(REPO_ROOT, "results")

SEED_COUNT = 100
MAX_SECONDS = 4000   # розмір спільних масивів лічильників (секунд від старту)
STATUS_TIMEOUT = -2  # у записах: запит перевищив тайм-аут (рахується як не 2xx)
STATUS_CONN = 0      # у записах: помилка зʼєднання


# --------------------------------------------------------------------------- #
# Допоміжні функції
# --------------------------------------------------------------------------- #
def pin_to_cpus(cpus):
    """Прив'язує поточний процес до заданих логічних ядер (нумерація з 0). Повертає True, якщо вдалося."""
    if not cpus:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            mask = 0
            for c in cpus:
                mask |= 1 << c
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
            k32.SetProcessAffinityMask.restype = ctypes.c_int
            ok = bool(k32.SetProcessAffinityMask(k32.GetCurrentProcess(), mask))
            if not ok:
                print(f"SetProcessAffinityMask: помилка {ctypes.get_last_error()} (маска {mask:#x}, ядер у системі {os.cpu_count()})")
            return ok
        os.sched_setaffinity(0, set(cpus))
        return True
    except Exception:
        return False


def percentile(sorted_vals, p):
    """Перцентиль p (0..100) з лінійною інтерполяцією для відсортованого списку."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def http_json(base, method, path, body=None, timeout=10):
    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, (json.loads(data) if data else None)
    finally:
        conn.close()


def ensure_seed(base, count=SEED_COUNT):
    """Гарантує щонайменше `count` записів у базі й повертає їхні ідентифікатори."""
    try:
        status, data = http_json(base, "GET", "/appointments?limit=1")
    except OSError as e:
        raise SystemExit(f"Немає зʼєднання з {base}: {e}. Стенд піднято?")
    if status != 200:
        raise SystemExit(f"GET /appointments повернув {status}: стенд не готовий ({base})")
    total = data["total"]
    if total < count:
        print(f"Заношу {count - total} записів (у базі {total}, потрібно {count})...")
        for i in range(total, count):
            minutes = i * 30
            body = {
                "patient_code": f"SEED-{i:03d}",
                "doctor": "Dr. Seed",
                "room": str(100 + i % 20),
                "start_time": (f"2026-11-{1 + minutes // 1440:02d}"
                               f"T{(minutes % 1440) // 60:02d}:{minutes % 60:02d}:00Z"),
                "duration_minutes": 30,
                "status": "scheduled",
            }
            st, resp = http_json(base, "POST", "/appointments", body)
            if st != 201:
                raise SystemExit(f"Не вдалося створити запис {i}: {st} {resp}")
    status, data = http_json(base, "GET", f"/appointments?limit={count}")
    ids = [item["id"] for item in data["items"]]
    if len(ids) < count:
        raise SystemExit(f"Очікував {count} ідентифікаторів, отримав {len(ids)}")
    return ids[:count]


def segment(j, cfg):
    """Сегмент профілю j: (початок, тривалість, інтенсивність). j=0 прогрів, j>=1 сходинка j-1."""
    if j == 0:
        return 0.0, cfg["warmup"], float(cfg["start_rps"])
    k = j - 1
    return (cfg["warmup"] + k * cfg["step_s"], cfg["step_s"],
            float(cfg["start_rps"] + k * cfg["step_rps"]))


def parse_cpus(text):
    """'4,5' або '4-5' -> [4, 5]; порожній рядок -> []."""
    out = []
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def target_of_step(k, cfg):
    return cfg["start_rps"] + k * cfg["step_rps"]


def planned_times(cfg, proc, procs):
    """Нескінченна послідовність запланованих моментів (с від старту) для процесу `proc`.
    Усі процеси разом дають рівномірний потік із заданою інтенсивністю."""
    j = 0
    while True:
        seg_start, seg_len, rate = segment(j, cfg)
        i = 0
        while True:
            t = seg_start + (i * procs + proc) / rate
            if t >= seg_start + seg_len:
                break
            yield t
            i += 1
        j += 1


# --------------------------------------------------------------------------- #
# Процес-виконавець (асинхронний клієнт)
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self, cfg, proc, procs, ok_counts, stop_flag, start_wall):
        self.cfg = cfg
        self.proc = proc
        self.procs = procs
        self.ok_counts = ok_counts
        self.stop_flag = stop_flag
        self.start_wall = start_wall
        self.records = []   # (заплановано, відправлено, завершено, код, вид помилки), с від старту
        self.lags = []      # запізнення планувальника, мс
        self.idle = []      # вільні зʼєднання (keep-alive)
        self.cpu_samples = []  # (секунда від старту, накопичений процесорний час процесу, с)
        self.kinds = {}
        self.t0 = 0.0

    def _kind(self, k):
        return self.kinds.setdefault(k, k)

    def _record(self, planned, sent, done, status, kind=""):
        self.records.append((planned, sent, done, status, kind))
        if 200 <= status < 300:
            sec = int(done)
            if 0 <= sec < len(self.ok_counts):
                self.ok_counts[sec] += 1

    async def _exchange(self, conn, req):
        """Надсилає запит і читає відповідь повністю. Повертає (код, чи зберегти зʼєднання)."""
        reader, writer = conn
        writer.write(req)
        line = await reader.readline()
        if not line:
            raise EOFError("зʼєднання закрито до відповіді")
        parts = line.split(None, 2)
        status = int(parts[1])
        clen = None
        chunked = False
        close = False
        while True:
            h = await reader.readline()
            if not h:
                raise EOFError("зʼєднання закрито в заголовках")
            if h in (b"\r\n", b"\n"):
                break
            name, _, val = h.partition(b":")
            name = name.strip().lower()
            if name == b"content-length":
                clen = int(val)
            elif name == b"transfer-encoding":
                chunked = b"chunked" in val.lower()
            elif name == b"connection":
                close = b"close" in val.lower()
        if status in (204, 304) or 100 <= status < 200:
            pass
        elif chunked:
            while True:
                size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()  # завершальний порожній рядок
                    break
                await reader.readexactly(size + 2)
        elif clen is not None:
            if clen:
                await reader.readexactly(clen)
        else:
            await reader.read()  # тіло до закриття зʼєднання
            close = True
        return status, not close

    async def _request(self, planned, req):
        cfg = self.cfg
        planned_abs = self.t0 + planned
        sent_abs = time.perf_counter()
        sent = sent_abs - self.t0
        remaining = planned_abs + cfg["timeout"] - sent_abs
        if remaining <= 0:
            # запит уже прострочений до відправки: генератор відстав
            self._record(planned, sent, sent, STATUS_TIMEOUT, self._kind("expired"))
            return
        conn = None
        status = STATUS_CONN
        kind = ""
        keep = False
        try:
            async with asyncio.timeout(remaining):
                conn = self.idle.pop() if self.idle else await asyncio.open_connection(
                    cfg["host"], cfg["port"])
                status, keep = await self._exchange(conn, req)
        except TimeoutError:
            status, kind = STATUS_TIMEOUT, "timeout"
        except Exception as e:
            status = STATUS_CONN
            kind = type(e).__name__
            code = getattr(e, "winerror", None) or getattr(e, "errno", None)
            if code:
                kind += f"[{code}]"
        done = time.perf_counter() - self.t0
        if conn is not None:
            if keep and status > 0:
                self.idle.append(conn)
            else:
                try:
                    conn[1].close()
                except Exception:
                    pass
        self._record(planned, sent, done, status, self._kind(kind))

    async def run(self):
        cfg = self.cfg
        reqs = cfg["requests"]
        rng = random.Random(1000 + self.proc)
        # Спільний старт: усі процеси чекають на один і той самий момент за настінним годинником
        await asyncio.sleep(max(0.0, self.start_wall - time.time()))
        self.t0 = time.perf_counter() - (time.time() - self.start_wall)

        gen = planned_times(cfg, self.proc, self.procs)
        nxt = next(gen)
        next_cpu_sample = 0.0
        tasks = set()
        loop = asyncio.get_running_loop()
        while not self.stop_flag.value:
            now = time.perf_counter() - self.t0
            if now >= next_cpu_sample:
                # процесорний час самого процесу генератора, щоб потім взяти його за 15 с потрібної сходинки
                self.cpu_samples.append((now, time.process_time()))
                next_cpu_sample = now + 1.0
            launched = 0
            while nxt <= now and launched < 200:
                lag_ms = (now - nxt) * 1000.0
                self.lags.append(lag_ms)
                task = loop.create_task(self._request(nxt, reqs[rng.randrange(len(reqs))]))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                nxt = next(gen)
                launched += 1
            delay = nxt - (time.perf_counter() - self.t0)
            await asyncio.sleep(delay if delay > 0 else 0)

        if tasks:
            await asyncio.wait(tasks, timeout=cfg["timeout"] + 2)
        for conn in self.idle:
            try:
                conn[1].close()
            except Exception:
                pass
        self.cpu_samples.append((time.perf_counter() - self.t0, time.process_time()))
        return {"proc": self.proc, "records": self.records, "lags": self.lags,
                "cpu_s": time.process_time(), "cpu_samples": self.cpu_samples}


def _worker_main(cfg, proc, procs, ok_counts, stop_flag, start_wall, conn):
    try:
        pin_to_cpus(cfg.get("cpus"))
        if sys.platform == "win32":
            try:  # точність таймера 1 мс замість 15,6 мс
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass
        w = Worker(cfg, proc, procs, ok_counts, stop_flag, start_wall)
        res = asyncio.run(w.run())
        conn.send(res)
    except Exception as e:
        conn.send({"error": repr(e), "proc": proc})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Керування прогоном і підсумок
# --------------------------------------------------------------------------- #
def build_requests(args, target):
    host = urlparse(args.base).hostname
    if target == "noop":
        paths = ["/noop"]
    else:
        paths = [f"/appointments/{i}" for i in ensure_seed(args.base)]
    return [f"GET {p} HTTP/1.1\r\nHost: {host}\r\nAccept: */*\r\n\r\n".encode() for p in paths]


def run_profile(args, target):
    u = urlparse(args.base)
    procs = args.procs or (len(parse_cpus(args.cpus)) or os.cpu_count() or 1)
    cfg = {
        "host": u.hostname, "port": u.port or 80,
        "requests": build_requests(args, target),
        "warmup": args.warmup, "step_s": args.step_seconds,
        "start_rps": args.start_rps, "step_rps": args.step_rps,
        "timeout": args.timeout,
        "cpus": parse_cpus(args.cpus),
    }
    pinned = pin_to_cpus(cfg["cpus"])
    if cfg["cpus"]:
        print(f"Генератор прив'язано до ядер {cfg['cpus']}: {'так' if pinned else 'НЕ ВДАЛОСЯ'}")
    ctx = get_context("spawn")
    ok_arrays = [ctx.Array("i", MAX_SECONDS, lock=False) for _ in range(procs)]
    stop_flag = ctx.Value("b", 0, lock=False)
    start_wall = time.time() + 3.0 + 0.5 * procs  # запас на запуск процесів

    pipes, ps = [], []
    for k in range(procs):
        r, w = ctx.Pipe(duplex=False)
        p = ctx.Process(target=_worker_main,
                        args=(cfg, k, procs, ok_arrays[k], stop_flag, start_wall, w))
        p.start()
        w.close()
        pipes.append(r)
        ps.append(p)

    window = args.step_seconds - args.transient
    steps_done = 0
    consecutive_low = 0
    stop_reason = None
    print(f"Старт: {datetime.fromtimestamp(start_wall, timezone.utc).isoformat(timespec='seconds')} "
          f"({procs} процесів). Прогрів {args.warmup:.0f} с, далі сходинки по {args.step_seconds:.0f} с.")
    try:
        while stop_reason is None:
            k = steps_done
            step_end = args.warmup + (k + 1) * args.step_seconds
            wait_until = start_wall + step_end + 0.7  # дати завершитись відповідям останньої секунди
            while time.time() < wait_until:
                time.sleep(0.1)
                if any(not p.is_alive() and p.exitcode not in (0, None) for p in ps):
                    raise SystemExit("Процес генератора завершився з помилкою")
            lo = int(args.warmup + k * args.step_seconds + args.transient)
            hi = int(args.warmup + (k + 1) * args.step_seconds)
            per_sec = [sum(a[s] for a in ok_arrays) for s in range(lo, hi)]
            achieved = sum(per_sec) / window
            target_rps = target_of_step(k, cfg)
            low = achieved < 0.9 * target_rps
            consecutive_low = consecutive_low + 1 if low else 0
            steps_done += 1
            print(f"  сходинка {k + 1}: задано {target_rps}, досягнуто {achieved:.1f} "
                  f"({100 * achieved / target_rps:.0f} %)")
            if consecutive_low >= 2:
                stop_reason = "на двох сходинках поспіль досягнута інтенсивність нижча за 0,9 заданої"
            elif args.g_limit and target_rps >= args.g_limit:
                stop_reason = f"сходинка досягла межі генератора G = {args.g_limit}"
            elif target_rps + args.step_rps > args.max_rps:
                stop_reason = f"досягнуто обмеження --max-rps {args.max_rps}"
            elif (args.warmup + (steps_done + 1) * args.step_seconds + 5) >= MAX_SECONDS:
                stop_reason = "вичерпано розмір лічильників"
    finally:
        stop_flag.value = 1

    results = []
    wait_limit = args.timeout + 60
    for r in pipes:
        if not r.poll(wait_limit):
            raise SystemExit("Процес генератора не відповів вчасно")
        results.append(r.recv())
    for p in ps:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()
    for res in results:
        if "error" in res:
            raise SystemExit(f"Помилка в процесі генератора: {res['error']}")
    return cfg, procs, start_wall, steps_done, stop_reason, results


def _interp_cpu(samples, t):
    """Накопичений процесорний час процесу в момент t (лінійна інтерполяція між відліками)."""
    if t <= samples[0][0]:
        return samples[0][1]
    for (t0, c0), (t1, c1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return c0 + (c1 - c0) * (t - t0) / (t1 - t0) if t1 > t0 else c1
    return samples[-1][1]


def analyze(args, cfg, steps_done, results):
    warm, step_s, tr = args.warmup, args.step_seconds, args.transient
    nsec = int(warm + steps_done * step_s)
    sent_n = [0] * nsec
    ok_n = [0] * nsec
    non_n = [0] * nsec
    conn_n = [0] * nsec
    lats = [[] for _ in range(nsec)]
    err_kinds = {}
    for res in results:
        for planned, sent, done, st, kind in res["records"]:
            si = int(sent)
            if 0 <= si < nsec:
                sent_n[si] += 1
            di = int(done)
            if 0 <= di < nsec:
                if st == STATUS_CONN:
                    conn_n[di] += 1
                    err_kinds[kind] = err_kinds.get(kind, 0) + 1
                else:
                    lats[di].append((done - planned) * 1000.0)
                    if 200 <= st < 300:
                        ok_n[di] += 1
                    else:
                        non_n[di] += 1

    seconds = []
    for s in range(nsec):
        if s < warm:
            phase, target = "warmup", cfg["start_rps"]
        else:
            k = int((s - warm) // step_s)
            phase, target = f"step{k + 1}", target_of_step(k, cfg)
        srt = sorted(lats[s])
        seconds.append({
            "second": s, "phase": phase, "target_rps": target,
            "sent_rps": sent_n[s], "achieved_rps": ok_n[s],
            "n_2xx": ok_n[s], "n_non2xx": non_n[s], "n_conn_errors": conn_n[s],
            "p50_ms": None if not srt else round(percentile(srt, 50), 2),
            "p95_ms": None if not srt else round(percentile(srt, 95), 2),
            "p99_ms": None if not srt else round(percentile(srt, 99), 2),
        })

    steps = []
    for k in range(steps_done):
        a = int(warm + k * step_s + tr)
        b = int(warm + (k + 1) * step_s)
        win = range(a, b)
        ach = [ok_n[s] for s in win]
        wl = sorted(x for s in win for x in lats[s])
        n_ok = sum(ok_n[s] for s in win)
        n_non = sum(non_n[s] for s in win)
        target = target_of_step(k, cfg)
        mean = statistics.fmean(ach)
        # CPU процесів генератора за ці самі 15 с: сума приростів процесорного часу / тривалість вікна
        gen_cpu = 0.0
        for res in results:
            cs = res["cpu_samples"]
            gen_cpu += _interp_cpu(cs, b) - _interp_cpu(cs, a)
        gen_cpu /= (b - a)
        steps.append({
            "step": k + 1, "target_rps": target,
            "generator_cpu_cores": round(gen_cpu, 3),
            "sent_to_target": round(statistics.fmean(sent_n[s] for s in win) / target, 4),
            "window_from_s": a, "window_to_s": b,
            "sent_rps_mean": round(statistics.fmean(sent_n[s] for s in win), 2),
            "achieved_rps_mean": round(mean, 2),
            "achieved_rps_std": round(statistics.stdev(ach), 2) if len(ach) > 1 else 0.0,
            "achieved_to_target": round(mean / target, 4),
            "n_2xx": n_ok, "n_non2xx": n_non,
            "non2xx_share_pct": round(100.0 * n_non / (n_ok + n_non), 3) if n_ok + n_non else 0.0,
            "n_conn_errors": sum(conn_n[s] for s in win),
            "client_p50_ms": None if not wl else round(percentile(wl, 50), 2),
            "client_p95_ms": None if not wl else round(percentile(wl, 95), 2),
            "client_p99_ms": None if not wl else round(percentile(wl, 99), 2),
        })

    best = max(steps, key=lambda s: s["achieved_rps_mean"])
    sent_ok = all(abs(st["sent_to_target"] - 1.0) <= 0.005 for st in steps)
    lags = sorted(x for r in results for x in r["lags"])
    summary = {
        "X": best["achieved_rps_mean"],
        "X_std": best["achieved_rps_std"],
        "X_step": best["step"],
        "X_step_target_rps": best["target_rps"],
        "last_step_achieved_to_target": steps[-1]["achieved_to_target"],
        "last_step_reached_0_9": steps[-1]["achieved_to_target"] >= 0.9,
        "g_limit": args.g_limit,
        "X_ge_0_9_g": (best["achieved_rps_mean"] >= 0.9 * args.g_limit) if args.g_limit else None,
        "sched_lag_p99_ms": None if not lags else round(percentile(lags, 99), 2),
        "sched_lag_max_ms": None if not lags else round(lags[-1], 2),
        "conn_error_kinds": err_kinds,
        # перевірка межі G: на кожній сходинці надіслано стільки, скільки задано (допуск 0,5 %)
        "sent_equals_target_all_steps": sent_ok,
        "generator_cpu_cores_X_step": best["generator_cpu_cores"],
        "generator_cpus": cfg.get("cpus"),
        # процесорний час усіх процесів генератора, поділений на тривалість прогону
        "generator_cpu_cores_avg": round(
            sum(r["cpu_s"] for r in results) / (warm + steps_done * step_s), 2),   # лише за весь прогін; для звіту — generator_cpu_cores_X_step
    }
    return seconds, steps, summary


def cmd_run(args):
    target = args.target
    label = args.label or ("step_noop" if target == "noop" else f"step_{args.instances}web")
    path = os.path.join(RESULTS_DIR, f"{label}.json")
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"{label}: файл уже є (--force, щоб перезняти)")
    if target == "by-id" and args.instances < 1:
        raise SystemExit("--instances має бути не менше 1 для сценарію by-id")

    print(f"=== {label} ({'GET /noop' if target == 'noop' else 'GET /appointments/{id}'}) ===")
    cfg, procs, start_wall, steps_done, stop_reason, results = run_profile(args, target)
    seconds, steps, summary = analyze(args, cfg, steps_done, results)

    out = {
        "label": label,
        "config": {
            "target": target, "instances": args.instances, "base_url": args.base,
            "warmup_s": args.warmup, "step_s": args.step_seconds,
            "transient_s": args.transient, "start_rps": args.start_rps,
            "step_rps": args.step_rps, "timeout_s": args.timeout, "processes": procs,
            "g_limit": args.g_limit, "max_rps": args.max_rps, "cpus": cfg.get("cpus"),
            "cpu_count": os.cpu_count(), "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "start_epoch": round(start_wall, 3),
        "end_epoch": round(start_wall + args.warmup + steps_done * args.step_seconds, 3),
        "started_utc": datetime.fromtimestamp(start_wall, timezone.utc).isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "summary": summary,
        "steps": steps,
        "seconds": seconds,
    }
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    csv_path = os.path.join(RESULTS_DIR, f"{label}_seconds.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(seconds[0].keys()))
        wr.writeheader()
        wr.writerows(seconds)

    print(f"\nПричина зупинки: {stop_reason}")
    print("| Сходинка | Задано | Надіслано | Досягнуто | σ | досягнуто/задано | не-2xx, % | помилок зʼєднання | p50 | p95 | p99, мс | CPU генератора, ядер |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in steps:
        print(f"| {s['step']} | {s['target_rps']} | {s['sent_rps_mean']} | {s['achieved_rps_mean']} "
              f"| {s['achieved_rps_std']} | {s['achieved_to_target']} | {s['non2xx_share_pct']} "
              f"| {s['n_conn_errors']} | {s['client_p50_ms']} | {s['client_p95_ms']} | {s['client_p99_ms']} "
              f"| {s['generator_cpu_cores']} |")
    print(f"\nX = {summary['X']} req/s (σ = {summary['X_std']}), сходинка {summary['X_step']} "
          f"(задано {summary['X_step_target_rps']})")
    if target == "noop":
        print(f"Межа генератора G = {summary['X']}  ->  далі: --g-limit {summary['X']}")
    else:
        if summary["last_step_reached_0_9"]:
            print("УВАГА: на останній сходинці досягнуто не менше 0,9 заданої: точка обмежена генератором.")
        if summary["X_ge_0_9_g"]:
            print("УВАГА: X(N) >= 0,9·G: точка обмежена генератором.")
    print(f"CPU генератора на сходинці X: {summary['generator_cpu_cores_X_step']} ядра "
          f"(за весь прогін у середньому {summary['generator_cpu_cores_avg']}).")
    if summary["sent_equals_target_all_steps"]:
        print("Перевірка G: надіслано = задано на всіх сходинках (допуск 0,5 %).")
    else:
        bad = [f"{st['step']}: {st['sent_rps_mean']} з {st['target_rps']}" for st in steps
               if abs(st["sent_to_target"] - 1.0) > 0.005]
        print("УВАГА: генератор відстав від розкладу на сходинках (надіслано з заданого): " + "; ".join(bad))
        print("       дайте генератору більше окремих ядер (--cpus) або зменште --procs.")
    lag = summary["sched_lag_p99_ms"]
    if lag is not None and lag > 5:
        print(f"УВАГА: запізнення планувальника p99 = {lag} мс (>5 мс): генератор або хост перевантажені.")
    if summary["conn_error_kinds"]:
        print(f"Види помилок зʼєднання: {summary['conn_error_kinds']}")
    print(f"Збережено: {os.path.relpath(path, REPO_ROOT)}, {os.path.relpath(csv_path, REPO_ROOT)}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="один ступінчастий прогін")
    r.add_argument("--base", default="http://localhost:8080", help="адреса балансувальника")
    r.add_argument("--instances", type=int, required=True,
                   help="скільки екземплярів web піднято (для мітки й метаданих; 0 для noop)")
    r.add_argument("--target", choices=["by-id", "noop"], default="by-id",
                   help="by-id: GET /appointments/{id}; noop: ендпоінт балансувальника без web")
    r.add_argument("--g-limit", type=float, default=0.0,
                   help="межа генератора G, req/s: прогін зупиниться після сходинки, що її досягла")
    r.add_argument("--warmup", type=float, default=10.0)
    r.add_argument("--step-seconds", type=float, default=20.0)
    r.add_argument("--transient", type=float, default=5.0,
                   help="початок сходинки, що не враховується, с")
    r.add_argument("--start-rps", type=int, default=200)
    r.add_argument("--step-rps", type=int, default=200)
    r.add_argument("--max-rps", type=int, default=12000, help="запобіжник")
    r.add_argument("--timeout", type=float, default=5.0, help="тайм-аут запиту, с")
    r.add_argument("--procs", type=int, default=0,
                   help="кількість процесів (типово: кількість ядер з --cpus, а без нього — усі ядра)")
    r.add_argument("--cpus", default="",
                   help="логічні ядра, до яких прив'язати генератор, напр. 4,5 (нумерація з 0); окремо від ядер стенда")
    r.add_argument("--label", default="", help="мітка результату (типово step_<N>web або step_noop)")
    r.add_argument("--force", action="store_true", help="перезаписати наявний результат")
    r.set_defaults(func=cmd_run)
    return p


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    cli_args = build_parser().parse_args()
    cli_args.func(cli_args)
