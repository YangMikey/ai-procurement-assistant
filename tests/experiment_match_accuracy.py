# -*- coding: utf-8 -*-
"""M1 客观回测：两表匹配置信度评估（用人工基准，避免自嗨）。

匹配表 = data/ground_truth/匹配表_001_合同进度表.xlsx（取写回前的原始快照 Sheet，A 列空）
钥匙表 + 人工基准 = data/ground_truth/钥匙表_采购项目汇总.xlsx · 「事业部匹配结果」
    · 该 Sheet 是**人工做出来的**匹配结果（含「匹配方式」「未匹配」），当 ground truth
评估：把我们的 run_match 预测的「事业部」与人工结果逐行比对 → 精确率 / 召回率 / 准确率
运行：py tests/experiment_match_accuracy.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.excel_io import (detect_header_block_bottom, read_table,
                           split_two_row_header, unmerge_fill_data)
from core.matcher import run_match

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M_PATH = os.path.join(ROOT, "data", "ground_truth", "匹配表_001_合同进度表.xlsx")
K_PATH = os.path.join(ROOT, "data", "ground_truth", "钥匙表_采购项目汇总.xlsx")
OUT_DIR = os.path.join(ROOT, "data", "outputs")
M_HDR_SHEET = "ai副本合同进度表"        # 写回前的原始快照（A 列空）
GT_SHEET = "事业部匹配结果"

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def load_two_row(path, sheet, hdr_pick=1):
    with open(path, "rb") as f:
        fb = f.read()
    bottom = detect_header_block_bottom(fb, path, sheet, hdr_pick)
    hdr = bottom or hdr_pick
    names, is_two = split_two_row_header(fb, path, sheet, hdr)
    df = read_table(path, sheet, header_row=hdr)
    if names and is_two:
        df = df.rename(columns={df.columns[j]: names[j]
                                for j in range(min(len(names), len(df.columns)))})
    df, _ = unmerge_fill_data(fb, path, sheet, hdr, df)
    return df, hdr


# ---- 读匹配表（原始快照） ----
with open(M_PATH, "rb") as _f:
    pass
import openpyxl
wb = openpyxl.load_workbook(M_PATH, read_only=True)
sheets = wb.sheetnames
wb.close()
check("匹配表存在写回前快照 Sheet", M_HDR_SHEET in sheets)
m_df, m_hdr = load_two_row(M_PATH, M_HDR_SHEET, 1)
check("匹配表列（项目/类别）", "项目" in m_df.columns and "类别" in m_df.columns)

# ---- 读人工基准 ----
gt = read_table(K_PATH, GT_SHEET, header_row=1)
check("基准列（项目名称/采购三级分类/事业部/匹配方式）",
      all(c in gt.columns for c in ["项目名称", "采购三级分类", "事业部", "匹配方式"]))

# ---- 预测 ----
logs = []
res = run_match(m_df, gt, ["项目", "类别"], ["项目名称", "采购三级分类"], [], [],
                ["事业部"], mode="fuzzy", threshold=80.0, log=logs.append,
                fetch_alias={"事业部": "事业部"})
stats = res["stats"]
print("统计：", {k: v for k, v in stats.items() if v})

# 逐行取预测（重复预警取匹配率最高的一条；未匹配/人工忽略 → 空）
def predict(i):
    st = res["row_status"][i]
    if not st["combos"]:
        return ""
    best = max(st["combos"], key=lambda t: t[1])
    return gt.iloc[best[0]]["事业部"]


def norm(v):
    try:
        import pandas as pd
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
    except Exception:
        if v is None:
            return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none") else s


# 人工基准： (项目名称, 采购三级分类) -> 事业部
gt_map = {}
for _, r in gt.iterrows():
    key = (norm(r["项目名称"]), norm(r["采购三级分类"]))
    if key not in gt_map or (not gt_map[key] and norm(r["事业部"])):
        gt_map[key] = norm(r["事业部"])

tp = fp = fn = tn_noexp = 0
skipped = 0
detail = []
for i in range(len(m_df)):
    key = (norm(m_df.iloc[i]["项目"]), norm(m_df.iloc[i]["类别"]))
    if key not in gt_map:          # 匹配表里但基准没有 → 不计入
        skipped += 1
        continue
    expected = gt_map[key]
    got = norm(predict(i))
    if expected:
        if got == expected:
            tp += 1
        elif got:
            fp += 1
        else:
            fn += 1
    else:
        if got:
            fp += 1
        else:
            tn_noexp += 1
    detail.append((i + 1, key[0], key[1], expected, got))

precision = tp / (tp + fp) if (tp + fp) else 1.0
recall = tp / (tp + fn) if (tp + fn) else 1.0
n_eval = tp + fp + fn + tn_noexp
accuracy = (tp + tn_noexp) / n_eval if n_eval else 1.0

lines = []
lines.append("=" * 72)
lines.append("两表匹配 · 客观回测（人工基准：事业部匹配结果）")
lines.append(f"匹配表行数={len(m_df)}  可评估={n_eval}  不在基准中(跳过)={skipped}")
lines.append(f"TP={tp}  FP={fp}  FN={fn}  基准为未匹配且我们也没配={tn_noexp}")
lines.append(f"精确率={precision:.3f}  召回率={recall:.3f}  总准确率={accuracy:.3f}")
lines.append("-" * 72)
lines.append("逐行（行号 | 项目 | 类别 | 人工 | 预测 | 判定）")
for (ln, proj, cat, exp, got) in detail:
    verdict = "OK" if exp == got else ("误配" if (exp and got) else ("漏配" if exp else "多配"))
    lines.append(f"{ln:>3} | {proj} | {cat} | {exp or '∅'} | {got or '∅'} | {verdict}")
report = "\n".join(lines)
print(report)
os.makedirs(OUT_DIR, exist_ok=True)
with open(os.path.join(OUT_DIR, "匹配准确率报告.txt"), "w", encoding="utf-8") as f:
    f.write(report + "\n")

check("回测：可评估行数 > 20", n_eval > 20)
check("回测：精确率 ≥ 0.60", precision >= 0.60)
check("回测：召回率 ≥ 0.60", recall >= 0.60)
print(f"\n===== 两表匹配客观回测完成：{ok} 项断言；报告见 data\\outputs\\匹配准确率报告.txt =====")
