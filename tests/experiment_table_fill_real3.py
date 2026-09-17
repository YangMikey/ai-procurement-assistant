# -*- coding: utf-8 -*-
"""真实三文件回归（离线）：金蝶/合约规划 → 补 采购项目汇总（用户 2026-09-15 实测场景）。

守住的 bug：同义词族把「到期」和「终止」算一族 → 合同到期月份/截止日期 被源表"终止状态"
（未终止/已终止）顶上；以及日期时间戳显示成 2026-11-30 00:00:00。

报告：data/outputs/多表补全_真实三文件回归.txt
运行：py tests/experiment_table_fill_real3.py
"""
import glob
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd

from core.excel_io import (detect_header_block_bottom, list_sheets_from_bytes,
                           read_table, split_two_row_header, unmerge_fill_data)
from core.table_filler import fill_multi

GT = os.path.join(_ROOT, "data", "ground_truth")
OUT = os.path.join(_ROOT, "data", "outputs", "多表补全_真实三文件回归.txt")
ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def find(pat):
    hits = sorted(glob.glob(os.path.join(GT, pat)))
    return hits[0] if hits else None


def load_flow(path, hdr_pick, sheet_kw=None):
    """模拟 app.load_table：表头探测/降级 + 两行表头拆分 + 合并填充。

    sheet_kw：按关键字挑 Sheet（如"规划明细"）；None 则用第一个。
    """
    with open(path, "rb") as f:
        fb = f.read()
    sheets = list_sheets_from_bytes(fb, os.path.basename(path))
    sheet = sheets[0]
    if sheet_kw:
        for s in sheets:
            if sheet_kw in s:
                sheet = s
                break
    bottom = detect_header_block_bottom(fb, path, sheet, hdr_pick)
    hdr = bottom or hdr_pick
    names, is_two = split_two_row_header(fb, path, sheet, hdr)
    df = read_table(path, sheet, header_row=hdr)
    if names and is_two:
        df = df.rename(columns={df.columns[j]: names[j]
                                for j in range(min(len(names), len(df.columns)))})
    df, _n = unmerge_fill_data(fb, path, sheet, hdr, df)
    return df


TPL = find("*2026*12*.xlsx")
S1 = find("1*合约规划*.xlsx")
S2 = find("*金蝶*.xlsx")
if not (TPL and S1 and S2):
    print("（跳过：data/ground_truth 里没有这三份真实文件）")
else:
    tpl = load_flow(TPL, 1, "Sheet1")
    tpl = tpl.rename(columns={c: str(c).strip() for c in tpl.columns})
    keys = ["项目名称", "采购三级分类"]
    tgts = ["事业部", "起始日期", "截止日期", "合同到期月份", "合同编号"]
    src1 = load_flow(S1, 5, "\u89c4\u5212\u660e\u7ec6")
    src1 = src1.rename(columns={c: str(c).strip() for c in src1.columns})
    src2 = load_flow(S2, 2, "sheet1")
    src2 = src2.rename(columns={c: str(c).strip() for c in src2.columns})
    r = fill_multi(tpl, key_cols=keys,
                   sources=[{"name": "合约规划明细表", "df": src1},
                            {"name": "金蝶对账口径", "df": src2}])
    res, st, rv = r["result"], r["stats"], r["review"]
    BAD = ["未终止", "已终止", "已生效", "未生效", "未关闭", "已关闭", "冻结"]

    def vals(cols):
        out = []
        for i in res.index:
            for c in cols:
                v = res.at[i, c]
                if str(v).strip() not in ("", "nan"):
                    out.append(str(v))
        return out

    date_vals = vals(["起始日期", "截止日期", "合同到期月份"])
    check("日期类列没有被状态文本(未终止/已终止…)顶上",
          not any(any(b in v for b in BAD) for v in date_vals))
    check("合同编号列没有被状态文本顶上",
          not any(any(b in v for b in BAD) for v in vals(["合同编号"])))
    check("日期类列的值不再带 00:00:00（归一化生效）",
          not any("00:00:00" in v for v in date_vals))
    check("统计自检：各档之和 == 行数 × 目标列数",
          (st["完全匹配(100%)"] + st["高置信(80-99%)"] + st["中置信(40-80%)"]
           + st["低置信(20-40%)"] + st["未匹配(留空)"] + st["歧义格数"])
          == st["模板行数"] * st["目标列数"])
    check("未匹配(留空) 不为负且 ≤ 留空合计",
          0 <= st["未匹配(留空)"] <= st["留空合计"])
    check("清单只含「多候选 / 未补上」两类",
          set(rv["类型"].astype(str).map(lambda s: s.split("(")[0])) <= {"多候选", "未补上"}
          )
    check("两源交叉核对：有可比格，且矛盾格数 ≤ 可比格（合同编号两源都供）",
          st["两源可核对格"] > 0 and 0 <= st["两源矛盾格"] <= st["两源可核对格"])
    check("85% 闸门：两表同名不同口径（起始日期 59%/合同编号 11%）→ 拒绝互补、不硬补",
          st["互补格数"] == 0 and len(st.get("拒绝互补列") or []) >= 1)

    # ---- 验收：用户的真实痛点（钥匙少一把 → 多候选）----
    # 金蝶有「合同二级分类」（表头 三级≠二级 被判冲突）但取值与模板「采购三级分类」高度重合
    # → 工具应出「列口径」题；确认后它成为第 2 把钥匙 → 多候选应显著减少、对率不变
    from core.conventions import ConventionStore
    from core.table_filler import apply_col_answer
    _cs = os.path.join(os.environ.get("TEMP", "."), "opencode", "conv_real3.json")
    os.makedirs(os.path.dirname(_cs), exist_ok=True)
    if os.path.exists(_cs):
        os.remove(_cs)
    cst = ConventionStore(_cs)
    r2 = fill_multi(tpl, key_cols=keys, sources=[{"name": "合约规划明细表", "df": src1},
                                                 {"name": "金蝶对账口径", "df": src2}],
                    conventions=cst, key_min=80)
    _qs = r2.get("value_questions") or []
    _colq = [q for q in _qs if q.get("类型") == "列口径" and q.get("源表") == "金蝶对账口径"]
    check("列口径题：金蝶「合同二级分类」被认出（表头冲突但值域重合）并出题问一次",
          bool(_colq) and _colq[0]["源列"] == "合同二级分类")
    _n_multi_before = int(r2["stats"]["歧义格数"])
    if _colq:
        apply_col_answer(tpl, [{"name": "金蝶对账口径", "df": src2}], _colq[0]["列名"],
                         "金蝶对账口径", _colq[0]["源列"], "same", cst)
    r3 = fill_multi(tpl, key_cols=keys, sources=[{"name": "合约规划明细表", "df": src1},
                                                 {"name": "金蝶对账口径", "df": src2}],
                    conventions=cst, key_min=80)
    _keys_desc = dict(r3["stats"]["实际钥匙列"])
    check("确认列口径后：金蝶的钥匙里出现「采购三级分类」（第二把钥匙生效）",
          "采购三级分类" in str(_keys_desc.get("金蝶对账口径", "")))
    _vq = [q for q in (r3.get("value_questions") or [])
           if q.get("类型") == "整列" and q.get("列名") == "采购三级分类"]
    check("配对后再出值域题：源值多出的后缀能学成规律（消防维保 ↔ 消防维保类采购合同）",
          bool(_vq) and _vq[0]["覆盖率"] >= 60)
    check("确认列口径后：歧义不增加（剩余歧义=源表自身多行/分类缺失，属无解）",
          int(r3["stats"]["歧义格数"]) <= _n_multi_before)
    check("确认列口径后：事业部填出的行数不减少（不因新增钥匙而退化）",
          int((r3["result"]["事业部"].astype(str).str.strip() != "").sum()) >=
          int((res["事业部"].astype(str).str.strip() != "").sum()))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    lines = ["多表补全 · 真实三文件回归",
             "=" * 44,
             f"模板：{os.path.basename(TPL)} Sheet1（{st['模板行数']} 行 × {st['目标列数']} 目标列）",
             f"源表1：{os.path.basename(S1)} 合约规划明细表 (4-12月)（{len(src1)} 行 × {len(src1.columns)} 列）",
             f"源表2：{os.path.basename(S2)} sheet1（{len(src2)} 行 × {len(src2.columns)} 列）",
             "",
             "【实际用的钥匙列】"]
    for nm, kd in st["实际钥匙列"]:
        lines.append(f"  {nm}：{kd}")
    lines += ["", "【统计】"]
    for k in ("完全匹配(100%)", "高置信(80-99%)", "中置信(40-80%)", "低置信(20-40%)",
              "未匹配(留空)", "留空合计", "歧义格数", "需人工确认行数",
              ):
        lines.append(f"  {k} = {st.get(k)}")
    lines += ["", f"【需人工确认】{len(rv)} 行（多候选/未补上）"]
    for _, row in rv.head(20).iterrows():
        lines.append(f"  {row['类型']}｜行{row['行号']}｜{row['列名']}｜{row['钥匙值']}")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    check("报告已生成", os.path.exists(OUT))
    print(f"报告：{OUT}")

print(f"\n===== 真实三文件回归通过：{ok} 项断言 =====")
