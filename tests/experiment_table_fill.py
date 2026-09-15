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
check("尽量填：B 公司（钥匙精确）合同结束时间已补",
      r.loc[1, "合同结束时间"] == "2026-06-30")
_c_types = set(res["review"]["类型"].astype(str).map(lambda s: s.split("(")[0]))
check("宁缺勿错：C 公司模糊钥匙（低相似度对上多行）→ 留空不猜，并进清单",
      str(r.loc[2, "合同结束时间"]).strip() == ""
      and ("多候选" in _c_types or "未补上" in _c_types))
check("未匹配：ZZZ集团（钥匙 <20%）→ 留空", str(r.loc[3, "合同结束时间"]).strip() == "")
check("填充：金额 A←源2、B←源3（逐行回退到备选源）",
      r.loc[0, "金额"] == "1000" and r.loc[1, "金额"] == "999")
check("置信：完全匹配不标色（ok）",
      str(res["confidence"].loc[0, "合同结束时间"]).startswith("ok"))
check("置信：无候选列为 miss（留空+红底档）",
      str(res["confidence"].loc[3, "合同结束时间"]).startswith("miss"))
check("统计：留空合计 ≥1、未补全行数 ≥1、图例存在",
      res["stats"]["留空合计"] >= 1 and res["stats"]["未补全行数"] >= 1
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
check("导出：未匹配单元格=主题「缺失」浅灰（FFF2F2F2）",
      str(c_miss.fill.fgColor.rgb).upper().endswith("F2F2F2"))
tail_txt = "".join(str(ws.cell(row=rr, column=1).value or "") for rr in range(ws.max_row - 6, ws.max_row + 1))
check("导出：表格下方写了图例/统计备注", "颜色图例" in tail_txt and "统计" in tail_txt)
wb.close()
os.remove(TMP)

check("调色板：五档齐备（ok/high/mid/low/miss）",
      set(COLORS.keys()) == {"ok", "high", "mid", "low", "miss"})
# ---- 多钥匙（自动识别）+ 分层降级 + 级联 ----
tpl8 = pd.DataFrame({"供应商": ["甲", "甲"], "品类": ["A", "B"], "价格": ["", ""]})
sA = pd.DataFrame({"供应商": ["甲", "甲"], "品类": ["A", "B"], "价格": [10, 20]})
r8 = fill_multi(tpl8, key_cols=["供应商", "品类"], sources=[{"name": "sA", "df": sA}])
check("多钥匙：两列钥匙区分同供应商不同品类（10/20）",
      list(r8["result"]["价格"]) == [10, 20])

tpl9 = pd.DataFrame({"项目": ["P1"], "类别": ["X"], "负责人": [""]})
sB = pd.DataFrame({"项目": ["P1"], "类别": ["Y"], "负责人": ["张三"]})   # 类别对不上 → 2 钥匙失配
r9 = fill_multi(tpl9, key_cols=["项目", "类别"], sources=[{"name": "sB", "df": sB}])
check("分层降级：两钥匙失配→降为单钥匙命中", r9["result"]["负责人"].iloc[0] == "张三")

tplC = pd.DataFrame({"项目名称": ["P1"], "合同编号": [""], "事业部": [""]})
c1s = pd.DataFrame({"项目名称": ["P1"], "合同编号": ["C1"]})
c2s = pd.DataFrame({"合同编号": ["C1"], "事业部": ["事业部A"]})
rC = fill_multi(tplC, key_cols=["项目名称"], sources=[{"name": "源1", "df": c1s}, {"name": "源2", "df": c2s}])
check("级联：第1轮补出合同编号", rC["result"]["合同编号"].iloc[0] == "C1")
check("级联：第2轮用合同编号补出事业部", rC["result"]["事业部"].iloc[0] == "事业部A")
check("级联：统计含轮数与间接格数",
      rC["stats"]["级联轮数"] >= 2 and rC["stats"]["间接补全格数"] >= 1)

# 低置信不升级为钥匙（模糊 70 分补出的合同编号 不应成为钥匙）
tplD = pd.DataFrame({"项目名称": ["朗晴居二期"], "合同编号": [""], "事业部": [""]})
d1 = pd.DataFrame({"项目名称": ["朗晴花园"], "合同编号": ["C9"]})   # 与"朗晴居二期"约 50 分（<80）
d2 = pd.DataFrame({"合同编号": ["C9"], "事业部": ["事业部B"]})
rD = fill_multi(tplD, key_cols=["项目名称"], sources=[{"name": "源1", "df": d1}, {"name": "源2", "df": d2}])
check("低置信：模糊补出的值不升级为钥匙（事业部未补）",
      str(rD["result"]["事业部"].iloc[0]).strip() == ""
      and str(rD["confidence"].loc[0, "事业部"]).startswith("miss"))

# 三跳链 + 轮数上限
tplE = pd.DataFrame({"A": ["a1"], "B": [""], "C": [""], "D": [""]})
e1 = pd.DataFrame({"A": ["a1"], "B": ["b1"]})
e2 = pd.DataFrame({"B": ["b1"], "C": ["c1"]})
e3 = pd.DataFrame({"C": ["c1"], "D": ["d1"]})
rE = fill_multi(tplE, key_cols=["A"], sources=[{"name": "e1", "df": e1}, {"name": "e2", "df": e2},
                                              {"name": "e3", "df": e3}], max_rounds=4)
check("三跳链：B/C/D 全部补出，轮数=3", list(rE["result"][["B", "C", "D"]].iloc[0]) == ["b1", "c1", "d1"]
      and rE["stats"]["级联轮数"] == 3)

# 歧义：同钥匙多行且取值不同 → 留空 + 记入清单（不猜第 1 条）
tplF = pd.DataFrame({"钥匙": ["k1"], "值": [""]})
f1 = pd.DataFrame({"钥匙": ["k1", "k1"], "值": ["X", "Y"]})
rF = fill_multi(tplF, key_cols=["钥匙"], sources=[{"name": "f1", "df": f1}])
check("歧义：命中多行取值不同 → 留空 + 进清单（多候选）",
      str(rF["result"]["值"].iloc[0]).strip() == "" and rF["stats"]["歧义格数"] == 1
      and len(rF["review"]) == 1 and "多候选" in str(rF["review"]["类型"].iloc[0]))
# 并列多行但取值相同 → 直接用，不算歧义
tplF2 = pd.DataFrame({"钥匙": ["k1"], "值": [""]})
f2 = pd.DataFrame({"钥匙": ["k1", "k1"], "值": ["Z", "Z"]})
rF2 = fill_multi(tplF2, key_cols=["钥匙"], sources=[{"name": "f2", "df": f2}])
check("同键多行但取值相同 → 照常填，不算歧义",
      str(rF2["result"]["值"].iloc[0]) == "Z" and rF2["stats"]["歧义格数"] == 0)

# ---- 真实文件：采购项目汇总（用「事业部匹配结果」补 Sheet1 的合同编号，双钥匙） ----
KF = os.path.join(_ROOT, "data", "ground_truth", "钥匙表_采购项目汇总.xlsx")
if os.path.exists(KF):
    from core.excel_io import read_table
    src_df = read_table(KF, "事业部匹配结果", header_row=1)
    tplK = src_df[["项目名称", "采购三级分类"]].copy()
    tplK["合同编号"] = ""
    rK = fill_multi(tplK, key_cols=["项目名称", "采购三级分类"],
                    sources=[{"name": "GT", "df": src_df}])
    hit = (rK["result"]["合同编号"].astype(str).str.strip() != "").mean()
    row0 = rK["result"][(rK["result"]["项目名称"] == "朗晴居二期")
                        & (rK["result"]["采购三级分类"] == "绿化养护")]
    check("真实文件：双钥匙补全命中率 ≥ 85%（余下 = 源表自身重复行，已留空）", hit >= 0.85)
    check("真实文件：源表自身重复行的格 → 留空并进清单（不猜第1条）",
          rK["stats"]["歧义格数"] == 4
          and "多候选" in "".join(rK["review"]["类型"].astype(str)))
    check("真实文件：指定行合同编号正确（朗晴居二期/绿化养护）",
          len(row0) == 1 and str(row0["合同编号"].iloc[0]).startswith("YC-XMYC0112-WF02"))
else:
    check("真实文件：钥匙表_采购项目汇总.xlsx 存在", False)

# ---- 回归：列供给下拉标签（曾有 KeyError: 'name'——候选字典无 name 字段） ----
from core.table_filler import supply_option_label
_srcs = [{"name": "源A", "df": s1}, {"name": "源B", "df": s2}]
_sup2 = discover_supply(["金额"], _srcs, 70)
_lbls = [supply_option_label(c, _srcs) for c in _sup2["金额"]]
check("列供给标签：用源表名格式且不报错",
      len(_lbls) >= 1 and all("【" in x and "(" in x for x in _lbls)
      and any(x.startswith("源B") for x in _lbls))

# ================= 新增：自动配钥匙（值域/同义/择优/延后/复验） =================
from core.table_filler import pair_col, plan_keys, value_domain_sim

# --- 同义与级别冲突（采购三级分类 ↔ 类别；一级分类 不可替代）---
check("列名：采购三级分类 ↔ 类别 判同义（可替代）",
      col_match("采购三级分类", "类别")[0] >= 90)
check("列名：采购三级分类 ↔ 一级分类 判冲突（级别不同不可替代）",
      col_match("采购三级分类", "一级分类")[1] == "冲突")

# --- 值域指纹：列名完全对不上（'服务大类'），但取值集合一致 → 90 分「值域」---
_vt = pd.Series(["绿化养护", "日常保洁", "生活垃圾清运", "消防维保", "四害消杀", "建筑垃圾清运"])
_vs = pd.Series(["绿化养护", "日常保洁", "生活垃圾清运", "消防维保", "四害消杀", "建筑垃圾清运"])
_ds, _cov = value_domain_sim(_vt, _vs)
check("值域：两列取值一致 → 认定并给双向覆盖率", _ds >= 90 and _cov == 100)
check("值域：列名对不上也能配对（方式=值域）",
      pair_col("采购三级分类", "服务大类", _vt, _vs)[1] == "值域")
_short = pd.Series(["A", "B", "C"])
check("值域：唯一值太少不认（防小表误配）",
      pair_col("采购三级分类", "服务大类", _short, _short)[0] == 0.0)

# --- 自动补位：只点「项目名称」→ 系统自动补上能对上的第二把钥匙（值域）---
_CATS = ["绿化养护", "日常保洁", "生活垃圾清运", "消防维保", "四害消杀", "建筑垃圾清运"]
_DEPTS = ["华二第三事业部", "华二第一事业部", "华二第二事业部", "华二第一事业部",
          "华二第二事业部", "华二第二事业部"]
_tD = pd.DataFrame({"项目名称": ["朗晴居二期"] * 6 + ["朗晴居二期"], "采购三级分类": _CATS + ["绿化养护"],
                    "事业部": [""] * 7})
_tD.loc[6, "项目名称"] = "景安花园"
_sD = pd.DataFrame({"项目名称": ["朗晴居二期"] * 6 + ["景安花园"], "服务大类": _CATS + ["绿化养护"],
                    "事业部": _DEPTS + ["华二第三事业部"]})
_rD = fill_multi(_tD, key_cols=["项目名称"], sources=[{"name": "sD", "df": _sD}])
check("自动补钥匙：只用「项目名称」也会补上值域钥匙 → 每行各取正确行",
      list(_rD["result"]["事业部"]) == _DEPTS + ["华二第三事业部"])
check("自动补钥匙：备注标 2 钥匙、且无歧义格",
      "2钥匙" in str(_rD["confidence"].loc[0, "事业部"]) and _rD["stats"]["歧义格数"] == 0)
check("自动补钥匙：给了说明（哪张表补了哪把钥匙）",
      any("自动补了钥匙列" in x for x in _rD["stats"]["钥匙说明"]))

# --- 表级延后：单钥匙源表第 1 轮整表延后；第 2 轮凑到 2 把就用 2 把 ---
_tE = pd.DataFrame({"项目名称": ["P1", "P1"], "采购三级分类": ["A", "B"],
                    "合同编号": ["", ""], "金额": ["", ""]})
_se1 = pd.DataFrame({"项目名称": ["P1", "P1"], "采购三级分类": ["A", "B"],
                     "合同编号": ["C-A", "C-B"]})
_se2 = pd.DataFrame({"项目名称": ["P1", "P1"], "合同编号": ["C-A", "C-B"], "金额": ["11", "22"]})
_rE2 = fill_multi(_tE, key_cols=["项目名称"], sources=[{"name": "e1", "df": _se1},
                                                      {"name": "e2", "df": _se2}],
                  defer_single=True, max_rounds=4)
check("表级延后：单钥匙源表被延后（计数 ≥1）", _rE2["stats"]["延后源表数"] >= 1)
check("表级延后：第 2 轮凑到两把钥匙后才补（金额正确、备注含 2钥匙）",
      list(_rE2["result"]["金额"]) == ["11", "22"]
      and "2钥匙" in str(_rE2["confidence"].loc[0, "金额"]))

# --- 表级延后：始终只有 1 把 → 仍按 1 把匹配（不是留空）---
_tF = pd.DataFrame({"项目名称": ["Q1"], "金额": [""]})
_sF = pd.DataFrame({"项目名称": ["Q1"], "金额": ["77"]})
_rF2 = fill_multi(_tF, key_cols=["项目名称"], sources=[{"name": "f", "df": _sF}],
                  defer_single=True, max_rounds=4)
check("表级延后：始终单钥匙时照常补上（不留空）", str(_rF2["result"]["金额"].iloc[0]) == "77")
check("表级延后：关掉延后开关也能补上",
      str(fill_multi(_tF, key_cols=["项目名称"], sources=[{"name": "f", "df": _sF}],
                     defer_single=False)["result"]["金额"].iloc[0]) == "77")

# --- 复验：只用"少一把钥匙"的降级对照 → 只报"降级后给出不同值"的真矛盾 ---
_tK = pd.DataFrame({"K1": ["ZZZ", "ACME"], "K2": ["D", "B"], "V": ["", ""]})
_sK = pd.DataFrame({"K1": ["ZZZ", "ZZZ", "ZZZ", "ACME", "ACME6"],
                    "K2": ["D", "E", "D", "A", "B"],
                    "V": ["w1", "w2", "w1b", "v1", "v2"]})
_rK2 = fill_multi(_tK, key_cols=["K1"], sources=[{"name": "sK", "df": _sK}], audit_rounds=2)
check("复验：跑降级对照（自动去重）、给出可比格与一致率",
      _rK2["stats"]["复验组数"] >= 1 and _rK2["stats"]["复验可比格"] > 0
      and 0 <= _rK2["stats"]["复验一致率"] <= 100)
check("复验：降级后给出**不同值** → 记入明细（不进主清单）",
      _rK2["stats"]["复验不一致格"] > 0 and len(_rK2["audit"]) > 0
      and "复验不一致" not in set(_rK2["review"]["类型"]))
check("清单只收「必看」：多候选(已留空) / 未补上 两类",
      set(_rK2["review"]["类型"].astype(str).map(lambda s: s.split("(")[0])) <= {"多候选", "未补上"})
check("复验：降级后只是\"定不出来\"→ 不算存疑（不当噪声）",
      fill_multi(_tD, key_cols=["项目名称"], sources=[{"name": "sD", "df": _sD}],
                 audit_rounds=2)["stats"]["复验不一致格"] == 0)
check("复验：关掉复验则不跑（组数 0）",
      fill_multi(_tK, key_cols=["K1"], sources=[{"name": "sK", "df": _sK}],
                 audit_rounds=0)["stats"]["复验组数"] == 0)

# --- 回归：类型守卫（"到期/截止"日期口径 与 "终止"状态口径 不许混）---
check("列名：合同到期月份 ↔ 终止状态 不再判同义（曾误判 95）",
      col_match("合同到期月份", "终止状态")[0] < 70)
check("列名：截止日期 ↔ 截至日期 判同义（95）", col_match("截止日期", "截至日期")[0] >= 95)
_KSRC = pd.DataFrame({"合同到期月份": ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"],
                      "终止状态": ["未终止"] * 6, "值": ["v1", "v2", "v3", "v4", "v5", "v6"]})
_tM = pd.DataFrame({"钥匙": ["2026-05"], "合同到期月份": [""], "值": [""]})
_sM = pd.DataFrame({"钥匙": ["2026-05"], "合同到期月份": ["2026-05"], "终止状态": ["未终止"], "值": ["X"]})
_rM = fill_multi(_tM, key_cols=["钥匙"], sources=[{"name": "m", "df": _sM}])
check("类型守卫：日期类目标列不会被状态列(未终止)顶上",
      str(_rM["result"]["合同到期月份"].iloc[0]) == "2026-05")

# --- 回归：日期归一化（时间戳零点 → 只留日期）---
_tN = pd.DataFrame({"钥匙": ["k1"], "截止日期": [""]})
_sN = pd.DataFrame({"钥匙": ["k1"], "截止日期": [pd.Timestamp("2026-11-30")]})
_rN = fill_multi(_tN, key_cols=["钥匙"], sources=[{"name": "n", "df": _sN}])
check("日期归一化：2026-11-30 00:00:00 → 2026-11-30",
      str(_rN["result"]["截止日期"].iloc[0]) == "2026-11-30")

# --- 回归：第1轮歧义 → 第2轮被填上 → 不能再算歧义/不能出负数 ---
_tO = pd.DataFrame({"钥匙": ["P1"], "甲": [""], "乙": [""]})
_sO1 = {"name": "O1", "df": pd.DataFrame({"钥匙": ["P1"], "甲": ["only"]})}
_sO2 = {"name": "O2", "df": pd.DataFrame({"钥匙": ["P1", "P1"], "甲": ["only", "x"],
                                          "乙": ["d1", "d2"]})}
_rO = fill_multi(_tO, key_cols=["钥匙"], sources=[_sO1, _sO2])
_rv = _rO["stats"]
check("级联：第1轮歧义的格第2轮被填上 → 歧义格 0、留空不为负",
      str(_rO["result"]["乙"].iloc[0]) == "d1" and _rv["歧义格数"] == 0
      and _rv["未匹配(留空)"] >= 0 and _rv["留空合计"] >= 0)
check("统计自检：完全匹配+高+中+低+未匹配+歧义 == 行数×目标列数",
      (_rv["完全匹配(100%)"] + _rv["高置信(80-99%)"] + _rv["中置信(40-80%)"]
       + _rv["低置信(20-40%)"] + _rv["未匹配(留空)"] + _rv["歧义格数"])
      == _rv["模板行数"] * _rv["目标列数"])

# --- 上限：每张源表最多 4 把钥匙（你点 5 列也只取前 4）---
_tH = pd.DataFrame({f"k{i}": ["v"] for i in range(1, 6)})
_tH["要补"] = [""]
_sH = pd.DataFrame({f"k{i}": ["v"] for i in range(1, 6)})
_sH["要补"] = ["x"]
_plan = plan_keys(_tH, list(_tH.columns), [{"name": "sH", "df": _sH}],
                  user_keys=[f"k{i}" for i in range(1, 6)], max_keys=4)
check("上限：手点 5 列也只用 4 把", len(_plan["per_source"][0]) <= 4)
check("上限：fill_multi 里钥匙列也截到 4",
      len(fill_multi(_tH, key_cols=[f"k{i}" for i in range(1, 6)],
                     sources=[{"name": "sH", "df": _sH}])["key_pairs"][0]) <= 4)

# --- 多源交叉核对：两张源表都能供同一列 → 两源都给值且不同 = 两源矛盾 ---
_tX = pd.DataFrame({"钥匙": ["k1", "k2"], "值": ["", ""]})
_xA = {"name": "源A", "df": pd.DataFrame({"钥匙": ["k1", "k2"], "值": ["X1", "X2"]})}
_xB = {"name": "源B", "df": pd.DataFrame({"钥匙": ["k1", "k2"], "值": ["X1", "Y2"]})}
_rX = fill_multi(_tX, key_cols=["钥匙"], sources=[_xA, _xB])
check("两源交叉核对：可比 2 格、矛盾 1 格（行3 两源不同）、一致率 50%",
      _rX["stats"]["两源可核对格"] == 2 and _rX["stats"]["两源矛盾格"] == 1
      and _rX["stats"]["两源一致率"] == 50.0
      and len(_rX["cross"]) == 1 and int(_rX["cross"]["行号"].iloc[0]) == 3)
check("两源交叉核对：只有一张源表能供时不产生矛盾",
      fill_multi(_tK, key_cols=["K1"], sources=[{"name": "sK", "df": _sK}])["stats"]["两源可核对格"] == 0)

# --- 复验默认已关（改为"少一把钥匙"的对照仅供开发回测）---
check("复验默认关：默认调用不跑对照（组数 0）",
      _rX["stats"]["复验组数"] == 0)

# --- 清单导出：Sheet2「需人工确认」，复验明细单独一个 Sheet ---
_rI = _rK2
_pI = os.path.join(os.path.dirname(TMP), "fill_review.xlsx")
export_filled(_rI["result"], _rI["confidence"], _pI, stats=_rI["stats"],
              review_df=_rI["review"], audit_df=_rI["audit"])
_wbI = load_workbook(_pI)
check("清单导出：Sheet2「需人工确认」列名齐全（类型/行号/列名）",
      "需人工确认" in _wbI.sheetnames
      and [c.value for c in _wbI["需人工确认"][1]][:4] == ["类型", "行号", "列名", "钥匙值"])
check("清单导出：复验明细写在「复验存疑」Sheet（不混进主清单）",
      "复验存疑" in _wbI.sheetnames
      and [c.value for c in _wbI["复验存疑"][1]][:2] == ["行号", "列名"])

print(f"\n===== 多表补全测试通过：{ok} 项断言（离线）=====")
