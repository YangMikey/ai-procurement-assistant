# -*- coding: utf-8 -*-
"""报价单解析（LLM 兜底）：非标文件（表头乱/列名怪）交给 LLM 出字段映射。

原则（PRD：规则优先、LLM 兜底、同接口插拔）：
- **先规则**：`parser_rule.parse_quote_file` 能正常解析（有品名+价格）就直接用，**不调用 LLM**（零成本）
- **规则不行才上 LLM**：把该 Sheet 的前若干行原样给模型，让它给出
  `header_row` 与字段→列（1-based）映射；再用 `parser_rule.build_canonical` 组装规范表
- 结果与 `parser_rule` **同结构**，多一个 `via` 标记（rule / llm），供界面标注来源
- 只发必要内容（前 N 行），配合 `llm_client` 内容缓存，成本可控

用法：
    q, used = parse_quote_auto(path=p, llm=client)          # 自动（规则优先）
    q, used = parse_quote_auto(path=p, llm=client, force_llm=True)
"""
import os

import pandas as pd

from .parser_rule import (CANONICAL, _list_sheets, _rate_from_header, _read_raw,
                          build_canonical, parse_quote_file, supplier_from_filename)


def rule_quality(q):
    """规则解析结果是否"够好"（有品名 + 有价格 + 有数据行）。"""
    m = q.get("mapping", {}) or {}
    has_name = "品名" in m
    has_price = ("含税单价" in m) or ("不含税单价" in m)
    return bool(has_name and has_price and len(q.get("df", [])) > 0)


def _table_text(raw, max_rows=15, max_cols=30, cell=28):
    lines = []
    ncol = min(len(raw.columns), max_cols)
    for r in range(min(len(raw), max_rows)):
        cells = []
        for c in range(ncol):
            v = raw.iloc[r, c]
            s = "" if v is None else str(v).strip().replace("\n", " ")
            cells.append(s[:cell])
        lines.append(f"行{r + 1}: " + " | ".join(cells))
    return "\n".join(lines), ncol


def _llm_mapping(llm, filename, sheet, raw):
    text, ncol = _table_text(raw)
    fields = "、".join(CANONICAL)
    sys_p = ("你是采购报价单解析助手。下面给出某表格的前若干行原始内容（行号 1 起、列用 1 起编号）。"
             f"请判断表头所在行，并把字段映射到列号。可用字段（用不上就省略）：{fields}。"
             "注意：数量=数量列；含税单价/不含税单价=对应价格列；税率=税率列；规格/型号不合并时各自列。"
             f'只输出 JSON：{{"header_row":整数0~{len(raw)},"mapping":{{字段:列号}},"supplier":"供应商名或空"}}。')
    usr = f"文件：{os.path.basename(filename)}｜Sheet：{sheet}｜共 {ncol} 列\n{text}"
    r = llm.chat([{"role": "system", "content": sys_p},
                  {"role": "user", "content": usr}], max_tokens=4000, temperature=0)
    if r.get("error") or not r.get("text"):
        return None, f"LLM 解析失败：{r.get('error') or '空响应'}"
    from .matcher import _extract_json
    data = _extract_json(r["text"])
    if not isinstance(data, dict):
        return None, "LLM 未返回可解析的 JSON"
    hdr = data.get("header_row")
    mp = data.get("mapping") or {}
    mapping = {}
    for k, v in mp.items():
        if k in CANONICAL:
            try:
                iv = int(v) - 1          # LLM 用 1-based，这里转 0-based
            except Exception:
                continue
            if 0 <= iv < len(raw.columns):
                mapping[k] = iv
    if not mapping:
        return None, "LLM 映射为空"
    try:
        hdr = int(hdr)
    except Exception:
        hdr = 1
    if not (1 <= hdr <= len(raw)):
        hdr = 1
    supplier = str(data.get("supplier") or "").strip()
    return {"header_row": hdr, "mapping": mapping, "supplier": supplier}, None


def parse_quote_auto(path=None, data=None, filename=None, sheet=None, supplier=None,
                     mode="file", llm=None, force_llm=False):
    """规则优先、非标用 LLM。返回 (quotes, used_llm)。quotes 与 parser_rule 同结构。"""
    if filename is None:
        filename = os.path.basename(path) if path else "unknown.xlsx"

    def _rule():
        return parse_quote_file(path=path, data=data, filename=filename, sheet=sheet,
                                supplier=supplier, mode=mode)

    quotes = _rule()
    if not force_llm and all(rule_quality(q) for q in quotes):
        for q in quotes:
            q["via"] = "rule"
        return quotes, False
    if llm is None or not getattr(llm, "enabled", False):
        for q in quotes:
            q.setdefault("via", "rule")
        if not all(rule_quality(q) for q in quotes):
            for q in quotes:
                if not rule_quality(q):
                    q["warnings"] = list(q.get("warnings", [])) + ["规则解析质量低，且 LLM 未启用"]
        return quotes, False

    # 规则不行：对质量低的 Sheet 用 LLM 重解析
    sheets = [sheet] if sheet else _list_sheets(path, data, filename)
    good = {q["sheet"]: q for q in quotes if rule_quality(q) and not force_llm}
    out, used = [], False
    for sh in sheets:
        if sh in good:
            out.append(good[sh])
            continue
        raw = _read_raw(path, data, filename, sh)
        if raw is None or raw.empty:
            out.append({"supplier": supplier or supplier_from_filename(filename),
                        "sheet": sh, "header_row": 1,
                        "df": pd.DataFrame(columns=CANONICAL), "mapping": {},
                        "warnings": ["空表"], "source": filename, "via": "rule"})
            continue
        info, err = _llm_mapping(llm, filename, sh, raw)
        if info is None:
            # LLM 也没成 → 退回规则结果
            base = next((q for q in quotes if q["sheet"] == sh), None)
            if base is None:
                base = {"supplier": supplier or supplier_from_filename(filename),
                        "sheet": sh, "header_row": 1,
                        "df": pd.DataFrame(columns=CANONICAL), "mapping": {},
                        "warnings": [], "source": filename}
            base["warnings"] = list(base.get("warnings", [])) + [err]
            base["via"] = "rule"
            out.append(base)
            continue
        hdr = info["header_row"]
        headers = raw.iloc[hdr - 1].tolist()
        data_rows = raw.iloc[hdr:].reset_index(drop=True)
        header_rate = None
        for h in headers:
            hr = _rate_from_header(h)
            if hr is not None:
                header_rate = hr
                break
        warnings = [f"LLM 解析：表头行={hdr}，字段映射={ {k: v + 1 for k, v in info['mapping'].items()} }"]
        df, warnings = build_canonical(headers, data_rows, info["mapping"], warnings,
                                       header_rate=header_rate)
        if df.empty:
            warnings.append("解析后无有效数据行")
        sup = supplier or info.get("supplier") or (
            sh if mode == "sheet" else supplier_from_filename(filename))
        out.append({"supplier": sup, "sheet": sh, "header_row": hdr, "df": df,
                    "mapping": {k: str(headers[v]) for k, v in info["mapping"].items()},
                    "warnings": warnings, "source": filename, "via": "llm"})
        used = True
    return out, used
