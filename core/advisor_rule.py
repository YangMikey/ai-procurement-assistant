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
def advise(matrix, supplier_cols, low_ratio=0.5):
    rows = []
    for _, r in matrix.iterrows():
        prices = {s: to_number(r.get(s)) for s in supplier_cols}
        valid = {s: v for s, v in prices.items() if v is not None and v > 0}
        name = str(r.get("品名", "") or "")
        spec = str(r.get("规格", "") or "")
        label = (name + (" " + spec if spec else "")).strip()
        if not valid:
            rows.append({"品名": name, "规格": spec, "报价数": 0, "最低价供应商": "",
                         "最低价": "", "建议": "无有效报价，请补充询价", "预警": "缺报价"})
            continue
        best = min(valid, key=valid.get)
        low = valid[best]
        med = statistics.median(valid.values()) if len(valid) >= 2 else low
        tips, warn = [], ""
        if len(valid) >= 3 and med > 0 and low < med * low_ratio:
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
    n_warn = int((out["预警"] == "异常低价").sum()) if not out.empty else 0
    n_miss = int((out["报价数"] < len(supplier_cols)).sum()) if not out.empty else 0
    summary = {"品类数": len(out), "异常低价": n_warn, "有缺报价的品类": n_miss,
               "供应商数": len(supplier_cols)}
    text = []
    if n_warn:
        bad = out[out["预警"] == "异常低价"]["品名"].tolist()
        text.append(f"⚠️ 异常低价 {n_warn} 项：{'、'.join(str(x) for x in bad[:8])}"
                    f"{' 等' if n_warn > 8 else ''}")
    if n_miss:
        text.append(f"📭 有 {n_miss} 个品类存在供应商未报价，建议补齐询价")
    if not text:
        text.append("✅ 未发现异常低价/缺报价，可正常比价结论")
    return {"rows": out, "summary": summary, "text": text}
