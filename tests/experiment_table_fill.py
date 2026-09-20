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
tail_txt = "".join(str(ws.cell(row=rr, column=1).value or "") for rr in range(ws.max_row - 14, ws.max_row + 1))
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

# 回归：标签必须按候选自带的 source 索引取表名（曾有 bug：用了循环遗留变量 → 全写成最后一张表）
_sheet_a = pd.DataFrame({"项目名称": ["P1", "P2"], "起始日期": ["2026-01-01", "2026-02-01"]})
_sheet_b = pd.DataFrame({"项目名称": ["P1"], "起始日期": ["2026-03-01"]})
_src_sheet = [{"name": "合约规划明细表", "df": _sheet_a, "sheet": "合约规划明细表 (4-12月)"},
              {"name": "金蝶对账", "df": _sheet_b, "sheet": "sheet1"}]
_sup3 = discover_supply(["起始日期"], _src_sheet, 70)
_lbl3 = [supply_option_label(c, _src_sheet) for c in _sup3["起始日期"]]
check("列供给标签：多源各带各的表名（不再张冠李戴）",
      any(x.startswith("合约规划明细表") for x in _lbl3)
      and any(x.startswith("金蝶对账") for x in _lbl3))
check("列供给标签：同一目标列的两个来源都列出（915/199 场景）", len(_lbl3) == 2)
check("列供给标签：附工作表名（多 sheet 文件可分辨）",
      all("〔" in x for x in _lbl3))
_miss = supply_option_label({"source": 99, "col": "X", "how": "同名", "score": 100}, _src_sheet)
check("列供给标签：越界 source 不崩（回退 ?）", _miss.startswith("?"))

# ================= 新增：确定性机制（结构型 / canon 默认 / 修饰词碰撞 / 区分位 / 双源印证） =================
from core.table_filler import (_diff_discriminating, _disc_series, _key_sim,
                               _strippable_tokens, value_kind)

check("结构型判定：日期/年月/金额/编号/文本 分类正确",
      value_kind("2026-01-01") == "date" and value_kind("2026-11") == "date"
      and value_kind("2026年1月1日") == "date" and value_kind("1,000") == "num"
      and value_kind("YC-XMYC0122-WF02-26-0001") == "code"
      and value_kind("怡安花园") == "text" and value_kind("3号楼") == "text")
check("结构型：日期格式归一（2026/1/1 = 2026-01-01 = 2026年1月1日 = 时间戳）",
      _key_sim("2026/1/1", "2026-01-01") == 100
      and _key_sim("2026年1月1日", "2026-01-01") == 100
      and _key_sim("2026-01-01 00:00:00", "2026-01-01") == 100)
check("结构型：金额去千分位/小数归一（1,000 = 1000；26.10 = 26.1）",
      _key_sim("1,000", "1000") == 100 and _key_sim("26.10", "26.1") == 100)
check("结构型：编号差 1 个字符 → 0（模糊分不适用，绝不猜行）",
      _key_sim("YC-XMYC10033-WF01-26-0001", "YC-XMYC1003-WF01-26-0001") == 0
      and _key_sim("2026-11", "2026-12") == 0)
check("canon 默认：1事业部 = 一事业部 = 第一事业部（100，不等口径本学习）",
      _key_sim("1事业部", "一事业部") == 100 and _key_sim("第一事业部", "一事业部") == 100)
check("canon 保区分度：第一 ≠ 第二、叠溪三期 ≠ 四期（不满分）",
      _key_sim("第一事业部", "第二事业部") < 100
      and _key_sim("叠溪花园三期", "叠溪花园四期") < 100)
check("区分位差异识别：三/四 算区分位；楼/搂（含非区分位字）不算",
      _diff_discriminating("叠溪花园3", "叠溪花园4")
      and not _diff_discriminating("3号楼巡查", "3号搂巡查"))
check("碰撞裁决（各表内部判）：同表两种形态并存 → 不许剥；跨表两种写法 → 允许剥",
      "委外" not in _strippable_tokens({"绿化养护"}, {"绿化养护", "绿化养护委外"})
      and "项目" in _strippable_tokens({"华夏中央广场"}, {"华夏中央广场项目"}))

# 填充级：修饰差异 → 直接精确命中（100 无色）；区分位+有兄弟 → 留空不猜
_tpl5 = pd.DataFrame({"项目名称": ["华夏中央广场"], "事业部": [""]})
_s5a = pd.DataFrame({"项目名称": ["华夏中央广场项目"], "事业部": ["华二第三事业部"]})
_r5 = fill_multi(_tpl5, key_cols=["项目名称"], sources=[{"name": "源", "df": _s5a}])
check("修饰差异等价：华夏中央广场 ← 华夏中央广场项目 直接精确命中（100 无色）",
      str(_r5["result"].at[0, "事业部"]).strip() == "华二第三事业部"
      and str(_r5["confidence"].at[0, "事业部"]).startswith("ok"))

_tpl6 = pd.DataFrame({"项目名称": ["叠溪花园三期"], "事业部": [""]})
_s6a = pd.DataFrame({"项目名称": ["叠溪花园四期", "叠溪花园五期"], "事业部": ["甲部", "乙部"]})
_r6a = fill_multi(_tpl6, key_cols=["项目名称"], sources=[{"name": "源", "df": _s6a}])
check("区分位+值域确有兄弟（四期/五期并存）：三期模板 → 留空不猜行",
      not str(_r6a["result"].at[0, "事业部"]).strip())
_s6b = pd.DataFrame({"项目名称": ["叠溪花园四期"], "事业部": ["甲部"]})
_r6b = fill_multi(_tpl6, key_cols=["项目名称"], sources=[{"name": "源", "df": _s6b}])
check("区分位但无兄弟值（只有四期一个值）：照常模糊填（黄色，人工再确认）",
      str(_r6b["result"].at[0, "事业部"]).strip() == "甲部"
      and str(_r6b["confidence"].at[0, "事业部"]).startswith("high"))

_tpl7 = pd.DataFrame({"项目名称": ["保利花园", "绿城小区"], "服务费": ["", ""]})
_s7a = pd.DataFrame({"项目名称": ["保利花园城", "绿城小区"], "服务费": ["1000", "2000"]})
_s7b = pd.DataFrame({"项目名称": ["保利花园", "绿城小区"], "服务费": ["1000", "9999"]})
_r7b = fill_multi(_tpl7, key_cols=["项目名称"],
                  sources=[{"name": "甲表", "df": _s7a}, {"name": "乙表", "df": _s7b}])
check("双源印证：两源独立命中一致 → 高置信格自动升为 100（无色）",
      str(_r7b["confidence"].at[0, "服务费"]).startswith("ok")
      and "双源印证" in str(_r7b["confidence"].at[0, "服务费"])
      and _r7b["stats"]["双源印证格数"] >= 1)
check("两源矛盾照常：不一致 → 进矛盾清单、不升级、保持原填值",
      "双源印证" not in str(_r7b["confidence"].at[1, "服务费"])
      and _r7b["stats"]["两源矛盾格"] >= 1)

# ---- 链式印证：精确命中 + 所用钥匙格全部可信 → 继承印证升 100（复刻"单源列级联1跳"场景）----
# S2 同名两行事业部不同 → 只用项目名称是歧义（留空）；第2轮靠补出的合同编号消歧 → 90%
# 链头（合同编号，第1轮由 S1 精确填出）可信 → 继承印证升 100
# S1 供合同编号（2 值排前），S2 供事业部；S2 同名两行事业部不同 → 需补出的合同编号消歧 → 90% → 继承升 100
_tpl8 = pd.DataFrame({"项目名称": ["保利花园", "玫瑰花园"], "采购三级分类": ["绿化养护", "绿化养护"],
                      "合同编号": ["", ""], "事业部": ["", ""]})
_s8a = pd.DataFrame({"项目名称": ["保利花园", "玫瑰花园"], "采购三级分类": ["绿化养护", "绿化养护"],
                     "合同编号": ["CT-A", "CT-C"]})
_s8b = pd.DataFrame({"项目名称": ["保利花园", "保利花园"],
                     "合同编号": ["CT-A", "CT-B"], "事业部": ["甲部", "乙部"]})
_r8 = fill_multi(_tpl8, key_cols=["项目名称", "采购三级分类"],
                 sources=[{"name": "S1", "df": _s8a}, {"name": "S2", "df": _s8b}])
check("链式印证：级联1跳(90%) + 链头(合同编号)可信 → 自动升 100 无色",
      str(_r8["confidence"].at[0, "事业部"]).startswith("ok")
      and "链式印证" in str(_r8["confidence"].at[0, "事业部"])
      and _r8["stats"]["链式印证格数"] >= 1)

_s8c = pd.DataFrame({"项目名称": ["保利花园城", "玫瑰花园"], "采购三级分类": ["绿化养护", "绿化养护"],
                     "合同编号": ["CT-A", "CT-C"]})
_r8b = fill_multi(_tpl8, key_cols=["项目名称", "采购三级分类"],
                  sources=[{"name": "S1", "df": _s8c}, {"name": "S2", "df": _s8b}])
check("链头模糊(90%)不可信 → 不继承，保持黄色（人看）",
      str(_r8b["confidence"].at[0, "事业部"]).startswith("high")
      and "链式印证" not in str(_r8b["confidence"].at[0, "事业部"]))

_tpl9 = pd.DataFrame({"项目名称": ["保利花园"], "事业部": [""]})
_s9 = pd.DataFrame({"项目名称": ["保利花园城"], "事业部": ["甲部"]})
_r9 = fill_multi(_tpl9, key_cols=["项目名称"], sources=[{"name": "S2", "df": _s9}])
check("模糊命中（匹配分<100）永不继承（保持黄色）",
      str(_r9["confidence"].at[0, "事业部"]).startswith("high")
      and "链式印证" not in str(_r9["confidence"].at[0, "事业部"]))

# ---- 同名互补豁免：首选列该行没值 → 同名的第 2 候选列补上（豁免 85% 闸门），黄色·互补 ----
from core.table_filler import all_supply_options

_namesA = [f"P{i}" for i in range(1, 13)]
_t10 = pd.DataFrame({"项目名称": _namesA, "起始日期": [""] * 12})
_s10a = pd.DataFrame({"项目名称": _namesA,
                      "起始日期": ["2026-01-01"] * 11 + [""]})     # P12 没值
_s10b = pd.DataFrame({"项目名称": _namesA,
                      "起始日期": [f"2026-06-{i:02d}" for i in range(1, 13)]})  # 与甲表全不同 → 一致率 0
_r10 = fill_multi(_t10, key_cols=["项目名称"],
                  sources=[{"name": "甲表", "df": _s10a}, {"name": "乙表", "df": _s10b}])
check("同名互补豁免：首选列缺值的行 → 同名第2候选补上（黄色·互补）",
      str(_r10["result"].at[11, "起始日期"]).startswith("2026-06-12")
      and str(_r10["confidence"].at[11, "起始日期"]).startswith("high")
      and "互补" in str(_r10["confidence"].at[11, "起始日期"])
      and _r10["stats"]["互补格数"] >= 1)
check("同名互补豁免：非缺值的行仍来自首选列（100 无色，不走互补）",
      str(_r10["result"].at[10, "起始日期"]) == "2026-01-01"
      and str(_r10["confidence"].at[10, "起始日期"]).startswith("ok"))

# ---- 非同名次选仍受 85% 闸门：同义列(结束日期)一致率低 → 拒绝互补、留空 ----
_t11 = pd.DataFrame({"项目名称": _namesA, "截止日期": [""] * 12})
_s11a = pd.DataFrame({"项目名称": _namesA, "截止日期": ["2026-01-01"] * 11 + [""]})
_s11b = pd.DataFrame({"项目名称": _namesA, "结束日期": [f"2026-07-{i:02d}" for i in range(1, 13)]})
_r11 = fill_multi(_t11, key_cols=["项目名称"],
                  sources=[{"name": "甲表", "df": _s11a}, {"name": "乙表", "df": _s11b}])
check("非同名次选仍受 85% 闸门：同义列一致率 0% → 拒绝互补、留空",
      not str(_r11["result"].at[11, "截止日期"]).strip()
      and any("截止日期" in x for x in _r11["stats"]["拒绝互补列"]))

# ---- 同名 100 候选排序：有值多的排第一（默认选中=它）----
_s13a = pd.DataFrame({"项目名称": list("ABCDEFG"),
                      "起始日期": ["2026-01-01"] * 5 + [None, None]})
_s13b = pd.DataFrame({"项目名称": list("ABCDEFG"), "起始日期": ["2026-02-01"] * 7})
_o13 = all_supply_options("起始日期", [{"name": "甲", "df": _s13a}, {"name": "乙", "df": _s13b}], 70)
_s13 = [c for c in _o13 if c["how"] == "同名" and c["score"] >= 100]
check("同名100候选排序：有值多的排第一（默认选中=它）",
      len(_s13) == 2 and _s13[0]["source"] == 1
      and _s13[0]["filled"] == 7 and _s13[1]["filled"] == 5)


# ================= 新增：自动配钥匙（值域/同义/择优/延后/两源交叉） =================
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
check("表级延后：第 2 轮凑到两把钥匙后才补（金额正确、备注含 2钥匙或已链式印证）",
      list(_rE2["result"]["金额"]) == ["11", "22"]
      and ("2钥匙" in str(_rE2["confidence"].loc[0, "金额"])
           or "链式印证" in str(_rE2["confidence"].loc[0, "金额"])))

# --- 表级延后：始终只有 1 把 → 仍按 1 把匹配（不是留空）---
_tF = pd.DataFrame({"项目名称": ["Q1"], "金额": [""]})
_sF = pd.DataFrame({"项目名称": ["Q1"], "金额": ["77"]})
_rF2 = fill_multi(_tF, key_cols=["项目名称"], sources=[{"name": "f", "df": _sF}],
                  defer_single=True, max_rounds=4)
check("表级延后：始终单钥匙时照常补上（不留空）", str(_rF2["result"]["金额"].iloc[0]) == "77")
check("表级延后：关掉延后开关也能补上",
      str(fill_multi(_tF, key_cols=["项目名称"], sources=[{"name": "f", "df": _sF}],
                     defer_single=False)["result"]["金额"].iloc[0]) == "77")


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
_tK = pd.DataFrame({"K1": ["k1"], "值": [""]})
_sK = pd.DataFrame({"K1": ["k1"], "值": ["X1"]})
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

# --- 清单导出：Sheet2「需人工确认」+「两源矛盾」---
_rI = fill_multi(pd.DataFrame({"钥匙": ["k1"], "值": [""]}), key_cols=["钥匙"],
                 sources=[{"name": "f1", "df": pd.DataFrame({"钥匙": ["k1", "k1"],
                                                            "值": ["X", "Y"]})}])
_pI = os.path.join(os.path.dirname(TMP), "fill_review.xlsx")
export_filled(_rI["result"], _rI["confidence"], _pI, stats=_rI["stats"],
              review_df=_rI["review"], cross_df=_rX.get("cross"))
_wbI = load_workbook(_pI)
check("清单导出：Sheet2「需人工确认」列名齐全（类型/行号/列名）",
      "需人工确认" in _wbI.sheetnames
      and [c.value for c in _wbI["需人工确认"][1]][:4] == ["类型", "行号", "列名", "钥匙值"])
check("清单导出：两源矛盾单独一个 Sheet（不进主清单）",
      "两源矛盾" in _wbI.sheetnames
      and [c.value for c in _wbI["两源矛盾"][1]][:2] == ["行号", "列名"])

print(f"\n===== 多表补全测试通过：{ok} 项断言（离线）=====")
