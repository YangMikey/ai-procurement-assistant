# -*- coding: utf-8 -*-
"""报价单解析（规则引擎）：把格式各异的供应商报价单解析成规范列。

设计（PRD F02 / 评估纪律）：
- 表头行自动探测（字段关键词打分），真实件表头常在第 2 行、合成件也有变体
- 字段关键词字典 → 规范列：序号/品名/规格/单位/数量/含税单价/不含税单价/税率/品牌
- 供应商名默认取「文件名」（真实数据：一个文件=一个供应商，sheet=分类）；可手动覆盖
- 支持 xlsx / xls / csv；跳过「合计/小计/全空」行；不臆造数据、保留警告
- 纯规则、离线、零成本；LLM 兜底（parser_llm）后续以同接口接入

输出每条报价 = dict：
  {supplier, sheet, header_row, df(规范列), mapping{规范字段: 原始表头}, warnings[], source}
"""
import io
import os
import re

import pandas as pd

from .converter import to_number, parse_rate, parse_rate_from_name
from .registry import skill

CANONICAL = ["序号", "品名", "规格", "单位", "数量",
             "含税单价", "不含税单价", "税率", "品牌"]

_SUM_WORDS = ("合计", "小计", "总计", "总价", "合 计")


def _norm_header(v):
    s = str(v) if v is not None else ""
    s = s.replace("（", "(").replace("）", ")")
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


def _classify(header):
    """表头单元格 → (规范字段, 是否品名+规格合并列)；识别不到返回 None。

    顺序即优先级：不含税 > 含税 > 税率 > 通用价 > 规格/型号 > 品名 > 单位 > 数量 > 序号 > 品牌。
    """
    s = _norm_header(header)
    if not s:
        return None
    if any(k in s for k in ("品名及规格", "名称及规格", "品名规格", "名称规格",
                            "货物名称及规格", "材料名称及规格", "品名及型号", "名称及型号")):
        return ("品名", True)
    if "不含税" in s or "除税" in s or "未税" in s:
        return ("不含税单价", False)
    if "含税" in s:
        return ("含税单价", False)
    # 价格优先于税率：真实件常见「单价（元）\n税率1%」同格，须判为价格列
    if "单价" in s or "价格" in s or "报价" in s or s == "价":
        return ("含税单价", False)
    if "税率" in s or "税点" in s or s in ("tax",):
        return ("税率", False)
    if "规格" in s or "型号" in s:
        return ("规格", False)
    if "品名" in s or "名称" in s or "物料" in s or "货物" in s or "品类" in s or "品项" in s or "产品" in s:
        return ("品名", False)
    if "单位" in s:
        return ("单位", False)
    if "数量" in s:
        return ("数量", False)
    if "序号" in s or "编号" in s or re.fullmatch(r"no\.?", s):
        return ("序号", False)
    if "品牌" in s or "厂家" in s or "厂商" in s:
        return ("品牌", False)
    return None


def _rate_from_header(header):
    """从表头文本提取内嵌税率（如「单价（元）\n税率1%」→ 0.01）；无则 None。"""
    return parse_rate_from_name(header, default=None)


def guess_header_row(raw, max_scan=25):
    """在原始行里找最像表头的一行；返回 (1-based 行号, 命中字段数)。"""
    best_i, best_score = 1, -1
    for i in range(min(len(raw), max_scan)):
        fields = set()
        for v in raw.iloc[i].tolist():
            c = _classify(v)
            if c:
                fields.add(c[0])
        score = len(fields)
        if score > best_score:
            best_score, best_i = score, i + 1
    return best_i, best_score


def _engine_for(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".xls":
        return "xlrd"
    if ext in (".xlsx", ".xlsm"):
        return "openpyxl"
    return None


def _read_raw(path, data, filename, sheet, n=None):
    """读原始表（不指定表头）。xls 用 xlrd，xlsx 用 openpyxl，csv 自动编码。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        src = io.BytesIO(data) if data is not None else path
        last = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return pd.read_csv(src, encoding=enc, header=None, nrows=n,
                                   dtype=object, keep_default_na=False)
            except (UnicodeDecodeError, UnicodeError) as e:
                last = e
                if data is not None:
                    src.seek(0)
                continue
        raise ValueError(f"CSV 编码无法识别：{last}")
    src = io.BytesIO(data) if data is not None else path
    return pd.read_excel(src, sheet_name=sheet, header=None, nrows=n,
                         dtype=object, engine=_engine_for(filename))


def _split_name_spec(text):
    """合并列拆分：优先按括号"(规格)"拆；否则按空格取尾段（含数字/×/Φ 等型号特征）。"""
    t = str(text).strip()
    m = re.match(r"^(.*?)[（(]([^）)]*)[)）]\s*$", t)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*?)\s+(\S+)$", t)
    if m and re.search(r"[\d×xX*Φφ\-/]", m.group(2)):
        return m.group(1).strip(), m.group(2).strip()
    return t, ""


def supplier_from_filename(filename):
    """从文件名猜供应商名：优先括号内非纯数字内容，否则去掉常见词。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    cands = [x.strip() for x in re.findall(r"[（(]([^）)]+)[)）]", stem)
             if x.strip() and not x.strip().isdigit()]
    if cands:
        return cands[0]
    s = stem
    for w in ("物资采购清单", "采购清单", "报价单", "报价", "清单", "物资", "采购", "表"):
        s = s.replace(w, "")
    s = s.strip(" _-()（）")
    return s or stem


def _clean_data(df):
    """去掉合计/小计/全空行，返回 (df, dropped_reasons)。"""
    keep, dropped = [], []
    for _, r in df.iterrows():
        name = str(r.get("品名", "") or "")
        txt = " ".join(str(v) for v in r.values if v is not None)
        if any(w in name for w in _SUM_WORDS) or any(w in txt for w in ("合计", "小计", "总计")):
            dropped.append(f"跳过合计/小计行：{name[:20]}")
            continue
        if all(str(v).strip() in ("", "nan", "None") for v in r.values):
            dropped.append("跳过全空行")
            continue
        keep.append(r)
    out = pd.DataFrame(keep).reset_index(drop=True) if keep else df.iloc[0:0].reset_index(drop=True)
    return out, dropped


@skill(
    name="报价单解析",
    desc="规则解析供应商报价单：表头行自动探测 + 字段关键词映射 → 规范列；xlsx/xls/csv，一个文件=一个供应商",
    inputs={"path/data": "文件路径或字节流", "filename": "文件名（判扩展名）",
            "sheet": "Sheet 名（可选，默认全部）", "supplier": "供应商名（可选，默认取文件名）",
            "mode": "file=一个文件一个供应商（默认）/ sheet=每 sheet 一个供应商"},
    outputs={"quotes": "解析结果列表 [{supplier, sheet, header_row, df, mapping, warnings}]"},
    task_modes=["完整比价"],
)
def parse_quote_file(path=None, data=None, filename=None, sheet=None,
                     supplier=None, mode="file"):
    """解析报价单文件 → [ParsedQuote]。sheet=None 时解析全部 Sheet。"""
    if filename is None:
        filename = os.path.basename(path) if path else "unknown.xlsx"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".xls", ".csv"):
        raise ValueError(f"不支持的文件类型：{ext}（支持 xlsx/xlsm/xls/csv）")

    sheets = [sheet] if sheet else _list_sheets(path, data, filename)
    quotes = []
    for sh in sheets:
        raw = _read_raw(path, data, filename, sh)
        if raw is None or raw.empty:
            quotes.append({"supplier": supplier or supplier_from_filename(filename),
                           "sheet": sh, "header_row": 1, "df": pd.DataFrame(columns=CANONICAL),
                           "mapping": {}, "warnings": ["空表"], "source": filename})
            continue
        hdr, score = guess_header_row(raw)
        warnings = []
        if score < 2:
            warnings.append(f"表头行识别把握低（命中字段={score}），请人工确认表头行")
        mapping, combined_col = {}, None
        headers = raw.iloc[hdr - 1].tolist()
        header_rate = None
        for j, h in enumerate(headers):
            c = _classify(h)
            if c:
                canon, is_comb = c
                if canon not in mapping:
                    mapping[canon] = j
                if is_comb:
                    combined_col = j
            if header_rate is None:
                header_rate = _rate_from_header(h)
        if "含税单价" in mapping and "不含税单价" not in mapping:
            warnings.append("价格列未标含税/不含税，已按「含税单价」处理")
        data_rows = raw.iloc[hdr:].reset_index(drop=True)
        out, warnings = build_canonical(headers, data_rows, mapping, warnings,
                                        header_rate=header_rate, combined_col=combined_col)
        if out.empty:
            warnings.append("解析后无有效数据行")
        sup = supplier
        if not sup:
            sup = sh if mode == "sheet" else supplier_from_filename(filename)
        quotes.append({"supplier": sup, "sheet": sh, "header_row": hdr,
                       "df": out,
                       "mapping": {k: (str(headers[v]) if isinstance(v, int) else str(v))
                                   for k, v in mapping.items()},
                       "warnings": warnings, "source": filename})
    return quotes


def build_canonical(headers, data_rows, mapping, warnings=None,
                    header_rate=None, combined_col=None):
    """按 {规范字段: 列下标} 把原始数据行组装成规范 df（parser_rule / parser_llm 共用）。

    返回 (df, warnings)；会就地清洗数量/价格/税率、拆分合并列、剔除合计/空行。
    """
    warnings = list(warnings or [])
    out = pd.DataFrame(index=range(len(data_rows)))
    for canon, j in mapping.items():
        if isinstance(j, int) and 0 <= j < len(data_rows.columns):
            out[canon] = data_rows[j].values
    if combined_col is not None and "规格" not in mapping and isinstance(combined_col, int):
        names, specs = [], []
        for v in data_rows[combined_col].tolist():
            nm, sp = _split_name_spec(v)
            names.append(nm)
            specs.append(sp)
        if mapping.get("品名") == combined_col or "品名" not in out.columns:
            out["品名"] = names
        out["规格"] = specs
        mapping["规格"] = combined_col
    for c in ("数量", "含税单价", "不含税单价"):
        if c in out.columns:
            out[c] = [to_number(v) for v in out[c].tolist()]
    if "税率" in out.columns:
        out["税率"] = [(None if str(v).strip() in ("", "nan", "None")
                        else parse_rate(v, default=None)) for v in out["税率"].tolist()]
    elif header_rate is not None:
        out["税率"] = header_rate
        mapping["税率"] = "(表头税率)"
        warnings.append(f"税率取自表头：{header_rate:g}")
    for c in ("序号", "品名", "规格", "单位", "品牌"):
        if c in out.columns:
            out[c] = ["" if v is None else str(v).strip() for v in out[c].tolist()]
    out, dropped = _clean_data(out)
    warnings.extend(dropped)
    return out, warnings


def _list_sheets(path, data, filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        return ["Sheet1"]
    if ext == ".xls":
        import xlrd
        wb = xlrd.open_workbook(path) if data is None else xlrd.open_workbook(file_contents=data)
        return wb.sheet_names()
    from openpyxl import load_workbook
    wb = load_workbook(path if data is None else io.BytesIO(data), read_only=True)
    names = wb.sheetnames
    wb.close()
    return names


def manual_quote(supplier, rows, price_field="含税单价"):
    """【F16 手动报价录入】把界面键入的报价转成与解析器同结构的一条报价。

    rows: [{"品名","规格","单位","数量","价格","税率"}, ...]
    price_field: 键入的"价格"属于哪个口径（含税单价/不含税单价）；另一口径由税率推算。
    """
    heads = ["品名", "规格", "单位", "数量", "价格", "税率"]
    data = pd.DataFrame([[r.get(h, "") for h in heads] for r in rows])   # 位置列 0..n
    idx = {h: i for i, h in enumerate(heads)}
    mapping = {"品名": idx["品名"], "规格": idx["规格"], "单位": idx["单位"],
               "数量": idx["数量"], price_field: idx["价格"], "税率": idx["税率"]}
    warnings = [f"手动录入（价格口径：{price_field}）"]
    df, warnings = build_canonical(heads, data, mapping, warnings)
    return {"supplier": str(supplier).strip() or "手动录入", "sheet": "(手动录入)",
            "header_row": 1, "df": df,
            "mapping": {k: heads[v] for k, v in mapping.items()},
            "warnings": warnings, "source": "manual", "via": "manual"}
