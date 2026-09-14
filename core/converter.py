# -*- coding: utf-8 -*-
"""税率换算：含税↔不含税同口径换算（独立技能，可脱离比价单独使用）。

规则（PRD F04，v3 用户反馈修订——每列独立、无配对）：
- 只新增换算列，不覆盖原值；新列默认插在每个原价格列右侧（不堆表尾）
- 每列税率独立：rates[i] 对应 price_cols[i]；未指定的列用全局 tax_rate
- 列名自动识别税率：'同国贸易 税率13%'→0.13；'玉兰邮（1%）'→0.01（表头自带税率时免填）
- 税率值兼容 "13%"、"13"、"0.13" 三种写法
"""
import re

from .registry import skill

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def to_number(v):
    """把单元格值转为 float；无法解析（空/—/文本无数字）返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = (str(v).replace(",", "").replace("，", "").replace("¥", "")
         .replace("￥", "").replace("$", "").strip())
    if s in ("", "-", "—", "/", "无", "免"):
        return None
    m = _NUM_RE.search(s)
    if not m:
        return None
    n = float(m.group())
    if ("%" in s or "％" in s) and n > 1:
        n = n / 100
    return n


def parse_rate(v, default=0.13):
    """税率解析（v3 规则：框内数字 = 百分比数值）。

    '13' → 0.13；'1' → 0.01；'13%' → 0.13；'0.13' → 0.0013（即 0.13%，有歧义写法以百分比数值为准）。
    无法解析 → default。
    """
    m = _NUM_RE.search(str(v))
    if not m:
        return default
    return float(m.group()) / 100


def parse_rate_from_name(name, default=None):
    """从列名解析税率（表头自带税率的场景）。

    '同国贸易 税率13%' → 0.13；'玉兰邮（1%）' → 0.01；'润锋 （13%）' → 0.13
    '利源通 税率（1%)' → 0.01（兼容不带 % 号的括号写法）；识别不到返回 default。
    """
    s = str(name)
    m = re.search(r"税率\s*(\d+(?:\.\d+)?)\s*%", s)
    if m:
        return float(m.group(1)) / 100
    m = re.search(r"[（(]\s*(\d+(?:\.\d+)?)\s*%?\s*[)）]", s)
    if m:
        return float(m.group(1)) / 100
    return default


def _convert_cell(v, rate, op):
    n = to_number(v)
    if n is None:
        return ""
    return round(n / (1 + rate), 6) if op == "div" else round(n * (1 + rate), 6)


@skill(
    name="税率换算",
    desc="含税↔不含税同口径换算；每列独立、税率各自指定（支持从列名自动识别）；新增列插在原列右侧",
    inputs={"df": "DataFrame", "price_cols": "价格列名列表", "tax_rate": "全局税率(兜底默认0.13)",
            "direction": "to_ex=含税转不含税 / to_in=不含税转含税",
            "rates": "每列税率列表（与价格列一一对应，None 项用 tax_rate）"},
    outputs={"df": "插入换算列后的 DataFrame", "added_cols": "新增列名列表"},
    task_modes=["仅换算", "完整比价"],
)
def convert_tax(df, price_cols, tax_rate=0.13, direction="to_ex", rates=None,
                insert_right=True, skip_mask=None):
    out = df.copy()
    added = []
    op = "div" if direction == "to_ex" else "mul"
    suffix = "(不含税)" if direction == "to_ex" else "(含税)"
    rates = list(rates) if rates else []
    skip = list(skip_mask) if skip_mask else [False] * len(out)

    for i, col in enumerate(price_cols):
        if col not in out.columns:
            continue
        rate = rates[i] if i < len(rates) and rates[i] is not None else tax_rate
        new_name = f"{col}{suffix}"
        if new_name in out.columns:
            new_name = f"{col}{suffix}(换算)"
        vals = ["" if skip[k] else _convert_cell(v, rate, op)
                for k, v in enumerate(out[col].tolist())]
        if insert_right:
            out.insert(out.columns.get_loc(col) + 1, new_name, vals)
        else:
            out[new_name] = vals
        added.append(new_name)
    return out, added
