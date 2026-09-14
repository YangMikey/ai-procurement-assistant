# -*- coding: utf-8 -*-
"""M2b 跨供应商对齐测试：合成 4 家 → 比价矩阵（同品异名/缺行/多行）+ 经验库飞轮 + 校验变量。

运行：py tests/experiment_align.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd

from core.aligner import align_quotes, norm_text, record_alias, SEP
from core.experience import ExperienceStore
from core.parser_rule import parse_quote_file

SAMPLES = os.path.join(_ROOT, "data", "samples")
OUT = os.path.join(_ROOT, "data", "outputs")

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


with open(os.path.join(SAMPLES, "ground_truth.json"), encoding="utf-8") as f:
    GT = json.load(f)

paths = [
    os.path.join(SAMPLES, "报价单A_标准格式.xlsx"),
    os.path.join(SAMPLES, "报价单B_表头第2行_仅不含税.xlsx"),
    os.path.join(SAMPLES, "报价单C_文本价格.xlsx"),
    os.path.join(SAMPLES, "报价单D_同品异名_缺行多行.xlsx"),
]
quotes = [parse_quote_file(path=p)[0] for p in paths]
sup = [q["supplier"] for q in quotes]

res = align_quotes(quotes, price_field="不含税单价")
m, conf, low = res["matrix"], res["confidence"], res["low_confidence"]

check("对齐：4 家供应商列", res["suppliers"] == sup and all(s in m.columns for s in sup))
check("对齐：行数=7（6 品 + D 多出的劳保手套）", len(m) == 7)
check("对齐：同品异名未拆成多行（螺钉/电线/触摸开关各 1 行）",
      sum(1 for n in m["品名"] if "螺钉" in str(n)) == 1
      and sum(1 for n in m["品名"] if "电线电缆" in str(n)) == 1
      and sum(1 for n in m["品名"] if "触摸开关" in str(n)) == 1)

# 螺钉行：D 的同品异名(螺钉M4*10) 应落到同一行
row_screw = m[m["品名"].astype(str).str.contains("螺钉")].iloc[0]
check("对齐：D 的同品异名 螺钉M4*10 归到螺钉行", pd.notna(row_screw[sup[3]]))

# 缺行：D 缺 LED 灯管 → D 列应为空；多行：劳保手套仅 D 有
row_led = m[m["品名"].astype(str).str.contains("LED")].iloc[0]
row_gl = m[m["品名"].astype(str).str.contains("劳保")].iloc[0]
check("对齐：D 缺 LED 灯管（D 列空）", pd.isna(row_led[sup[3]]) or row_led[sup[3]] in ("", None))
check("对齐：劳保手套仅 D 有（其他列空）",
      all(pd.isna(row_gl[s]) or row_gl[s] in ("", None) for s in sup[:3]))

# 最低价供应商：B 在 6 个品类都最低（DELTA B=-5%）
check("对齐：螺钉行最低价供应商=B", row_screw["最低价供应商"] == sup[1])
check("对齐：劳保手套最低价供应商=D", row_gl["最低价供应商"] == sup[3])

# 最低价数值与 ground truth 一致（不含税 = 含税/(1+税)）
exp_nums = {s: round(GT["items"][0]["prices_tax_in"][k] / (1 + GT["tax"]), 4)
            for s, k in zip(sup, ["A", "B", "C", "D"])}
check("对齐：螺钉行最低价≈GT(B 不含税)",
      abs(float(row_screw["最低价"]) - exp_nums[sup[1]]) < 0.02)

# 置信度：字符袋/乱序命中 → 非"高"，并出现在待确认里
check("对齐：低/中置信度项进入待人工确认", len(low) >= 1)
check("对齐：乱序异名(触摸开关)被识别并降级待确认",
      any("触摸开关" in str(x["品名"]) or "触摸开关" in str(x["对齐到"]) for x in low))
check("对齐：置信度矩阵维度一致", list(conf.columns) == ["品名", "规格"] + sup)

# ---- 经验库飞轮：人工确认后 → 第①级命中、置信度升高 ----
exp_path = os.path.join(SAMPLES, "_tmp_align_exp.json")
if os.path.exists(exp_path):
    os.remove(exp_path)
store = ExperienceStore(path=exp_path)
# 新语义：模糊能配上时，经验库不抢答（记录后仍是「模糊」）
qD = quotes[3]
rowD = qD["df"][qD["df"]["品名"].astype(str).str.contains("触摸开关")].iloc[0]
alias_key = SEP.join(norm_text(rowD.get(f, "")) for f in ("品名", "规格"))
canon_key = res["key_to_canon"][alias_key]
record_alias(store, rowD, canon_key)
res2 = align_quotes(quotes, price_field="不含税单价", experience=store)
lv = [a for a in res2["assignments"]
      if a["supplier"] == sup[3] and "触摸开关" in str(a["品名"])]
check("经验库兜底：模糊能配上时不抢答（仍为模糊）", lv and lv[0]["level"] == "模糊")

# 模糊配不上的异名 → 库兜底命中
qj = {"supplier": "甲", "df": pd.DataFrame({"品名": ["新之助"], "规格": [""], "单位": ["个"],
                                           "数量": [1], "不含税单价": [10.0]})}
qy = {"supplier": "乙", "df": pd.DataFrame({"品名": ["小爱"], "规格": [""], "单位": ["个"],
                                           "数量": [1], "不含税单价": [9.0]})}
r_new = align_quotes([qj, qy], price_field="不含税单价", experience=store)
check("对齐：模糊配不上时先成新品类（2 行）", len(r_new["matrix"]) == 2)
_canon = [a["canonical"] for a in r_new["assignments"] if a["supplier"] == "甲"][0]
record_alias(store, {"品名": "小爱", "规格": ""}, SEP.join(norm_text(x) for x in (_canon, "")))
r_fb = align_quotes([qj, qy], price_field="不含税单价", experience=store)
_lv = [a for a in r_fb["assignments"] if a["supplier"] == "乙"][0]
check("经验库兜底：并入标准品（1 行）", len(r_fb["matrix"]) == 1)
check("经验库兜底：来源标「经验库」", _lv["level"] == "经验库" and _lv["confidence"] == "高")
os.remove(exp_path)

# ---- 校验变量：单位不一致 → 置信度降级 ----
qa = {"supplier": "甲", "df": pd.DataFrame({"品名": ["阀门"], "规格": ["DN50"], "单位": ["个"],
                                           "数量": [1], "不含税单价": [10.0]})}
qb = {"supplier": "乙", "df": pd.DataFrame({"品名": ["阀门"], "规格": ["DN50"], "单位": ["套"],
                                           "数量": [1], "不含税单价": [9.0]})}
rc = align_quotes([qa, qb], price_field="不含税单价")
lv2 = [a for a in rc["assignments"] if a["supplier"] == "乙"][0]
check("校验变量：单位不一致 → 置信度降级为非高", lv2["confidence"] != "高")
check("校验变量：降级项进入待确认", any("单位不一致" in str(x["原因"]) for x in rc["low_confidence"]))

print(f"\n===== M2b 跨供应商对齐测试通过：{ok} 项断言 =====")
