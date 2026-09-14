# -*- coding: utf-8 -*-
"""采购建议（LLM 版，F09）：把比价矩阵摘要交给 LLM，输出结构化建议（成本/风险/谈判点）。

- 规则版（advisor_rule）先跑，结论一并作为上下文（便宜且稳）
- 只发**摘要**（品名/规格 + 各家价格 + 规则预警），不发原始文件
- 复用 llm_client：默认关、内容缓存、成本留痕、调用上限；失败不抛异常
"""
from .converter import to_number
from .registry import skill


def _digest(matrix, supplier_cols, rule_advice=None, max_rows=60):
    lines = ["品名 | 规格 | " + " | ".join(supplier_cols) + " | 最低价供应商"]
    for _, r in matrix.head(max_rows).iterrows():
        vals = []
        for s in supplier_cols:
            v = to_number(r.get(s))
            vals.append("" if v is None else f"{v:g}")
        lines.append(f"{r.get('品名', '')} | {r.get('规格', '')} | " + " | ".join(vals)
                     + f" | {r.get('最低价供应商', '')}")
    txt = "\n".join(lines)
    if rule_advice:
        txt += "\n\n【规则版结论】\n" + "\n".join(rule_advice.get("text", []))
    return txt


@skill(
    name="采购建议(LLM)",
    desc="基于比价矩阵+规则结论，用 LLM 输出结构化采购建议（成本/风险/谈判点）",
    inputs={"matrix": "比价矩阵", "supplier_cols": "供应商列", "llm": "LLM 客户端",
            "rule_advice": "规则版建议（可选，作为上下文）"},
    outputs={"text": "建议文本", "error": "错误信息", "cost_usd": "本次消耗估算"},
    task_modes=["完整比价"],
)
def advise_llm(matrix, supplier_cols, llm, rule_advice=None, max_tokens=4000):
    if llm is None or not getattr(llm, "enabled", False):
        return {"text": "", "error": "LLM 未启用", "cost_usd": 0.0, "cached": False}
    sys_p = ("你是资深采购分析师。基于给定的比价矩阵与规则结论，输出中文结构化建议，"
             "分三块：①成本结论（各项最优供应商）②风险提示（异常低价/缺报价/疑似错配）"
             "③谈判建议（可压价项、备选供应商）。简洁分点，不要客套，不要重复表格数字。")
    usr = _digest(matrix, supplier_cols, rule_advice)
    r = llm.chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": usr}], max_tokens=max_tokens, temperature=0)
    return {"text": r.get("text") or "", "error": r.get("error"),
            "cost_usd": r.get("cost_usd", 0.0), "cached": r.get("cached", False)}
