"""
稳态 tensor core 吞吐测试。

判据：FLOP / SM / cycle
  Ada GeForce 出厂锁定 = 512  （FP16/BF16 配 FP32 累加，官方规格）
  解锁后            = 1024

做法：持续压 20 秒让 GPU 爬到稳态 boost，只统计后半段，
      同时高频采样 clocks.sm 得到稳态频率，两者相除即得每周期发射率。
"""
import subprocess, threading, time, statistics, json, sys

import torch

QUERY = "clocks.sm,power.draw,temperature.gpu,utilization.gpu,pstate"
SM_COUNT = None


def nv():
    out = subprocess.run(
        ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5,
    ).stdout.strip()
    if not out:
        return None
    p = [x.strip() for x in out.split(",")]
    try:
        return dict(sm=float(p[0]), pw=float(p[1]), temp=float(p[2]),
                    util=float(p[3]), pstate=p[4])
    except Exception:
        return None


class Sampler:
    def __init__(self):
        self.data = []
        self.stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self.stop:
            d = nv()
            if d:
                d["ts"] = time.perf_counter()
                self.data.append(d)
            time.sleep(0.1)

    def halt(self):
        self.stop = True
        self.t.join(timeout=2)


def steady_bench(dtype, n, label, burn=12.0, measure=8.0):
    a = torch.randn(n, n, device="cuda", dtype=dtype)
    b = torch.randn(n, n, device="cuda", dtype=dtype)
    fn = lambda: a @ b

    # 持续压，让 GPU 爬到稳态
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < burn:
        for _ in range(20):
            fn()
        torch.cuda.synchronize()

    s = Sampler()
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    t1 = None
    ts = []
    while time.perf_counter() - t0 < measure:
        torch.cuda.synchronize()
        s0 = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        ts.append(time.perf_counter() - s0)
    t1 = time.perf_counter()
    s.halt()

    med = statistics.median(ts)
    tflops = 2 * n ** 3 / med / 1e12

    # 只取测量窗口内的采样，且 util 高、P0
    win = [d for d in s.data if d["ts"] >= t0 and d["util"] > 60]
    clk = statistics.median([d["sm"] for d in win]) if win else float("nan")
    pw = statistics.median([d["pw"] for d in win]) if win else float("nan")
    tp = statistics.median([d["temp"] for d in win]) if win else float("nan")

    per_cyc = tflops * 1e12 / (clk * 1e6 * SM_COUNT) if clk == clk and clk > 0 else float("nan")

    print(f"{label:12s} N={n:5d}  {tflops:7.2f} TFLOPS  "
          f"clk={clk:6.0f}MHz  pw={pw:5.1f}W  {tp:4.0f}C  "
          f"-> {per_cyc:6.1f} FLOP/SM/cyc")
    del a, b
    torch.cuda.empty_cache()
    return dict(label=label, n=n, tflops=round(tflops, 2), clk=clk,
                pw=round(pw, 1), temp=tp, flop_per_sm_cyc=round(per_cyc, 1),
                n_samp=len(win))


p = torch.cuda.get_device_properties(0)
SM_COUNT = p.multi_processor_count
print(f"GPU: {torch.cuda.get_device_name(0)}   SM={SM_COUNT}  sm_{p.major}{p.minor}")
print(f"torch {torch.__version__} / cuda {torch.version.cuda}")
print("=" * 92)
print("判据: 出厂锁 512 FLOP/SM/cyc   解锁后 1024")
print("=" * 92)

res = []
res.append(steady_bench(torch.float16, 8192, "FP16"))
res.append(steady_bench(torch.bfloat16, 8192, "BF16"))
res.append(steady_bench(torch.float16, 16384, "FP16"))

print("=" * 92)
print(json.dumps(res, indent=1))
