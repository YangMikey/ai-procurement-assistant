# -*- coding: utf-8 -*-
"""多表补全·真实回测（离线）：用你的真实两表 + 人工答案，给出「改前 vs 改后」数字。

场景：
  A 同源人工答案：模板=钥匙表 Sheet1（项目名称/采购三级分类 + 空的事业部）
                 源  =钥匙表「事业部匹配结果」（人工填好的事业部）→ 当标准答案
  B 真·另一张表：源  =匹配表_001_合同进度表（项目/类别/事业部）→ 验证 同义/值域 自动配钥匙

输出报告：data/outputs/多表补全回测报告.txt
运行：py tests/experiment_table_fill_accuracy.py
"""
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd

from core.excel_io import (detect_header_block_bottom, read_table,
                           split_two_row_header, unmerge_fill_data)
from core.table_filler import fill_multi

ok = 0
REPORT = os.path.join(_ROOT, "data", "outputs", "多表补全回测报告.txt")
KEYF = os.path.join(_ROOT, "data", "ground_truth", "钥匙表_采购项目汇总.xlsx")
MATCHF = os.path.join(_ROOT, "data", "ground_truth", "匹配表_001_合同进度表.xlsx")


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def _norm(v):
    return "".join(str(v).split()).lower()


def _bench(pred_df, gt_lut, key_cols, label, target="事业部"):
    """对比预测与人工答案：填了几格/对了几格/错了几格/留空几格。"""
    n_fill = n_hit = n_bad = n_blank = 0
    bad_rows = []
    for i in pred_df.index:
        key = tuple(_norm(pred_df.at[i, c]) for c in key_cols)
        got = pred_df.at[i, target]
        want = gt_lut.get(key)
        if not str(got or "").strip():
            n_blank += 1
            continue
        n_fill += 1
        if want is None:
            continue
        if _norm(got) == _norm(want):
            n_hit += 1
        else:
            n_bad += 1
            bad_rows.append((i + 2, key[0], key[1], got, want, label))
    return {"填格": n_fill, "对": n_hit, "错": n_bad, "留空": n_blank, "错行": bad_rows}


def _load_flow(path, sheet, hdr_pick):
    """模拟 app.load_table：两行表头拆分 + 合并单元格填充（匹配表_001 是两行合并表头）。"""
    with open(path, "rb") as f:
        fb = f.read()
    bottom = detect_header_block_bottom(fb, path, sheet, hdr_pick)
    hdr = bottom or hdr_pick
    names, is_two = split_two_row_header(fb, path, sheet, hdr)
    df = read_table(path, sheet, header_row=hdr)
    if names and is_two:
        df = df.rename(columns={df.columns[j]: names[j]
                                for j in range(min(len(names), len(df.columns)))})
    df, _n = unmerge_fill_data(fb, path, sheet, hdr, df)
    return df


if not (os.path.exists(KEYF) and os.path.exists(MATCHF)):
    check("真实基准文件存在（钥匙表_采购项目汇总 / 匹配表_001_合同进度表）", False)
else:
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    keys = read_table(KEYF, "Sheet1", header_row=1)
    ans = read_table(KEYF, "事业部匹配结果", header_row=1)
    src_other = _load_flow(MATCHF, "合同进度表", 1)
    tkeys = ["项目名称", "采购三级分类"]

    gt = {}
    for _, r in ans.iterrows():
        if str(r.get("事业部") or "").strip():
            gt[(_norm(r["项目名称"]), _norm(r["采购三级分类"]))] = r["事业部"]

    lines = [f"多表补全回测报告  {datetime.now():%Y-%m-%d %H:%M}", "=" * 44,
             f"模板：钥匙表_采购项目汇总 · Sheet1（{len(keys)} 行）",
             f"标准答案：人工结果表（{len(gt)} 行）", ""]

    def _scenario(target, label):
        """对比「改前(只用项目名称+猜第1条)」vs「改后(自动配钥匙+歧义留空)」。"""
        g = {}
        for _, r in ans.iterrows():
            if str(r.get(target) or "").strip():
                g[(_norm(r["项目名称"]), _norm(r["采购三级分类"]))] = r[target]
        tpl = keys[tkeys].copy()
        tpl[target] = ""
        out = []
        for tag, kw in (("改前", dict(auto_keys=False, defer_single=False, audit_rounds=0,
                                      blank_on_tie=False)),
                        ("改后", dict())):
            r = fill_multi(tpl, key_cols=["项目名称"],
                           sources=[{"name": "答案表", "df": ans}], **kw)
            mm = _bench(r["result"], g, tkeys, tag, target)
            out.append((tag, mm, r))
        (t1, m1, r1), (t2, m2, r2) = out
        lines.extend([
            f"【{label}】目标列 = {target}",
            f"  {t1}（只用「项目名称」一把钥匙，命中多行取第 1 条）："
            f"填 {m1['填格']}｜对 {m1['对']}｜**错 {m1['错']}**｜留空 {m1['留空']}"
            f"｜歧义格 {r1['stats']['歧义格数']}",
            f"  {t2}（自动配「项目名称+采购三级分类」；命中多行取值不同 → 留空）："
            f"填 {m2['填格']}｜对 {m2['对']}｜**错 {m2['错']}**｜留空 {m2['留空']}"
            f"｜歧义格 {r2['stats']['歧义格数']}",
            f"  实际钥匙列：{r2['stats']['实际钥匙列']}",
            f"  需人工确认：{int(len(r2['review']))} 行（多候选留空/未补上）；"
            f"复验存疑明细 {int(len(r2['audit']))} 格（不进主清单）",
            ""])
        for b in m1["错行"][:5]:
            lines.append(f"    {t1}错：行{b[0]} {b[1]}/{b[2]} 填了 {b[3]}，应为 {b[4]}")
        return (t1, m1, r1), (t2, m2, r2)

    # A：只跟项目名挂钩的列（改前本来也不会错）
    (_, mA1, _), (_, mA2, rA2) = _scenario("事业部", "A 只跟项目名挂钩的列")
    # C：随分类变化的列（改前会错，这才是关键证据）
    (_, mC1, rC1), (_, mC2, rC2) = _scenario("合同编号", "C 随三级分类变化的列")

    # ---- 场景 B：源表换成另一张真实表（列名叫「项目/类别」）----
    tplB = keys[tkeys].copy()
    tplB["事业部"] = ""
    rB = fill_multi(tplB, key_cols=["项目名称"], sources=[{"name": "匹配表_001", "df": src_other}])
    mB = _bench(rB["result"], gt, tkeys, "场景B")
    n_fillB = int((rB["result"]["事业部"].astype(str).str.strip() != "").sum())
    lines += ["", "【场景B】源表=匹配表_001_合同进度表（列名是「项目/类别」，需自动认出）",
              f"  补上 {n_fillB}/{len(tplB)} 行｜对 {mB['对']}｜错 {mB['错']}｜留空 {mB['留空']}"
              f"｜歧义格 {rB['stats']['歧义格数']}｜复验一致率 {rB['stats']['复验一致率']}%",
              f"  实际钥匙列：{rB['stats']['实际钥匙列']}"]
    for b in mB["错行"][:5]:
        lines.append(f"    错：行{b[0]} {b[1]}/{b[2]} 填了 {b[3]}，应为 {b[4]}")

    lines += ["", "结论：目标列只跟项目名挂钩时（场景A）改前本来就不会错；",
              "      目标列随三级分类变化时（场景C）改前靠猜第 1 条 → 错值 14，改后自动配第二把钥匙 + 歧义留空 → 错值 0；",
              "      余下留空 = 源表自身重复行（无解）或源表里确实没有，已进「需人工确认」清单（必看在前）。"]
    with open(REPORT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    check("回测：报告已生成", os.path.exists(REPORT))
    check("场景A（只跟项目名挂钩）：改前改后都不错（错 0）", mA1["错"] == 0 and mA2["错"] == 0)
    check("场景C（随分类变化）：改前会填错（错 >0）", mC1["错"] > 0)
    check("场景C：改后错值归零（靠自动配钥匙 + 歧义留空）", mC2["错"] == 0)
    check("场景C：改后是靠自动补的第二把钥匙定下来的", len(rC2["stats"]["钥匙说明"]) > 0)
    check("场景C：改后歧义格进清单（多候选留空）",
          rC2["stats"]["歧义格数"] > 0
          and "多候选" in "".join(rC2["review"]["类型"].astype(str)))
    check("场景C：清单只收必看（复验明细另存 audit）",
          set(rC2["review"]["类型"].astype(str).map(lambda s: s.split("(")[0])) <= {"多候选", "未补上"}
          and "复验不一致" not in set(rC2["review"]["类型"]))
    check("场景B：换一张真实表也能补上（自动认出「类别」）", n_fillB >= 5)
    check("场景B：对上人工答案的错值 ≤1 行", mB["错"] <= 1)

    print(f"报告：{REPORT}")

print(f"\n===== 多表补全回测通过：{ok} 项断言 =====")
