# -*- coding: utf-8 -*-
"""多表补全 · 经验库 + 候选 + 抽查（离线）。

覆盖：多候选给候选值；「确认并记住」写库后**下次自动填**（标 ·经验库、不再进清单）；
「未补上」给最接近候选；非 100% 的格进抽查表；清单只收必看。

运行：py tests/experiment_table_fill_exp.py
"""
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd

from core.experience import ExperienceStore
from core.table_filler import exp_key_for, fill_multi

ok = 0
TMP_STORE = os.path.join(os.environ.get("TEMP", "."), "opencode", "exp_fill_test.json")
os.makedirs(os.path.dirname(TMP_STORE), exist_ok=True)


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def fresh_store():
    if os.path.exists(TMP_STORE):
        os.remove(TMP_STORE)
    return ExperienceStore(TMP_STORE)


# ---- ① 多候选 → 给候选值 → 写库 → 下次自动填 ----
tpl = pd.DataFrame({"钥匙": ["k1"], "值": [""]})
src = {"name": "s1", "df": pd.DataFrame({"钥匙": ["k1", "k1"], "值": ["X", "Y"]})}
store = fresh_store()
r0 = fill_multi(tpl, key_cols=["钥匙"], sources=[src], experience=store)
ch = r0["choices"]
check("多候选：留空 + 进必看清单", str(r0["result"]["值"].iloc[0]).strip() == ""
      and r0["stats"]["歧义格数"] == 1 and len(r0["review"]) == 1)
check("候选表：给出候选1/候选2（干净取值）",
      len(ch) == 1 and [ch["候选1"].iloc[0], ch["候选2"].iloc[0]] == ["X", "Y"]
      and ch["类型"].iloc[0] == "多候选")

key = exp_key_for(tpl, 0, "值", r0["exp_key_cols"])
store.record([key], ["Y"], src="多表补全", raw=True)
r1 = fill_multi(tpl, key_cols=["钥匙"], sources=[src], experience=store)
check("写库后：自动填成 Y、标 ·经验库、不再进清单",
      str(r1["result"]["值"].iloc[0]) == "Y"
      and "经验库" in str(r1["confidence"]["值"].iloc[0])
      and r1["stats"]["经验库命中"] == 1 and r1["stats"]["需人工确认行数"] == 0
      and r1["stats"]["歧义格数"] == 0)
check("统计自检：各档之和 == 行数×目标列数",
      (r1["stats"]["完全匹配(100%)"] + r1["stats"]["高置信(80-99%)"]
       + r1["stats"]["中置信(40-80%)"] + r1["stats"]["低置信(20-40%)"]
       + r1["stats"]["未匹配(留空)"] + r1["stats"]["歧义格数"])
      == r1["stats"]["模板行数"] * r1["stats"]["目标列数"])

# 经验库只影响"同一行同一列"，换一行不该被套用
tpl2 = pd.DataFrame({"钥匙": ["k2"], "值": [""]})
r2 = fill_multi(tpl2, key_cols=["钥匙"], sources=[src], experience=store)
check("经验库不串行：别的钥匙值不受影响（该行源表也没有 → 留空）",
      str(r2["result"]["值"].iloc[0]).strip() == "" and r2["stats"]["经验库命中"] == 0)

# ---- ② 未补上 → 给最接近候选（钥匙门槛调高、让自动匹配失败时）----
tpl3 = pd.DataFrame({"项目": ["天河项目"], "值": [""]})
src3 = {"name": "s3", "df": pd.DataFrame({"项目": ["天河边检站项目", "别的项目"],
                                          "值": ["V1", "V2"]})}
r3 = fill_multi(tpl3, key_cols=["项目"], sources=[src3], experience=fresh_store(),
                auto_keys=False, key_min=95, audit_rounds=0)
ch3 = r3["choices"]
check("未补上：给出最接近候选（含相似度备注）",
      len(ch3) == 1 and ch3["类型"].iloc[0] == "最接近候选"
      and "≈" in str(ch3["备注"].iloc[0]) and len(r3["review"]) == 1
      and str(ch3["候选1"].iloc[0]) == "V1")
check("未补上：门槛放低后同一条能自动配上（说明候选只是线索）",
      str(fill_multi(tpl3, key_cols=["项目"], sources=[src3], auto_keys=False,
                     key_min=20, audit_rounds=0)["result"]["值"].iloc[0]) == "V1")

# ---- ③ 抽查表：非 100% 的格要列出来（模糊命中）；经验库=100% 不列 ----
tpl4 = pd.DataFrame({"钥匙": ["A公司"], "值": [""]})
src4 = {"name": "s4", "df": pd.DataFrame({"钥匙": ["A公司集团"], "值": ["Z"]})}
r4 = fill_multi(tpl4, key_cols=["钥匙"], sources=[src4], key_min=30, audit_rounds=0)
pt = r4["partial"]
check("抽查表：模糊补出的格（非100%）被列出",
      len(pt) == 1 and str(pt["值"].iloc[0]) == "Z"
      and str(r4["confidence"]["值"].iloc[0]).split(":")[0] != "ok")
r4b = fill_multi(tpl, key_cols=["钥匙"], sources=[src], experience=store, audit_rounds=0)
check("抽查表：经验库命中(100%)不进抽查表", len(r4b["partial"]) == 0)

if os.path.exists(TMP_STORE):
    os.remove(TMP_STORE)
print(f"\n===== 多表补全·经验库/候选/抽查 通过：{ok} 项断言 =====")
