# -*- coding: utf-8 -*-
"""测试分档入口：py tests\\run.py 快 | 全

快档（日常迭代，约 3~5 分钟）：冒烟 + 多表补全主套件 + 真实三文件回归
全量档（收尾才跑一次）：17 套各 1 遍（收尾纪律：关键套自行再复跑一遍）
synth_quotes.py 是造数脚本，不属于测试，永远不跑。
输出乱码不影响判定：以退出码为准；失败时打印该套件的输出尾部。
"""
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QUICK = ["smoke_pipeline.py", "experiment_table_fill.py", "experiment_table_fill_real3.py"]
ALL = QUICK + ["experiment_align.py", "experiment_beautify.py", "experiment_compare.py",
               "experiment_conventions.py", "experiment_features_b.py", "experiment_gongju.py",
               "experiment_match_accuracy.py", "experiment_parser.py", "experiment_parser_llm.py",
               "experiment_pipei.py", "experiment_table_fill_exp.py",
               "experiment_table_fill_accuracy.py", "test_llm_client.py", "test_ui_smoke.py"]


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "快")
    suites = QUICK if mode.startswith("快") else ALL
    t0 = time.time()
    failed = []
    for name in suites:
        t1 = time.time()
        proc = subprocess.run([sys.executable, os.path.join("tests", name)],
                              cwd=_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        dt = time.time() - t1
        status = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"[{status}] {name}  ({dt:.0f}s)")
        if proc.returncode != 0:
            tail = (proc.stdout or "").strip().splitlines()[-15:]
            print("   ---- 失败输出尾部 ----")
            for line in tail:
                print("   " + line)
            failed.append(name)
    print(f"===== {mode}档：{len(suites)} 套，通过 {len(suites) - len(failed)}，"
          f"失败 {len(failed)}，用时 {time.time() - t0:.0f} 秒 =====")
    if failed:
        print("失败套件：" + "、".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
