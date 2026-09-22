# -*- coding: utf-8 -*-
"""两表匹配·性能与候选上限（离线）：防"数据一多就卡"回归。

背景：低区分度钥匙（长编号、通用项目名）会让每行命中成百上千条候选，
旧实现把它们全塞进结果表 → 实测 5000 行变 315 万行结果、6 秒+。
现在每行最多保留 MAX_CAND 条（按相似度取前 N），其余写进备注。

运行：py tests/experiment_match_perf.py
"""
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from core.matcher import MAX_CAND, run_match  # noqa: E402

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


random.seed(7)
CODE = ["YC-XMYC", "ZZWYHN2-XMYHJT", "FKD-YC", "PO-XMYC"]
N = 1500

# ---- 场景A：长编号钥匙（前缀相同、只差尾号）→ 相似度普遍 ≥80 → 候选爆炸 ----
m = pd.DataFrame([{"合同编号": CODE[i % 4] + f"-WF0{i % 9 + 1}-26-{i:05d}"} for i in range(N)])
k = pd.DataFrame([{"合同编号": (m.at[i, "合同编号"] if i < 100
                              else CODE[i % 4] + f"-WF0{(i * 7) % 9 + 1}-26-{90000 + i:05d}"),
                   "事业部": f"华二第{i % 4 + 1}事业部"} for i in range(N)])
t0 = time.time()
r = run_match(m, k, ["合同编号"], ["合同编号"], [], [], ["事业部"],
              mode="fuzzy", threshold=80.0, log=lambda x: None)
dt = time.time() - t0
n_dup = r["stats"]["重复预警"]
check("候选爆炸场景：结果行数被上限约束（不再爆炸）",
      len(r["result"]) <= N + n_dup * MAX_CAND)
check("候选爆炸场景：耗时在合理范围（<20 秒；实测通常在 1 秒内）", dt < 20.0)
print(f"   （实测：{dt:.2f} 秒，结果 {len(r['result'])} 行，重复预警 {n_dup} 行）")

# 每行候选数 ≤ MAX_CAND（从 row_status 的 combos 看）
_max_c = max((len(s["combos"]) for s in r["row_status"].values()), default=0)
check(f"每行候选数 ≤ MAX_CAND({MAX_CAND})", _max_c <= MAX_CAND)

# 被省略的候选要写进备注（不许静默丢）
_notes = [str(x) for x in r["result"]["备注"].tolist() if "未列出" in str(x)]
check("候选过多时备注写明「另有 N 条候选未列出」", len(_notes) >= 1)
check("重复预警行仍能拿到补值（取前 N 条候选之一）",
      r["result"]["事业部"].astype(str).str.strip().ne("").any())

# ---- 场景B：正常小数据不受影响（精确命中照旧）----
m2 = pd.DataFrame({"供应商名称": ["A公司", "B公司"], "项目名称": ["花园", "广场"]})
k2 = pd.DataFrame({"供应商名称": ["A公司", "B公司"], "项目名称": ["花园", "广场"],
                   "事业部": ["甲部", "乙部"]})
r2 = run_match(m2, k2, ["供应商名称"], ["供应商名称"], [], [], ["事业部"],
               mode="fuzzy", threshold=80.0, log=lambda x: None)
check("正常数据：精确命中不受影响（2/2）",
      r2["stats"]["钥匙精确"] == 2 and list(r2["result"]["事业部"]) == ["甲部", "乙部"])
check("正常数据：无候选省略（备注不写未列出）",
      not any("未列出" in str(x) for x in r2["result"]["备注"].tolist()))

# ---- 场景C：经验库消歧不依赖候选上限（库目标排在第 11 名之外 / 干脆不在候选里）----
from core.experience import ExperienceStore  # noqa: E402

_expf = os.path.join(os.environ["TEMP"], "opencode", "exp_perf.json")
os.makedirs(os.path.dirname(_expf), exist_ok=True)
if os.path.exists(_expf):
    os.remove(_expf)

m3 = pd.DataFrame([{"钥匙": "K"}])
# 14 条"很像"的候选（K-01..K-14，ratio=40 ≥ 阈值35 → 会进重复预警、被截到 10 条）
# + 1 条库目标 K-9999（ratio=25 < 阈值 → 本来就不是候选）
k3 = pd.DataFrame({"钥匙": [f"K-{i:02d}" for i in range(1, 15)] + ["K-9999"],
                   "事业部": [f"第{i:02d}部" for i in range(1, 15)] + ["第十五部"]})
r3_no = run_match(m3, k3, ["钥匙"], ["钥匙"], [], [], ["事业部"],
                  mode="fuzzy", threshold=35.0, log=lambda x: None)
check("前置：不加经验库 → 该行是重复预警且候选≤上限",
      r3_no["row_status"][0]["status"] == "重复预警"
      and len(r3_no["row_status"][0]["combos"]) <= MAX_CAND)

_exp = ExperienceStore(_expf)
_exp.record("K", "K-9999", src="manual")
r3 = run_match(m3, k3, ["钥匙"], ["钥匙"], [], [], ["事业部"],
               mode="fuzzy", threshold=35.0, experience=_exp, log=lambda x: None)
check("经验库消歧：库目标不在候选列表里（更不在前10）→ 仍自动命中",
      r3["row_status"][0]["status"] == "经验库"
      and r3["result"]["事业部"].iloc[0] == "第十五部")

print(f"\n===== 两表匹配·性能/候选上限 通过：{ok} 项断言 =====")
