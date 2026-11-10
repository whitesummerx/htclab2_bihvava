#!/usr/bin/env python3
"""Лаб. 2 (варіант 3): аналіз прогонів. Лише stdlib + matplotlib.

Запуск:  python lab2_analysis.py [--data data] [--out out] [--pool 10]
Вхід:    step_<N>web.json, prom_<N>web.json, step_noop.json, docker_stats_4web.txt і host_cpu_4web.txt
         (останні два — з ТОГО САМОГО прогону N=4, що й step_4web.json; їхній час перевіряється)
Вихід:   out/*.png, out/summary.json, out/tables.md
"""
import argparse, bisect, json, math, re
from datetime import datetime, timezone
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


def by_instance(rows):
    """{instance: [values, ...]} для рядів job=web (кілька рядів одного instance сумуються)."""
    out = {}
    for r in rows:
        if r["metric"].get("job") == "web":
            out.setdefault(r["metric"]["instance"], []).append(r["values"])
    return dict(sorted(out.items()))


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
    s = json.load(open(data / f"step_{n}web.json", encoding="utf-8"))
    p = json.load(open(data / f"prom_{n}web.json", encoding="utf-8"))
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
        q = quantile(p, a, b)   # без відкидання сходинок: оцінка за приростами бакетів є завжди, коли були відповіді
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
            srv_err=None if q is None or q[3] else (q[2] - q[1]) * 1000,   # похибка = ширина бакета b(i) - b(i-1)
            sent=st.get("sent_rps_mean"), sent_ratio=st.get("sent_to_target"),
            gen_cpu=st.get("generator_cpu_cores"),
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
    """C(N)=N/(1+s(N-1)+k N(N-1)); МНК за лінеаризацією y = s*u + k*v,
    y = N/C - 1, u = N - 1, v = N(N-1). Нормальні рівняння:
        s*Σu² + k*Σuv = Σuy,   s*Σuv + k*Σv² = Σvy.
    Повертає (σ, κ, N*, примітка, деталі для звіту)."""
    pts = [(n, c) for n, c in zip(Ns, Cs) if n > 1]          # N=1 дає u=v=y=0 і нічого не додає
    rows = [dict(N=n, C=c, y=n / c - 1, u=n - 1, v=n * (n - 1)) for n, c in pts]
    S = dict(uu=sum(r["u"] ** 2 for r in rows), vv=sum(r["v"] ** 2 for r in rows),
             uv=sum(r["u"] * r["v"] for r in rows), uy=sum(r["u"] * r["y"] for r in rows),
             vy=sum(r["v"] * r["y"] for r in rows))
    det = S["uu"] * S["vv"] - S["uv"] ** 2
    sg = (S["uy"] * S["vv"] - S["vy"] * S["uv"]) / det
    kp = (S["uu"] * S["vy"] - S["uv"] * S["uy"]) / det
    note = "двопараметрична"
    sg2, kp2 = sg, kp
    if kp < 0:
        kp2, note = 0.0, "κ<0 → κ=0, σ перераховано лише за u"
        sg2 = S["uy"] / S["uu"]
    if sg2 < 0:
        sg2, note = 0.0, note + "; σ<0 → σ=0"
        kp2 = max(S["vy"] / S["vv"], 0.0)
        if kp >= 0:
            note = "σ<0 → σ=0, κ перераховано лише за v"
    nstar = math.sqrt((1 - sg2) / kp2) if kp2 > 0 else math.inf
    return sg2, kp2, nstar, note, dict(rows=rows, S=S, det=det, sg_raw=sg, kp_raw=kp)


def usl(n, sg, kp):
    return n / (1 + sg * (n - 1) + kp * n * (n - 1))


# ---------------------------------------------------------------- docker stats
def parse_stats(path):
    out, cur = [], None
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    for line in text.splitlines():
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
    """Прогін по секундах. Панелі на спільній осі часу:
    1) задано / надіслано / 2xx / не-2xx; 2) p99 клієнта і p99 сервера (вікно 15 с, оновлення кожні 5 с);
    3) CPU кожного інстанса; 4) запити/с кожного інстанса (приріст http_requests_total);
    5) запити в обробці кожного інстанса (http_requests_in_progress)."""
    sec = s["seconds"]; t0 = s["start_epoch"]; T = len(sec)
    t = [r["second"] for r in sec]
    fig, ax = plt.subplots(5, 1, figsize=(11, 15), sharex=True)
    ax[0].plot(t, [r["target_rps"] for r in sec], color="#9a9993", ls="--", lw=1, label="задано")
    ax[0].plot(t, [r["sent_rps"] for r in sec], color=C3, lw=1.0, label="надіслано/с")
    ax[0].plot(t, [r["achieved_rps"] for r in sec], color=C1, lw=1.6, label="2xx/с")
    ax[0].plot(t, [r["n_non2xx"] + r["n_conn_errors"] for r in sec], color=C2, lw=1.2, label="не-2xx + помилки з'єднання /с")
    ax[0].set_ylabel("req/s"); ax[0].legend(frameon=False, ncol=4, fontsize=8); ax[0].set_title(f"N={n}: по секундах")
    ax[1].semilogy(t, [max(r["p99_ms"], 0.1) if r["p99_ms"] is not None else float("nan") for r in sec],
                   color=C2, lw=1.2, label="клієнт p99 за секунду")
    # сервер: вікно 15 с, оновлення кожні 5 с -> відрізки [t0+k, t0+k+15], k = 0, 5, 10, ...
    xs, ys, ox, oy = [], [], [], []
    for k in range(0, T - 15 + 1, 5):
        q = quantile(p, t0 + k, t0 + k + 15)
        if q:
            (ox if q[3] else xs).append(k + 15)          # точка - кінець вікна (момент, коли значення стає відоме)
            (oy if q[3] else ys).append(1000.0 if q[3] else q[0] * 1000)
    ax[1].plot(xs, ys, color=C1, marker="s", lw=0, ms=4, label="сервер p99, вікно 15 с, крок 5 с")
    if ox:
        ax[1].plot(ox, oy, color=C1, marker="^", lw=0, ms=6, mfc="none", label="сервер p99 > 1000 мс")
    ax[1].set_ylabel("мс"); ax[1].legend(frameon=False, fontsize=8, ncol=3)
    cpu = by_instance(p["series"]["cpu_seconds"]); req = by_instance(p["series"]["requests_total"])
    inp = by_instance(p["series"]["in_progress"])
    names = {inst: f"web-{i + 1} ({inst.split(':')[0]})" for i, inst in enumerate(cpu)}
    cols = [C1, C2, C3, "#6250d6", "#9a6700", "#c2185b"]
    for i, (inst, ser) in enumerate(cpu.items()):
        xs = list(range(5, T + 1, 5))
        ax[2].plot(xs, [(interp(ser[0], t0 + k) - interp(ser[0], t0 + k - 5)) / 5 for k in xs], lw=1.4, color=cols[i % 6], label=names[inst])
        if inst in req:
            ax[3].plot(xs, [sum(interp(x, t0 + k) - interp(x, t0 + k - 5) for x in req[inst]) / 5 for k in xs],
                       lw=1.4, color=cols[i % 6], label=names[inst])
        if inst in inp:
            pts = [(v[0] - t0, float(v[1])) for v in inp[inst][0] if 0 <= v[0] - t0 <= T]
            ax[4].plot([a for a, _ in pts], [b for _, b in pts], lw=1.2, marker="o", ms=2.5, color=cols[i % 6], label=names[inst])
    ax[2].axhline(1.0, color="#9a9993", ls="--", lw=1); ax[2].set_ylabel("CPU, ядер"); ax[2].legend(frameon=False, ncol=4, fontsize=8)
    ax[2].set_title("CPU кожного інстанса (приріст process_cpu_seconds_total за 5 с)", fontsize=9)
    ax[3].set_ylabel("запитів/с"); ax[3].legend(frameon=False, ncol=4, fontsize=8)
    ax[3].set_title("запити за секунду кожного інстанса (приріст http_requests_total за 5 с)", fontsize=9)
    ax[4].set_ylabel("у польоті"); ax[4].set_xlabel("секунда прогону"); ax[4].legend(frameon=False, ncol=4, fontsize=8)
    ax[4].set_title("запити в обробці кожного інстанса (http_requests_in_progress, відліки скрейпу)", fontsize=9)
    for a in ax:
        for k in range(10, T, 20):
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


# ---------------------------------------------------------------- CPU хоста (семплер CPU)
def parse_typeperf(path, tz_offset_h):
    """Рядки "MM/DD/YYYY HH:MM:SS.mmm","знач1","знач2",... (локальний час) -> [(epoch_utc, [знач...])]."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        m = re.match(r'"(\d+)/(\d+)/(\d+) (\d+):(\d+):(\d+)\.(\d+)"((?:,"[\d.]*")+)', line)
        if m:
            mo, da, y, h, mi, sec, ms, rest = m.groups()
            t = datetime(int(y), int(mo), int(da), int(h), int(mi), int(sec), int(ms) * 1000)
            vals = [float(v) if v else float("nan") for v in re.findall(r'"([\d.]*)"', rest)]
            rows.append(((t - datetime(1970, 1, 1)).total_seconds() - tz_offset_h * 3600, vals))
    return rows


def host_cpu_steps(rows, start, warmup=10, step_s=20, transient=5, nsteps=7, col=0, cols=None):
    """Середнє і максимум за вікном сходинки (останні 15 с). col - один стовпець; cols - середнє за кількома."""
    out = []
    for k in range(1, nsteps + 1):
        a = start + warmup + step_s * (k - 1) + transient; b = start + warmup + step_s * k
        x = [(sum(v[c] for c in cols) / len(cols)) if cols else v[col] for t, v in rows if a <= t < b]
        out.append(dict(step=k, target=200 * k, n=len(x), mean=sum(x) / len(x) if x else None, max=max(x) if x else None))
    return out


def plot_host_cpu(rows, start, steps, achieved, out, warmup=10, step_s=20):
    fig, ax = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True, gridspec_kw=dict(height_ratios=[1, 1.3]))
    for k in range(len(achieved)):
        x0 = warmup + step_s * k
        ax[0].hlines(achieved[k], x0, x0 + step_s, color=C1, lw=3, label="досягнуто (середнє за сходинку)" if k == 0 else None)
        ax[0].hlines(200 * (k + 1), x0, x0 + step_s, color="#9a9993", ls="--", lw=1.2, label="задано" if k == 0 else None)
    ax[0].set_ylabel("req/s"); ax[0].set_title("N=4: інтенсивність і завантаження процесора хоста", loc="left"); ax[0].legend(frameon=False, fontsize=8, loc="upper left")
    t = [r[0] - start for r in rows]; v = [r[1][0] for r in rows]
    ax[1].plot(t, v, color=C2, lw=1.8, label="CPU хоста, усі логічні ядра, %")
    if rows and len(rows[0][1]) > 1:   # окремі ядра стенда (Docker VM), якщо їх знято
        vm = [sum(r[1][1:]) / (len(r[1]) - 1) for r in rows]
        ax[1].plot(t, vm, color=C3, lw=1.4, label="ядра стенда (Docker VM), сер. %")
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
def uk(v, d=1):
    """Число з десятковою комою для таблиць звіту."""
    return "—" if v is None else f"{v:.{d}f}".replace(".", ",")


def usl_markdown(Ns, X, C, sg, kp, nstar, note, det):
    R, S = det["rows"], det["S"]
    L = ["### Розрахунок USL (метод найменших квадратів)\n",
         "C(N) = X(N)/X(1); y(N) = N/C(N) − 1; u(N) = N − 1; v(N) = N(N − 1). Модель y = σ·u + κ·v (N = 1 дає нулі й не впливає).\n",
         "| N | X(N), req/s | C(N) | y(N) | u(N) | v(N) |", "|---|---|---|---|---|---|"]
    for n, x, c in zip(Ns, X, C):
        if n == 1:
            L.append(f"| 1 | {uk(x, 2)} | {uk(c, 4)} | 0 | 0 | 0 |")
        else:
            r = [q for q in R if q["N"] == n][0]
            L.append(f"| {n} | {uk(x, 2)} | {uk(c, 4)} | {uk(r['y'], 4)} | {r['u']} | {r['v']} |")
    L += ["", "Суми за N = 2, 3, 4:\n",
          "| Σu² | Σv² | Σuv | Σuy | Σvy |", "|---|---|---|---|---|",
          f"| {S['uu']} | {S['vv']} | {S['uv']} | {uk(S['uy'], 4)} | {uk(S['vy'], 4)} |", ""]
    L += ["Нормальні рівняння: σ·Σu² + κ·Σuv = Σuy;  σ·Σuv + κ·Σv² = Σvy. Розв'язок за правилом Крамера:\n",
          f"- Δ = Σu²·Σv² − (Σuv)² = {S['uu']}·{S['vv']} − {S['uv']}² = {uk(det['det'], 0)}",
          f"- σ = (Σuy·Σv² − Σvy·Σuv)/Δ = ({uk(S['uy'], 4)}·{S['vv']} − {uk(S['vy'], 4)}·{S['uv']})/{uk(det['det'], 0)} = {uk(det['sg_raw'], 4)}",
          f"- κ = (Σu²·Σvy − Σuv·Σuy)/Δ = ({S['uu']}·{uk(S['vy'], 4)} − {S['uv']}·{uk(S['uy'], 4)})/{uk(det['det'], 0)} = {uk(det['kp_raw'], 5)}", ""]
    L.append(f"Підсумок ({note}): **σ = {uk(sg, 4)}, κ = {uk(kp, 5)}**, "
             + (f"N* = √((1 − σ)/κ) = {uk(nstar, 2)}." if not math.isinf(nstar) else "κ = 0, тому N* = ∞."))
    return "\n".join(L)


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(here.parent / "results"), help="каталог з step_*.json, prom_*.json, docker stats, host_cpu_4web.txt")
    ap.add_argument("--out", default=str(here / "out")); ap.add_argument("--pool", type=int, default=10)
    ap.add_argument("--host-cpu", default="host_cpu_4web.txt", help="вивід семплера CPU під час прогону N=4 (у --data)")
    ap.add_argument("--host-tz", type=float, default=2.0, help="зсув локального часу семплера CPU відносно UTC, год (Київ узимку = 2)")
    a = ap.parse_args()
    data, out = Path(a.data), Path(a.out); out.mkdir(exist_ok=True)
    style()
    runs = {n: analyse_run(n, data, a.pool) for n in (1, 2, 3, 4)}
    Ns = [1, 2, 3, 4]
    X = [x_of(runs[n][0])[0] for n in Ns]; Xs = [x_of(runs[n][0])[1] for n in Ns]
    C = [x / X[0] for x in X]
    sg, kp, nstar, note, det = usl_fit(Ns, C)
    G = json.load(open(data / "step_noop.json", encoding="utf-8"))["summary"]["X"]
    summary = dict(X=dict(zip(Ns, X)), X_std=dict(zip(Ns, Xs)), C=dict(zip(Ns, C)), sigma=sg, kappa=kp,
                   N_star=None if math.isinf(nstar) else nstar, fit_note=note, G=G,
                   usl_sums=det["S"], usl_rows=det["rows"], sigma_raw=det["sg_raw"], kappa_raw=det["kp_raw"])
    summary["usl_C"] = {n: usl(n, sg, kp) for n in Ns}
    summary["quantization"] = "X(N) визначено з кроком сходинки 200 req/s: істинна межа в [X; X+200)"
    summary["sent_equals_target_all_steps"] = {n: runs[n][0]["summary"].get("sent_equals_target_all_steps") for n in Ns}
    summary["sched_lag_p99_ms"] = {n: runs[n][0]["summary"].get("sched_lag_p99_ms") for n in Ns}
    for n in (1, 4):
        plot_timeline(n, runs[n][0], runs[n][1], out)
        plot_profile(n, runs[n][0], runs[n][1], runs[n][2], out)
    plot_xn(Ns, X, Xs, sg, kp, out); plot_m2(runs, a.pool, out); plot_cpu_per_req(runs, out)

    s4 = runs[4][0]
    t_a, t_b = s4["start_epoch"], s4["end_epoch"]
    xst = [st for st in s4["steps"] if st["step"] == s4["summary"]["X_step"]][0]
    # docker stats: лише відліки з вікна 15 с сходинки X(4) ТОГО САМОГО прогону
    checks = []
    ds = data / "docker_stats_4web.txt"
    docker_md = ""
    if ds.exists():
        samples = parse_stats(ds)
        inside_run = [x for x in samples if t_a <= x["t"] <= t_b]
        a0, b0 = t_a + xst["window_from_s"], t_a + xst["window_to_s"]
        sel = [x for x in samples if a0 <= x["t"] <= b0]
        summary["docker_stats_check"] = dict(samples_total=len(samples), samples_inside_run=len(inside_run), samples_in_X_window=len(sel),
                                             first=datetime.fromtimestamp(samples[0]["t"], timezone.utc).isoformat() if samples else None,
                                             last=datetime.fromtimestamp(samples[-1]["t"], timezone.utc).isoformat() if samples else None,
                                             run_start=s4["started_utc"])
        if not sel:
            checks.append(f"УВАГА: docker stats ({summary['docker_stats_check']['first']} … {summary['docker_stats_check']['last']}) "
                          f"не перетинається з вікном сходинки X(4) прогону N=4 ({datetime.fromtimestamp(a0, timezone.utc).isoformat()}): "
                          "знімок з іншого прогону - перезніміть docker stats разом із прогоном.")
        else:
            names = sorted({k for x in sel for k in x["cpu"]})
            mean = {k: sum(x["cpu"].get(k, 0) for x in sel) / len(sel) for k in names}
            summary["docker_stats_X4"] = dict(samples=len(sel), window_utc=[datetime.fromtimestamp(a0, timezone.utc).isoformat(), datetime.fromtimestamp(b0, timezone.utc).isoformat()],
                                             mean_cpu_pct=mean, sum_cores=sum(mean.values()) / 100,
                                             web_cores=sum(v for k, v in mean.items() if "web" in k) / 100)
            D = [f"\n### docker stats на сходинці X(4) (сходинка {xst['step']}, {xst['target_rps']} req/s, вікно 15 с, {len(sel)} відліків)\n",
                 "| Контейнер | CPU, % від одного ядра |", "|---|---|"]
            for k in names:
                D.append(f"| {k} | {uk(mean[k], 1)} |")
            D.append(f"| Усього | ≈ {uk(sum(mean.values()) / 100, 2)} ядра (web ≈ {uk(summary['docker_stats_X4']['web_cores'], 2)}) |")
            gc = xst.get("generator_cpu_cores")
            D.append(f"\nПроцеси генератора на цій же сходинці: {uk(gc, 2) + ' ядра' if gc is not None else 'немає у файлі результатів (прогін зроблено старою версією генератора)'}.")
            docker_md = "\n".join(D)
    hc = data / a.host_cpu
    if not hc.exists() and (data.parent / "analysis" / "data" / a.host_cpu).exists():
        hc = data.parent / "analysis" / "data" / a.host_cpu
    host_md = ""
    if hc.exists():
        cfg = s4["config"]
        hrows = parse_typeperf(hc, a.host_tz)
        hsteps = host_cpu_steps(hrows, t_a, cfg["warmup_s"], cfg["step_s"], cfg["transient_s"], len(s4["steps"]))
        ncol = len(hrows[0][1]) if hrows else 1
        vsteps = host_cpu_steps(hrows, t_a, cfg["warmup_s"], cfg["step_s"], cfg["transient_s"], len(s4["steps"]), cols=list(range(1, ncol))) if ncol > 1 else None
        ach = [st["achieved_rps_mean"] for st in s4["steps"]]
        inside = [r for r in hrows if t_a <= r[0] <= t_b]
        summary["host_cpu_check"] = dict(samples=len(hrows), samples_inside_run=len(inside), columns=ncol)
        if not inside:
            checks.append("УВАГА: файл CPU хоста не перетинається з прогоном N=4 за часом (перевірте --host-tz і що results/host_cpu_4web.txt знято на цьому прогоні).")
        plot_host_cpu(hrows, t_a, hsteps, ach, out, cfg["warmup_s"], cfg["step_s"])
        summary["host_cpu_N4"] = [dict(h, achieved=ach[i] if i < len(ach) else None, vm_cores_mean=(vsteps[i]["mean"] if vsteps else None)) for i, h in enumerate(hsteps)]
        hdr = "| сх. | задано | досягнуто | CPU хоста (усі ядра), сер. % | макс. % |" + (" ядра стенда 0–3, сер. % |" if vsteps else "")
        T = ["\n### CPU хоста при N=4 (scripts/host_cpu_loop.ps1, вікно: останні 15 с сходинки)\n", hdr, "|---|---|---|---|---|" + ("---|" if vsteps else "")]
        for i, h in enumerate(hsteps):
            if h["mean"] is not None:
                T.append(f"| {h['step']} | {h['target']} | {uk(ach[i], 1)} | {uk(h['mean'], 1)} | {h['max']:.0f} |" + (f" {uk(vsteps[i]['mean'], 1)} |" if vsteps else ""))
        host_md = "\n".join(T)
    json.dump(summary, open(out / "summary.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)

    # ---- таблиці для звіту
    L = ["### X(N)\n", "| N | X(N), req/s | σ по секундах | остання сходинка ≥ 0,9·задана? | X ≥ 0,9·G? | надіслано = задано на всіх сходинках? | запізнення планувальника p99, мс |", "|---|---|---|---|---|---|---|"]
    for n in Ns:
        sm = runs[n][0]["summary"]
        L.append(f"| {n} | {uk(sm['X'], 1)} | {uk(sm['X_std'], 1)} | {'так' if sm['last_step_reached_0_9'] else 'ні'} | {'так' if sm.get('X_ge_0_9_g') else 'ні'} "
                 f"| {'так' if sm.get('sent_equals_target_all_steps') else ('ні' if sm.get('sent_equals_target_all_steps') is False else 'н/д')} | {uk(sm.get('sched_lag_p99_ms'), 0)} |")
    L.append(f"\nG = {uk(G, 1)} req/s.\n")
    for n in Ns:
        L.append(f"\n### N={n}\n\n| сх. | задано | надіслано | досягнуто ± σ | не-2xx % | p99 клієнт | p99 сервер (межі, похибка) | CPU/інст. (сер/макс) | in-flight | pg act/idle | tx/с | CPU-мс/запит | CPU генер., ядер |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in runs[n][2]:
            if r["srv_p99"] is None:
                sv = "—"
            elif r["srv_open"]:
                sv = "> 1000 мс"
            else:
                sv = f"{uk(r['srv_p99'], 1)} [{r['srv_lo']:.0f}; {r['srv_hi']:.0f}], ±{r['srv_err']:.0f}"
            L.append(f"| {r['step']} | {r['target']} | {uk(r['sent'], 1)} | {uk(r['achieved'], 1)} ± {uk(r['std'], 1)} | {uk(r['non2xx_pct'], 1)} | {uk(r['client_p99'], 1)} | {sv} | "
                     f"{uk(r['cpu_per_inst'], 2)}/{uk(r['cpu_max'], 2)} | {uk(r['inprog_mean'], 1)}/{uk(r['inprog_max'], 0)} | {uk(r['pg_active'], 1)}/{uk(r['pg_idle'], 1)} | {r['pg_tps']:.0f} | {uk(r['cpu_ms_per_req'], 2)} | {uk(r['gen_cpu'], 2)} |")
    L.append("\n### p99 клієнта і сервера, N=1\n\n| сх. | задано | p99 клієнт, мс | p99 сервер, мс | межі бакета, мс | похибка (ширина бакета), мс |\n|---|---|---|---|---|---|")
    for r in runs[1][2]:
        if r["srv_p99"] is None:
            L.append(f"| {r['step']} | {r['target']} | {uk(r['client_p99'], 1)} | немає відповідей сервера у вікні | — | — |")
        elif r["srv_open"]:
            L.append(f"| {r['step']} | {r['target']} | {uk(r['client_p99'], 1)} | > 1000 | (1000; +∞) | — |")
        else:
            L.append(f"| {r['step']} | {r['target']} | {uk(r['client_p99'], 1)} | {uk(r['srv_p99'], 1)} | [{r['srv_lo']:.0f}; {r['srv_hi']:.0f}] | {r['srv_err']:.0f} |")
    L.append("\n" + usl_markdown(Ns, X, C, sg, kp, nstar, note, det))
    (out / "tables.md").write_text("\n".join(L) + "\n" + docker_md + "\n" + host_md + "\n" + ("\n".join(["", "### Перевірки"] + checks) if checks else ""), encoding="utf-8")
    json.dump({n: runs[n][2] for n in Ns}, open(out / "rows.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    for c in checks:
        print(c)
    print(json.dumps({k: summary[k] for k in ("X", "X_std", "sigma", "kappa", "N_star", "fit_note")}, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
