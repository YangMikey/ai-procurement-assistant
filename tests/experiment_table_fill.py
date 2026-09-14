# -*- coding: utf-8 -*-
"""多表补全测试（离线）：列供给发现 / 多源逐行回退 / 置信分档 / 同义列名 / 备注块 / 导出色。

运行：py tests/experiment_table_fill.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd
from openpyxl import load_workbook

from core.table_filler import COLORS, col_match, discover_supply, export_filled, fill_multi

TMP = os.path.join(os.environ["TEMP"], "opencode", "fill_demo.xlsx")
os.makedirs(os.path.dirname(TMP), exist_ok=True)

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


# 模板：第1列=供应商；要补：合同结束时间（列名与源表"合同截止时间"不同）、责任人、金额
tpl = pd.DataFrame({"供应商": ["A公司", "B公司", "C公司", "ZZZ集团"],
                    "合同结束时间": ["", "", "", ""], "责任人": ["", "", "", ""], "金额": ["", "", "", ""]})
# 源1：有 合同截止时间 / 责任人；钥匙列"单位"
s1 = pd.DataFrame({"单位": ["A公司", "B公司"], "合同截止时间": ["2026-12-31", "2026-06-30"],
                   "责任人": ["张三", "李四"]})
# 源2：有 金额；钥匙列"供应商名称"（与模板钥匙列名不同，但内容一致）
s2 = pd.DataFrame({"供应商名称": ["A公司", "C公司"], "金额": ["1000", "2000"]})
# 源3：备用金额（优先级低、有值少）
s3 = pd.DataFrame({"单位": ["B公司"], "金额": ["999"]})
sources = [{"name": "源1", "df": s1, "key_col": "单位"},
           {"name": "源2", "df": s2, "key_col": "供应商名称"},
           {"name": "源3", "df": s3, "key_col": "单位"}]

# ---- 列名匹配：同义词 + 近似 ----
check("列名：'合同结束时间' ↔ '合同截止时间' 判为可替代（同义/近似）",
      col_match("合同结束时间", "合同截止时间")[0] >= 70)
check("列名：含税/不含税 判为冲突不可替代",
      col_match("含税单价", "不含税单价")[1] == "冲突")
check("列名：'供应商' ↔ '供应商名称' 可替代", col_match("供应商", "供应商名称")[0] >= 70)

# ---- 列供给发现 ----
sup = discover_supply(["合同结束时间", "责任人", "金额"], sources)
check("供给：合同结束时间 ← 源1",
      sup["合同结束时间"] and sup["合同结束时间"][0]["col"] == "合同截止时间")
check("供给：责任人 ← 源1", sup["责任人"] and sup["责任人"][0]["source"] == 0)
check("供给：金额首选有值多的源2（2 值）而非源3（1 值）",
      sup["金额"] and sup["金额"][0]["source"] == 1
      and sup["金额"][1]["source"] == 2)

# ---- 填充 ----
res = fill_multi(tpl, "供应商", sources)
r = res["result"]
check("填充：A/B 合同结束时间已补（列名不同也能对上）",
      r.loc[0, "合同结束时间"] == "2026-12-31" and r.loc[1, "合同结束时间"] == "2026-06-30")
check("尽量填：C 公司（钥匙仅~60%相似）也填，但降档（中/低置信）",
      str(r.loc[2, "合同结束时间"]).strip() == "2026-12-31"
      and str(res["confidence"].loc[2, "合同结束时间"]).split(":")[0] in ("mid", "low"))
check("未匹配：ZZZ集团（钥匙 <20%）→ 留空", str(r.loc[3, "合同结束时间"]).strip() == "")
check("填充：金额 A←源2、B←源3（逐行回退到备选源）",
      r.loc[0, "金额"] == "1000" and r.loc[1, "金额"] == "999")
check("置信：完全匹配不标色（ok）",
      str(res["confidence"].loc[0, "合同结束时间"]).startswith("ok"))
check("置信：无候选列为 miss（留空+红底档）",
      str(res["confidence"].loc[3, "合同结束时间"]).startswith("miss"))
check("统计：未匹配计数 ≥1、未补全行数=1、图例存在",
      res["stats"]["未匹配(留空)"] >= 1 and res["stats"]["未补全行数"] == 1
      and any("颜色图例" in x for x in res["legend"]))

# ---- 近似钥匙 → 分档上色（低/中/高） ----
tpl2 = pd.DataFrame({"供应商": ["A公司"], "责任人": [""], "金额": [""]})
s4 = pd.DataFrame({"单位": ["A公司集团"], "责任人": ["王五"], "金额": ["123"]})   # 钥匙近似
res2 = fill_multi(tpl2, "供应商", [{"name": "源4", "df": s4, "key_col": "单位"}])
band = str(res2["confidence"].loc[0, "责任人"]).split(":")[0]
check("置信：近似钥匙（A公司集团≈A公司）落入高置信档而非静默 100%", band == "high")

# ---- 手动指定优先 ----
res3 = fill_multi(tpl, "供应商", sources, mapping={"金额": (2, "金额")})
check("手动指定：金额优先取源3", res3["result"].loc[1, "金额"] == "999"
      and res3["supply"]["金额"][0]["how"] == "手动")

# ---- 导出 + 颜色/图例 ----
p = export_filled(r, res["confidence"], TMP, stats=res["stats"])
wb = load_workbook(p)
ws = wb.active
i_end = list(r.columns).index("合同结束时间") + 1
c_ok = ws.cell(row=2, column=i_end)          # A 公司：完全匹配 → 不标色
c_miss = ws.cell(row=5, column=i_end)        # ZZZ集团：未匹配 → 浅红底
check("导出：完全匹配不填色", c_ok.fill.fgColor.rgb in (None, "00000000"))
check("导出：未匹配单元格浅红底（FFC7CE/FCE8E6 档）",
      str(c_miss.fill.fgColor.rgb).upper().endswith("FCE8E6"))
tail_txt = "".join(str(ws.cell(row=rr, column=1).value or "") for rr in range(ws.max_row - 6, ws.max_row + 1))
check("导出：表格下方写了图例/统计备注", "颜色图例" in tail_txt and "统计" in tail_txt)
wb.close()
os.remove(TMP)

check("调色板：五档齐备（ok/high/mid/low/miss）",
      set(COLORS.keys()) == {"ok", "high", "mid", "low", "miss"})
print(f"\n===== 多表补全测试通过：{ok} 项断言（离线）=====")
