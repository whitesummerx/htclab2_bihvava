#!/usr/bin/env python3
"""Лаб. 2 (варіант 3): аналіз прогонів. Лише stdlib + matplotlib.

Запуск:  python lab2_analysis.py [--data data] [--out out] [--pool 10]
Вхід:    step_<N>web.json, prom_<N>web.json, docker_stats_4web.txt (опційно)
Вихід:   out/*.png, out/summary.json, out/tables.md
"""
import argparse, bisect, json, math, re
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
C1, C2, C3, INK, MUTED = "#2a78d6", "#eb6834", "#1baf7a", "#0b0b0b", "#52514e"


# ---------------------------------------------------------------- Prometheus
def interp(vals, t):
    ts = [v[0] for v in vals]
    if t <= ts[0]:
        return float(vals[0][1])
    if t >= ts[-1]:
        return float(vals[-1][1])
    i = bisect.bisect_right(ts, t) - 1
    t0, v0 = vals[i]; t1, v1 = vals[i + 1]
    return float(v0) + (float(v1) - float(v0)) * (t - t0) / (t1 - t0)


def inc(series, t0, t1):
    """Приріст лічильника за [t0,t1], сума по інстансах."""
    return sum(interp(s, t1) - interp(s, t0) for s in series)


def web(series):
    return [r["values"] for r in series if r["metric"].get("job") == "web"]


def quantile(prom, t0, t1, q=0.99):
    """histogram_quantile з лінійною інтерполяцією; повертає (оцінка, lo, hi, ">" )."""
    byle = {}
    for r in prom["series"]["duration_bucket"]:
        byle.setdefault(r["metric"]["le"], []).append(r["values"])
    cum = [inc(byle[("%g" % b) if "%g" % b in byle else str(b)], t0, t1) for b in BUCKETS]
    total = inc(byle["+Inf"], t0, t1)
    if total <= 0:
        return None
    target = q * total
    prev_c, prev_b = 0.0, 0.0
    for b, c in zip(BUCKETS, cum):
        if c >= target:
            est = prev_b + (b - prev_b) * (target - prev_c) / max(c - prev_c, 1e-9)
            return est, prev_b, b, False
        prev_c, prev_b = c, b
    return BUCKETS[-1], BUCKETS[-1], math.inf, True   # "> 1000 мс"


def pg_state(prom, state, t0, t1):
    rows = [r["values"] for r in prom["series"]["pg_connections"]
            if r["metric"].get("datname") == "appointments_db" and r["metric"].get("state") == state]
    ts = sorted({v[0] for r in rows for v in r if t0 <= v[0] <= t1})
    if not ts:
        return None
    return sum(sum(interp(r, t) for r in rows) for t in ts) / len(ts)


def pg_xact(prom, t0, t1):
    rows = [r["values"] for k in ("pg_xact_commit", "pg_xact_rollback")
            for r in prom["series"][k] if r["metric"].get("datname") == "appointments_db"]
    return inc(rows, t0, t1) / (t1 - t0)


def gauge_mean(series, t0, t1):
    out = []
    for s in series:
        x = [float(v[1]) for v in s if t0 <= v[0] <= t1]
        if x:
            out.append((sum(x) / len(x), max(x)))
    return out


# ---------------------------------------------------------------- per-run
def analyse_run(n, data, pool):
    s = json.load(open(data / f"step_{n}web.json"))
    p = json.load(open(data / f"prom_{n}web.json"))
    t_start = s["start_epoch"]
    cpu = web(p["series"]["cpu_seconds"])
    req = web(p["series"]["requests_total"])
    inprog = web(p["series"]["in_progress"])
    base = pg_xact(p, t_start - 25, t_start - 2)          # фонові транзакції (експортер)
    rows = []
    for st in s["steps"]:
        a, b = t_start + st["window_from_s"], t_start + st["window_to_s"]
        dt = b - a
        cores = [(interp(c, b) - interp(c, a)) / dt for c in cpu]
        dreq = inc(req, a, b)
        q = quantile(p, a, b)
        if q is not None and dreq / dt < 0.5 * st["target_rps"]:
            q = None            # сервер обробив <50% заданого — гістограма не репрезентативна
        ig = gauge_mean(inprog, a, b)
        rows.append(dict(
            step=st["step"], target=st["target_rps"], achieved=st["achieved_rps_mean"],
            std=st["achieved_rps_std"], ratio=st["achieved_to_target"],
            non2xx_pct=st["non2xx_share_pct"], conn_err=st["n_conn_errors"],
            client_p50=st["client_p50_ms"], client_p95=st["client_p95_ms"], client_p99=st["client_p99_ms"],
            srv_p99=None if q is None else q[0] * 1000,
            srv_lo=None if q is None else q[1] * 1000,
            srv_hi=None if q is None else (None if q[2] == math.inf else q[2] * 1000),
            srv_open=False if q is None else q[3],
            srv_rps=dreq / dt,
            cpu_per_inst=sum(cores) / max(len(cores), 1), cpu_max=max(cores), cpu_sum=sum(cores),
            cpu_ms_per_req=(sum(cores) * dt / dreq * 1000) if dreq > 50 else None,
            inprog_mean=(sum(m for m, _ in ig) / len(ig)) if ig else None,
            inprog_max=max((x for _, x in ig), default=None),
            pg_active=pg_state(p, "active", a, b), pg_idle=pg_state(p, "idle", a, b),  # включно з 1+1 з'єднаннями самого експортера
            pg_tps=pg_xact(p, a, b) - base,
        ))
    return s, p, rows


def x_of(s):
    return s["summary"]["X"], s["summary"]["X_std"]


# ---------------------------------------------------------------- USL
def usl_fit(Ns, Cs):
    """C(N)=N/(1+s(N-1)+k N(N-1)); МНК за лінеаризацією N/C-1 = s(N-1)+k N(N-1)."""
    y = [n / c - 1 for n, c in zip(Ns, Cs)]
    a1 = [n - 1 for n in Ns]; a2 = [n * (n - 1) for n in Ns]
    s11 = sum(x * x for x in a1); s12 = sum(x * z for x, z in zip(a1, a2)); s22 = sum(z * z for z in a2)
    b1 = sum(x * v for x, v in zip(a1, y)); b2 = sum(z * v for z, v in zip(a2, y))
    det = s11 * s22 - s12 ** 2
    sg, kp = (b1 * s22 - b2 * s12) / det, (s11 * b2 - s12 * b1) / det
    note = "двопараметрична"
    if kp < 0:
        kp, note = 0.0, "κ<0 → κ=0, лише σ (формула Амдала)"
        sg = b1 / s11
    if sg < 0:
        sg, note = 0.0, note + "; σ<0 → 0"
    nstar = math.sqrt((1 - sg) / kp) if kp > 0 else math.inf
    return sg, kp, nstar, note


def usl(n, sg, kp):
    return n / (1 + sg * (n - 1) + kp * n * (n - 1))


# ---------------------------------------------------------------- docker stats
def parse_stats(path):
    out, cur = [], None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if re.match(r"\d{4}-\d\d-\d\dT", line):
            cur = dict(t=datetime.fromisoformat(line).timestamp(), cpu={}, mem={})
            out.append(cur)
        elif cur is not None and line.startswith("generator_ws_MB"):
            cur["gen_mb"] = float(line.split(":")[1])
        elif cur is not None and line:
            m = re.match(r"(\S+)\s+([\d.]+)%\s+([\d.]+)(MiB|GiB)", line)
            if m:
                cur["cpu"][m[1]] = float(m[2])
                cur["mem"][m[1]] = float(m[3]) * (1024 if m[4] == "GiB" else 1)
    return out


# ---------------------------------------------------------------- plots
def style():
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": "#c9c8c3", "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True,
                         "grid.color": "#e6e5e1", "grid.linewidth": 0.6, "axes.spines.top": False,
                         "axes.spines.right": False, "figure.dpi": 130, "axes.titlesize": 10,
                         "axes.titleweight": "bold", "axes.titlelocation": "left"})


def plot_profile(n, s, p, rows, out):
    """Профіль прогону: 4 панелі по сходинках (N=1 / N=4)."""
    x = [r["target"] for r in rows]
    fig, ax = plt.subplots(2, 2, figsize=(11, 7))
    a = ax[0, 0]
    a.plot([0, max(x)], [0, max(x)], color="#9a9993", lw=1, ls="--", label="ідеал (задано)")
    a.errorbar(x, [r["achieved"] for r in rows], yerr=[r["std"] for r in rows], color=C1, marker="o", lw=2, capsize=3, label="досягнуто (середнє ± σ)")
    a.set_title(f"N={n}: досягнута інтенсивність"); a.set_xlabel("задана, req/s"); a.set_ylabel("req/s"); a.legend(frameon=False)
    a = ax[0, 1]
    a.plot(x, [r["client_p99"] for r in rows], color=C2, marker="o", lw=2, label="клієнт p99")
    lo = [r["srv_lo"] if r["srv_lo"] is not None else float("nan") for r in rows]
    hi = [r["srv_hi"] if r["srv_hi"] is not None else float("nan") for r in rows]
    mid = [r["srv_p99"] if r["srv_p99"] is not None else float("nan") for r in rows]
    a.plot(x, mid, color=C1, marker="s", lw=2, label="сервер p99 (інтерполяція)")
    a.fill_between(x, lo, [h if not math.isnan(h) else 1000 for h in hi], color=C1, alpha=0.15, label="межі бакета")
    for xi, r in zip(x, rows):
        if r["srv_open"]:
            a.annotate("> 1000", (xi, 1000), fontsize=7, color=MUTED, xytext=(0, 4), textcoords="offset points", ha="center")
    a.set_yscale("log"); a.set_title("p99: клієнт vs сервер"); a.set_xlabel("задана, req/s"); a.set_ylabel("мс"); a.legend(frameon=False, fontsize=8)
    a = ax[1, 0]
    a.bar([i - 20 for i in x], [r["cpu_per_inst"] for r in rows], width=40, color=C1, label="середнє по інстансах")
    a.bar([i + 20 for i in x], [r["cpu_max"] for r in rows], width=40, color=C3, label="максимум інстанса")
    a.axhline(1.0, color=C2, lw=1.2, ls="--"); a.text(x[0], 1.02, "ліміт 1 CPU", color=MUTED, fontsize=8)
    a.set_title("CPU web на інстанс"); a.set_xlabel("задана, req/s"); a.set_ylabel("ядер"); a.legend(frameon=False, fontsize=8)
    a = ax[1, 1]
    a.plot(x, [r["inprog_mean"] or 0 for r in rows], color=C1, marker="o", lw=2, label="середнє")
    a.plot(x, [r["inprog_max"] or 0 for r in rows], color=C2, marker="o", lw=2, label="максимум зі зрізів")
    a2 = a.twinx() if False else None
    a.set_title("запитів у польоті на інстанс"); a.set_xlabel("задана, req/s"); a.set_ylabel("in-flight"); a.legend(frameon=False, fontsize=8)
    fig.suptitle(f"Лаб. 2, N={n}: профіль по сходинках (вікно — останні 15 с сходинки)", x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(out / f"profile_N{n}.png"); plt.close(fig)


def plot_timeline(n, s, p, out):
    sec = s["seconds"]; t0 = s["start_epoch"]
    t = [r["second"] for r in sec]
    fig, ax = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    ax[0].plot(t, [r["target_rps"] for r in sec], color="#9a9993", ls="--", lw=1, label="задано")
    ax[0].plot(t, [r["achieved_rps"] for r in sec], color=C1, lw=1.6, label="2xx/с")
    ax[0].plot(t, [r["n_non2xx"] + r["n_conn_errors"] for r in sec], color=C2, lw=1.2, label="не-2xx + помилки з'єднання /с")
    ax[0].set_ylabel("req/s"); ax[0].legend(frameon=False, ncol=3, fontsize=8); ax[0].set_title(f"N={n}: по секундах")
    ax[1].semilogy(t, [max(r["p99_ms"], 0.1) for r in sec], color=C2, lw=1.2, label="клієнт p99 за секунду")
    pts = []
    for k in range(0, int(len(sec)) - 5, 5):
        q = quantile(p, t0 + k, t0 + k + 5)
        if q:
            pts.append((k + 2.5, q[0] * 1000))
    ax[1].plot([a for a, _ in pts], [b for _, b in pts], color=C1, marker="s", lw=1.6, label="сервер p99 за 5 с")
    ax[1].set_ylabel("мс"); ax[1].legend(frameon=False, fontsize=8)
    cpu = web(p["series"]["cpu_seconds"])
    for i, c in enumerate(cpu):
        xs, ys = [], []
        for k in range(0, len(sec) - 5, 5):
            xs.append(k + 2.5); ys.append((interp(c, t0 + k + 5) - interp(c, t0 + k)) / 5)
        ax[2].plot(xs, ys, lw=1.4, label=f"web-{i + 1}")
    ax[2].axhline(1.0, color="#9a9993", ls="--", lw=1); ax[2].set_ylabel("CPU, ядер"); ax[2].set_xlabel("секунда прогону"); ax[2].legend(frameon=False, ncol=4, fontsize=8)
    for a in ax:
        for k in range(10, len(sec), 20):
            a.axvline(k, color="#e6e5e1", lw=0.8, zorder=0)
    fig.tight_layout(); fig.savefig(out / f"timeline_N{n}.png"); plt.close(fig)


def plot_xn(Ns, X, Xstd, sg, kp, out):
    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    nn = [1 + i * 0.05 for i in range(0, 81)]
    ax.plot(nn, [X[0] * n for n in nn], color="#9a9993", ls="--", lw=1.2, label=f"лінійне λN, λ=X(1)={X[0]:.0f}")
    ax.plot(nn, [X[0] * usl(n, sg, kp) for n in nn], color=C2, lw=2, label=f"USL: σ={sg:.2f}, κ={kp:.3f}")
    ax.errorbar(Ns, X, yerr=Xstd, color=C1, marker="o", lw=0, elinewidth=2, capsize=4, ms=7, label="X(N) ± σ по секундах")
    for n, x in zip(Ns, X):
        ax.annotate(f"{x:.0f}", (n, x), xytext=(6, -12), textcoords="offset points", fontsize=8, color=MUTED)
    ax.set_xticks(Ns); ax.set_xlabel("кількість інстансів N"); ax.set_ylabel("X(N), req/s"); ax.set_ylim(0, X[0] * 4.4)
    ax.set_title("Межа масштабованості: X(N)", loc="left"); ax.legend(frameon=False, fontsize=8)
    fig.tight_layout(); fig.savefig(out / "xn_usl.png"); plt.close(fig)


def plot_m2(runs, pool, out):
    fig, ax = plt.subplots(2, 2, figsize=(11, 7))
    for j, n in enumerate((1, 4)):
        rows = runs[n][2]; x = [r["target"] for r in rows]
        a = ax[0, j]
        a.plot(x, [r["pg_active"] for r in rows], color=C2, marker="o", lw=2, label="active")
        a.plot(x, [r["pg_idle"] for r in rows], color=C1, marker="o", lw=2, label="idle")
        a.axhline(n * pool, color="#9a9993", ls="--", lw=1.2, label=f"N×пул = {n * pool}")
        a.set_title(f"N={n}: з'єднання до БД за станами"); a.set_xlabel("задана, req/s"); a.set_ylabel("з'єднань"); a.legend(frameon=False, fontsize=8)
        a = ax[1, j]
        a.plot([0, max(x)], [0, max(x)], color="#9a9993", ls="--", lw=1.2, label="ціль")
        a.plot(x, [r["pg_tps"] for r in rows], color=C1, marker="o", lw=2, label="транзакцій/с (commit+rollback)")
        a.set_title(f"N={n}: транзакції Postgres vs задана"); a.set_xlabel("задана, req/s"); a.set_ylabel("tx/s"); a.legend(frameon=False, fontsize=8)
    fig.suptitle("М2 (postgres-exporter)", x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout(); fig.savefig(out / "m2_postgres.png"); plt.close(fig)


def plot_cpu_per_req(runs, out):
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    cols = {1: C1, 2: C2, 3: C3, 4: "#6250d6"}
    for n in (1, 2, 3, 4):
        rows = [r for r in runs[n][2] if r["cpu_ms_per_req"] and r["ratio"] > 0.95]
        ax.plot([r["target"] for r in rows], [r["cpu_ms_per_req"] for r in rows], marker="o", lw=2, color=cols[n], label=f"N={n}")
    ax.set_xlabel("задана, req/s"); ax.set_ylabel("CPU-мс web на запит"); ax.set_title("Вартість запиту в CPU залежно від N", loc="left")
    ax.legend(frameon=False); fig.tight_layout(); fig.savefig(out / "cpu_per_request.png"); plt.close(fig)


# ---------------------------------------------------------------- CPU хоста (typeperf)
def parse_typeperf(path, tz_offset_h):
    """Рядки "MM/DD/YYYY HH:MM:SS.mmm","значення" (локальний час) -> [(epoch_utc, %)]."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r'"(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)\.(\d+)","([\d.]+)"', line)
        if m:
            mo, da, y, h, mi, sec, ms, v = m.groups()
            t = datetime(int(y), int(mo), int(da), int(h), int(mi), int(sec), int(ms) * 1000)
            rows.append(((t - datetime(1970, 1, 1)).total_seconds() - tz_offset_h * 3600, float(v)))
    return rows


def host_cpu_steps(rows, start, warmup=10, step_s=20, transient=5, nsteps=7):
    out = []
    for k in range(1, nsteps + 1):
        a = start + warmup + step_s * (k - 1) + transient; b = start + warmup + step_s * k
        x = [v for t, v in rows if a <= t < b]
        out.append(dict(step=k, target=200 * k, n=len(x), mean=sum(x) / len(x) if x else None, max=max(x) if x else None))
    return out


def plot_host_cpu(rows, start, steps, achieved, out, warmup=10, step_s=20):
    fig, ax = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True, gridspec_kw=dict(height_ratios=[1, 1.3]))
    for k in range(len(achieved)):
        x0 = warmup + step_s * k
        ax[0].hlines(achieved[k], x0, x0 + step_s, color=C1, lw=3, label="досягнуто (середнє за сходинку)" if k == 0 else None)
        ax[0].hlines(200 * (k + 1), x0, x0 + step_s, color="#9a9993", ls="--", lw=1.2, label="задано" if k == 0 else None)
    ax[0].set_ylabel("req/s"); ax[0].set_title("N=4: інтенсивність і завантаження процесора хоста", loc="left"); ax[0].legend(frameon=False, fontsize=8, loc="upper left")
    t = [r[0] - start for r in rows]; v = [r[1] for r in rows]
    ax[1].plot(t, v, color=C2, lw=1.8, label="CPU хоста, %")
    ax[1].axhline(100, color="#9a9993", ls="--", lw=1.2)
    for k, st in enumerate(steps):
        if st["mean"] is not None:
            x0 = warmup + step_s * k + 5
            ax[1].hlines(st["mean"], x0, x0 + step_s - 5, color=C1, lw=3, label="середнє за вікно сходинки" if k == 0 else None)
    for k in range(len(achieved) + 1):
        ax[1].axvline(warmup + step_s * k, color="#e6e5e0", lw=0.8, zorder=0)
    ax[1].set_ylim(0, 105); ax[1].set_ylabel("% (усі логічні ядра)"); ax[1].set_xlabel("секунда прогону"); ax[1].legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout(); fig.savefig(out / "host_cpu_N4.png"); plt.close(fig)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data"); ap.add_argument("--out", default="out"); ap.add_argument("--pool", type=int, default=10)
    ap.add_argument("--host-cpu", default="host_cpu_4web.txt", help="вивід typeperf під час прогону N=4 (у --data)")
    ap.add_argument("--host-start", default="2026-10-29T00:40:18+00:00", help="мітка «Старт» цього прогону (UTC)")
    ap.add_argument("--host-tz", type=float, default=2.0, help="зсув локального часу typeperf відносно UTC, год")
    ap.add_argument("--host-achieved", default="200.07,400.07,600.13,800.4,999.6,692.4,260.87", help="досягнуто по сходинках цього прогону")
    a = ap.parse_args()
    data, out = Path(a.data), Path(a.out); out.mkdir(exist_ok=True)
    style()
    runs = {n: analyse_run(n, data, a.pool) for n in (1, 2, 3, 4)}
    Ns = [1, 2, 3, 4]
    X = [x_of(runs[n][0])[0] for n in Ns]; Xs = [x_of(runs[n][0])[1] for n in Ns]
    C = [x / X[0] for x in X]
    sg, kp, nstar, note = usl_fit(Ns, C)
    G = json.load(open(data / "step_noop.json"))["summary"]["X"]
    summary = dict(X=dict(zip(Ns, X)), X_std=dict(zip(Ns, Xs)), C=dict(zip(Ns, C)), sigma=sg, kappa=kp,
                   N_star=None if math.isinf(nstar) else nstar, fit_note=note, G=G)
    summary["usl_C"] = {n: usl(n, sg, kp) for n in Ns}
    # похибка квантування сходинки 200 req/s
    summary["quantization"] = "X(N) визначено з кроком сходинки 200 req/s: істинна межа в [X; X+200)"
    for n in Ns:
        plot_timeline(n, runs[n][0], runs[n][1], out) if n in (1, 4) else None
        if n in (1, 4):
            plot_profile(n, runs[n][0], runs[n][1], runs[n][2], out)
    plot_xn(Ns, X, Xs, sg, kp, out); plot_m2(runs, a.pool, out); plot_cpu_per_req(runs, out)
    # docker stats
    ds = data / "docker_stats_4web.txt"
    if ds.exists():
        samples = parse_stats(ds); s4 = runs[4][0]
        st5 = [st for st in s4["steps"] if st["step"] == 5][0]
        a0, b0 = s4["start_epoch"] + st5["window_from_s"] - 1, s4["start_epoch"] + st5["window_to_s"]
        sel = [x for x in samples if a0 <= x["t"] <= b0]
        names = sorted({k for x in sel for k in x["cpu"]})
        mean = {k: sum(x["cpu"].get(k, 0) for x in sel) / len(sel) for k in names}
        summary["docker_stats_X4"] = dict(samples=len(sel), mean_cpu_pct=mean, sum_cores=sum(mean.values()) / 100,
                                         web_cores=sum(v for k, v in mean.items() if "web" in k) / 100,
                                         gen_mb=sum(x.get("gen_mb", 0) for x in sel) / len(sel))
    hc = data / a.host_cpu
    if hc.exists():
        start = datetime.fromisoformat(a.host_start).timestamp()
        hrows = parse_typeperf(hc, a.host_tz); hsteps = host_cpu_steps(hrows, start)
        ach = [float(x) for x in a.host_achieved.split(",")]
        plot_host_cpu(hrows, start, hsteps, ach, out)
        summary["host_cpu_N4"] = [dict(h, achieved=ach[i] if i < len(ach) else None) for i, h in enumerate(hsteps)]
        T = ["\n### CPU хоста при N=4 (typeperf, вікно: останні 15 с сходинки)\n", "| сх. | задано | досягнуто | CPU хоста, сер. % | макс. % |", "|---|---|---|---|---|"]
        for i, h in enumerate(hsteps):
            T.append(f"| {h['step']} | {h['target']} | {ach[i]:.1f} | {h['mean']:.1f} | {h['max']:.0f} |")
        host_md = "\n".join(T)
    else:
        host_md = ""
    json.dump(summary, open(out / "summary.json", "w"), ensure_ascii=False, indent=1, default=str)
    # таблиці
    L = []
    for n in Ns:
        L.append(f"\n### N={n}\n\n| сх. | задано | досягнуто ± σ | не-2xx % | p99 клієнт | p99 сервер (межі) | CPU/інст. (сер/макс) | in-flight | pg act/idle | tx/с | CPU-мс/запит |\n|---|---|---|---|---|---|---|---|---|---|---|")
        for r in runs[n][2]:
            sv = "—" if r["srv_p99"] is None else (f"> 1000" if r["srv_open"] else f"{r['srv_p99']:.1f} [{r['srv_lo']:.0f}–{r['srv_hi']:.0f}]")
            f = lambda v, d=1: "—" if v is None else f"{v:.{d}f}"
            L.append(f"| {r['step']} | {r['target']} | {r['achieved']:.1f} ± {r['std']:.1f} | {r['non2xx_pct']} | {r['client_p99']} | {sv} | {r['cpu_per_inst']:.2f}/{r['cpu_max']:.2f} | {f(r['inprog_mean'])}/{f(r['inprog_max'],0)} | {f(r['pg_active'])}/{f(r['pg_idle'])} | {r['pg_tps']:.0f} | {f(r['cpu_ms_per_req'],2)} |")
    (out / "tables.md").write_text("\n".join(L) + host_md, encoding="utf-8")
    json.dump({n: runs[n][2] for n in Ns}, open(out / "rows.json", "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(summary, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
