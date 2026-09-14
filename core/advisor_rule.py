# -*- coding: utf-8 -*-
"""采购建议（规则版，F09）：最低价汇总 + 异常低价预警 + 缺报价提示。

- 最低价供应商：矩阵每行最低价来自哪家
- 异常低价：最低价 < 该行“有报价供应商”的中位数 × low_ratio（默认 0.5）→ 疑似异常（防止低价陷阱/录入错误）
- 缺报价：某行有供应商未报价（该列为空）→ 提示询价补齐
- 纯规则、离线；LLM 版建议（M3）以同接口接入
"""
import statistics

from .converter import to_number
from .registry import skill


@skill(
    name="采购建议(规则)",
    desc="基于比价矩阵给结构化建议：最低价供应商、异常低价预警、缺报价提示",
    inputs={"matrix": "比价矩阵", "supplier_cols": "供应商价格列名列表", "low_ratio": "异常低价阈值(占中位数比例)"},
    outputs={"rows": "逐行建议 DataFrame", "summary": "汇总 dict", "text": "可读建议列表"},
    task_modes=["完整比价"],
)
def advise(matrix, supplier_cols, low_ratio=0.5, low_confidence=None):
    """逐行建议。low_confidence：对齐阶段的低/中置信度项（来自 aligner），用于"疑似错配"预警。"""
    mis = set()
    for it in (low_confidence or []):
        nm = str(it.get("品名", "")).strip()
        if nm:
            mis.add(nm)
    rows = []
    for _, r in matrix.iterrows():
        prices = {s: to_number(r.get(s)) for s in supplier_cols}
        valid = {s: v for s, v in prices.items() if v is not None and v > 0}
        name = str(r.get("品名", "") or "")
        spec = str(r.get("规格", "") or "")
        tips, warn = [], ""
        # 疑似品类错配（对齐置信度低 / 规格不一致）
        if name in mis:
            warn = "疑似错配"
            tips.append("该行存在低置信度对齐，请核对品名/规格是否对齐正确")
        if not valid:
            rows.append({"品名": name, "规格": spec, "报价数": 0, "最低价供应商": "",
                         "最低价": "", "建议": "无有效报价，请补充询价",
                         "预警": "缺报价" if not warn else warn})
            continue
        best = min(valid, key=valid.get)
        low = valid[best]
        med = statistics.median(valid.values()) if len(valid) >= 2 else low
        if not warn and len(valid) >= 3 and med > 0 and low < med * low_ratio:
            warn = "异常低价"
            tips.append(f"最低价明显低于中位数({med:g})，建议核实规格/是否漏项")
        missing = [s for s in supplier_cols if prices.get(s) is None or prices.get(s) == 0]
        if missing:
            tips.append("未报价：" + "、".join(missing))
        if not tips:
            tips.append("正常")
        rows.append({"品名": name, "规格": spec, "报价数": len(valid),
                     "最低价供应商": best, "最低价": low,
                     "建议": "；".join(tips), "预警": warn})
    import pandas as pd
    out = pd.DataFrame(rows)
    if not out.empty:
        n_warn = int((out["预警"] == "异常低价").sum())
        n_mis = int((out["预警"] == "疑似错配").sum())
        n_miss = int((out["报价数"] < len(supplier_cols)).sum())
    else:
        n_warn = n_mis = n_miss = 0
    summary = {"品类数": len(out), "异常低价": n_warn, "疑似错配": n_mis,
               "有缺报价的品类": n_miss, "供应商数": len(supplier_cols)}
    text = []
    if n_warn:
        bad = out[out["预警"] == "异常低价"]["品名"].tolist()
        text.append(f"⚠️ 异常低价 {n_warn} 项：{'、'.join(str(x) for x in bad[:8])}"
                    f"{' 等' if n_warn > 8 else ''}")
    if n_mis:
        bad = out[out["预警"] == "疑似错配"]["品名"].tolist()
        text.append(f"🔍 疑似品类错配 {n_mis} 项（对齐置信度低）：{'、'.join(str(x) for x in bad[:8])}")
    if n_miss:
        text.append(f"📭 有 {n_miss} 个品类存在供应商未报价，建议补齐询价")
    if not text:
        text.append("✅ 未发现异常低价/缺报价/错配，可正常比价结论")
    return {"rows": out, "summary": summary, "text": text}
