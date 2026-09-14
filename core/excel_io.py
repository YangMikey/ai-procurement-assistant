# -*- coding: utf-8 -*-
"""文件读写：加载（xlsx/xls/csv，支持指定表头行）、原文件内备份 Sheet、结果写回原文件。

网页版扩展：read_table_from_bytes / list_sheets_from_bytes / export_df_bytes
（Streamlit 上传件是内存字节流，无法走文件路径 API）。
"""
import io
import os
import sys
from datetime import datetime

import pandas as pd


def app_dir():
    """程序所在目录（exe 打包后取 exe 所在目录，用于日志/配置）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def read_table(path, sheet_name=None, header_row=1):
    """读取指定 Sheet，第 header_row 行（1-based）为表头，数据从其下一行开始。

    csv 自动尝试 utf-8-sig / gb18030 / utf-16 编码。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        last_err = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return pd.read_csv(path, encoding=enc, dtype=object,
                                   keep_default_na=False, header=header_row - 1)
            except (UnicodeDecodeError, UnicodeError) as e:
                last_err = e
                continue
        raise ValueError(f"无法识别该 CSV 文件的编码，请用 Excel 另存为 xlsx 后重试：{last_err}")
    if sheet_name is None:
        raise ValueError("未指定 Sheet 名称")
    return pd.read_excel(path, sheet_name=sheet_name, dtype=object, header=header_row - 1)


def get_sheet_names(path):
    """列出文件的全部 Sheet 名称；csv 返回固定的 Sheet1。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return ["Sheet1"]
    if ext == ".xls":
        import xlrd
        return xlrd.open_workbook(path, on_demand=True).sheet_names()
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    names = wb.sheetnames
    wb.close()
    return names


def get_headers(path, sheet_name=None, header_row=1):
    """读取指定表头行的列名（与 read_table 完全一致）。"""
    df = read_table(path, sheet_name, header_row=header_row)
    return list(df.columns)


def preview_rows(path, sheet_name=None, n=10):
    """读取前 n 行原始内容（不指定表头），返回 [(行号, [单元格...]), ...]，用于表头行预览。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        last_err = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                raw = pd.read_csv(path, encoding=enc, nrows=n, dtype=object,
                                  keep_default_na=False, header=None)
                break
            except (UnicodeDecodeError, UnicodeError) as e:
                last_err = e
        else:
            raise ValueError(f"无法识别该 CSV 文件的编码：{last_err}")
    else:
        if sheet_name is None:
            raise ValueError("未指定 Sheet 名称")
        raw = pd.read_excel(path, sheet_name=sheet_name, nrows=n, dtype=object, header=None)
    return [(i + 1, row) for i, row in enumerate(raw.values.tolist())]


def backup_sheet_in_file(path, sheet_name):
    """在原工作簿内把指定 Sheet 复制为「{sheet名}_备份_时间戳」。

    已存在该 Sheet 的备份时直接复用、不再新建。
    仅支持 xlsx/xlsm；xls/csv 返回 None（跳过备份）。
    文件被 Excel/WPS 占用时抛 PermissionError。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".xlsx", ".xlsm"):
        return None
    from openpyxl import load_workbook
    wb = load_workbook(path)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"工作簿中找不到 Sheet「{sheet_name}」")
    prefix = f"{sheet_name}_备份_"
    existing = [s for s in wb.sheetnames if s.startswith(prefix)]
    if existing:
        wb.close()
        return existing[0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{sheet_name}_备份_{stamp}"
    title = base
    n = 1
    while title in wb.sheetnames:
        n += 1
        title = f"{base}_{n}"
    src = wb[sheet_name]
    copied = wb.copy_worksheet(src)
    copied.title = title
    try:
        wb.save(path)
    except PermissionError:
        wb.close()
        raise PermissionError(f"文件被占用（可能正在 WPS/Excel 中打开）：{path}")
    wb.close()
    return title


RESULT_SHEET_BASE = "补缺结果"
RATE_COL = "匹配率 (%)"
ANNOT_COLS = ["匹配轮次", RATE_COL, "未匹配原因"]


def write_result_in_sheet(path, sheet_name, header_row,
                          row_map, row_status, fetch_records, fetch_names):
    """把匹配结果原地写进匹配表原 Sheet（不新建 Sheet、不新建文件）。

    - 查补列：目标名已存在于该 Sheet（含别名指向已有列）→ 原地更新该列；否则在表尾追加
    - 匹配轮次 / 匹配率 (%) / 未匹配原因：已存在则复用，否则追加
    - 重复预警行取匹配率最高的一条组合写入，未匹配行不动查补列原值
    - row_map：有效匹配表行 k → 原始数据行下标（0-based，不含表头行）
    - row_status：{k: {'status', 'combos': [(钥匙行下标, 匹配率)...], 'reason'}}
    - fetch_records：每条钥匙行的查补字段原始值
    返回 (写入列名列表, 覆盖了已有数据的列名列表)。
    """
    from openpyxl import load_workbook
    wb = load_workbook(path)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"工作簿中找不到 Sheet「{sheet_name}」")
    ws = wb[sheet_name]
    # 列名解析与读表一致：两行合并表头的标签在第 1 行（header_row 行是空的），
    # 只看 header_row 会找不到「事业部」这类竖向合并列 → 误判为新列追加到表尾。
    try:
        with open(path, "rb") as fh:
            _fb = fh.read()
        _names, _is_two = split_two_row_header(_fb, path, sheet_name, header_row)
    except Exception:
        _names, _is_two = None, False
    headers = []
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=header_row, column=c).value
        if _names and _is_two and c <= len(_names) and str(_names[c - 1]):
            headers.append(str(_names[c - 1]))
        else:
            headers.append(str(v) if v is not None else "")

    targets = []   # (列下标1-based, 名称, 是否新追加)
    overwritten = []
    for name in list(fetch_names) + ANNOT_COLS:
        if name in headers:
            targets.append((headers.index(name) + 1, name, False))
        else:
            new_idx = len(headers) + 1
            headers.append(name)
            ws.cell(row=header_row, column=new_idx, value=name)
            targets.append((new_idx, name, True))
    n_fetch = len(fetch_names)

    for k, orig_idx in enumerate(row_map):
        ws_row = header_row + 1 + orig_idx
        st = row_status[k]
        if st["combos"]:
            best = max(st["combos"], key=lambda t: t[1])
            for (col, _, _), v in zip(targets[:n_fetch], fetch_records[best[0]]):
                cell = ws.cell(row=ws_row, column=col,
                               value="" if pd.isna(v) else v)
                if cell.data_type == "f":   # 防止 = 开头内容被当公式
                    cell.data_type = "s"
        ws.cell(row=ws_row, column=targets[n_fetch][0], value=st["status"])
        ws.cell(row=ws_row, column=targets[n_fetch + 1][0],
                value=round(max(r for _, r in st["combos"]), 1) if st["combos"] else 0.0)
        ws.cell(row=ws_row, column=targets[n_fetch + 2][0], value=st["reason"])

    # 记录哪些已有列会被覆盖了非空数据（查补列，不含标注列），供界面提醒
    for col, name, appended in targets[:n_fetch]:
        if not appended:
            for r in range(header_row + 1, ws.max_row + 1):
                v = ws.cell(row=r, column=col).value
                if v is not None and str(v).strip() != "":
                    overwritten.append(name)
                    break

    try:
        wb.save(path)
    except PermissionError:
        wb.close()
        raise PermissionError(f"文件被占用（可能正在 WPS/Excel 中打开）：{path}")
    wb.close()
    return [name for _, name, _ in targets], overwritten


def export_fallback_result(df, folder, name=None):
    """降级：把结果另存为独立文件「匹配结果_时间.xlsx」。"""
    name = name or f"匹配结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    out = os.path.join(folder, name)
    _export_single_sheet(df, out, sheet_name=RESULT_SHEET_BASE)
    return out


def _export_single_sheet(df, out_path, sheet_name=RESULT_SHEET_BASE):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(list(df.columns))
    for row in df.itertuples(index=False, name=None):
        ws.append(["" if pd.isna(v) else v for v in row])
    for cells in ws.iter_rows(min_row=2):
        for cell in cells:
            if cell.data_type == "f":
                cell.data_type = "s"
    if RATE_COL in df.columns:
        letter = get_column_letter(list(df.columns).index(RATE_COL) + 1)
        for r in range(2, ws.max_row + 1):
            ws[f"{letter}{r}"].number_format = '0.0"%"'
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for col_idx, col_name in enumerate(df.columns, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = _estimate_width(df[col_name])
    ws.freeze_panes = "A2"
    wb.save(out_path)


def _estimate_width(series, cap=45):
    """按内容粗估列宽（中文按 2 个字符宽计）。"""
    try:
        sample = series.astype(str).head(200)
        width = max([sum(2 if ord(ch) > 127 else 1 for ch in s) for s in sample] + [10])
        header_w = sum(2 if ord(ch) > 127 else 1 for ch in str(series.name))
        return min(max(width + 2, header_w + 4, 10), cap)
    except Exception:
        return 14


# =================== 网页版扩展：内存字节流 ===================

def read_table_from_bytes(data, filename, sheet_name=None, header_row=1):
    """从内存字节流读表（Streamlit 上传件）。逻辑与 read_table 一致。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        last_err = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return pd.read_csv(io.BytesIO(data), encoding=enc, dtype=object,
                                   keep_default_na=False, header=header_row - 1)
            except (UnicodeDecodeError, UnicodeError) as e:
                last_err = e
                continue
        raise ValueError(f"无法识别该 CSV 文件的编码，请用 Excel 另存为 xlsx 后重试：{last_err}")
    if sheet_name is None:
        raise ValueError("未指定 Sheet 名称")
    return pd.read_excel(io.BytesIO(data), sheet_name=sheet_name,
                         dtype=object, header=header_row - 1)


def list_sheets_from_bytes(data, filename):
    """从内存字节流列出 Sheet 名；csv 返回固定 Sheet1。"""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".csv":
        return ["Sheet1"]
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True)
    names = wb.sheetnames
    wb.close()
    return names


def export_df_bytes(df, sheet_name="结果"):
    """DataFrame → xlsx 字节流（供网页下载），带表头样式与冻结首行。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append([str(c) for c in df.columns])
    for row in df.itertuples(index=False, name=None):
        ws.append(["" if pd.isna(v) else v for v in row])
    for cells in ws.iter_rows(min_row=2):
        for cell in cells:
            if cell.data_type == "f":
                cell.data_type = "s"
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for col_idx in range(1, len(df.columns) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 18
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# =================== 路径模式写回（ai副本保险机制） ===================

_XLSX_EXTS = (".xlsx", ".xlsm")


def _wb_open(path):
    from openpyxl import load_workbook
    return load_workbook(path)


def backup_sheet_numbered(path, sheet_name, prefix="ai副本"):
    """复制指定 Sheet 为备份：ai副本{sheet名}；已存在则 ai副本{sheet名}(1)、(2) 递增。

    每次运行前快照、不覆盖旧备份（可回溯）。仅支持 xlsx/xlsm。
    返回备份 Sheet 名；文件被占用时抛 PermissionError。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in _XLSX_EXTS:
        raise ValueError(f"原位写回仅支持 xlsx/xlsm（当前 {ext}），请改用下载结果")
    wb = _wb_open(path)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"工作簿中找不到 Sheet「{sheet_name}」")
    base = f"{prefix}{sheet_name}"
    title, n = base, 0
    while title in wb.sheetnames:
        n += 1
        title = f"{base}({n})"
    copied = wb.copy_worksheet(wb[sheet_name])
    copied.title = title
    try:
        wb.save(path)
    except PermissionError:
        wb.close()
        raise PermissionError(f"文件被占用（可能正在 WPS/Excel 中打开）：{path}")
    wb.close()
    return title


def _shift_merges_right(ws, before_col_idx):
    """调整合并单元格以配合 ws.insert_cols(before_col_idx + 1)（须在插入前调用）。

    openpyxl 3.1 的 insert_cols 不移动合并单元格：
    - 完全在插入点右侧的合并 → 整体右移 1 列
    - 跨插入点的块（如比价表供应商名横向合并 I1:J2）→ 拆成左右两半，
      左半保留在原位、右半随内容右移 1 列，让新列落在空位上
    """
    insert_col = before_col_idx + 1
    for r in list(ws.merged_cells.ranges):
        if r.min_col > before_col_idx:
            try:
                ws.unmerge_cells(str(r))
                ws.merge_cells(start_row=r.min_row, start_column=r.min_col + 1,
                               end_row=r.max_row, end_column=r.max_col + 1)
            except Exception:
                pass
        elif r.max_col >= insert_col:
            try:
                ws.unmerge_cells(str(r))
            except Exception:
                pass
            # 左半：min_col..before_col_idx（跨多行/多列才值得保留合并）
            if before_col_idx > r.min_col or r.max_row > r.min_row:
                try:
                    ws.merge_cells(start_row=r.min_row, start_column=r.min_col,
                                   end_row=r.max_row, end_column=before_col_idx)
                except Exception:
                    pass
            # 右半：旧 insert_col..max_col 的内容随插入右移 → insert_col+1..max_col+1
            if r.max_col > before_col_idx:
                try:
                    ws.merge_cells(start_row=r.min_row, start_column=insert_col + 1,
                                   end_row=r.max_row, end_column=r.max_col + 1)
                except Exception:
                    pass


def writeback_convert(path, sheet_name, header_row, items):
    """把换算结果写回原 Sheet（v2：按列位置定位，免疫合并表头/Unnamed）。

    items: [(源列1-based位置, 新列名, [逐行值...])]；自动从右往左处理。
    - 幂等：源列右侧紧邻列的表头（归一化空白后）与新列名相同 → 只覆写数值，不插列
      （重跑=自动纠错，不重复插列）
    - 新列表头复刻源列表头结构：prev 行（header_row-1）有值且源列为竖向合并 →
      新列 prev 行写新列名并竖向合并两行；否则 header_row 单行写新列名
    - 值为空串/None 的行跳过不写（保护合计/公式行原值）
    返回写入的新列名列表；文件被占用抛 PermissionError。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in _XLSX_EXTS:
        raise ValueError(f"原位写回仅支持 xlsx/xlsm（当前 {ext}），请改用下载结果")
    wb = _wb_open(path)
    if sheet_name not in wb.sheetnames:
        wb.close()
        raise ValueError(f"工作簿中找不到 Sheet「{sheet_name}」")
    ws = wb[sheet_name]
    prev_r = header_row - 1

    located = sorted(items, key=lambda t: -t[0])
    written = []
    for (col_idx, new_name, values) in located:
        want = _norm_ws_text(new_name)
        # 幂等重跑：先按名全表找已有新列（首跑后各列位置已漂移，按位置找会重复插列）
        target_col, overwrite = None, False
        for c in range(1, ws.max_column + 1):
            h = _norm_ws_text(ws.cell(row=prev_r, column=c).value) if prev_r >= 1 else ""
            if not h:
                h = _norm_ws_text(ws.cell(row=header_row, column=c).value)
            if h == want:
                target_col, overwrite = c, True
                break
        if target_col is None:
            target_col = col_idx + 1
        if not overwrite:
            _shift_merges_right(ws, col_idx)
            ws.insert_cols(col_idx + 1)
            # 复刻源列表头样式
            prev_val = _norm_ws_text(ws.cell(row=prev_r, column=col_idx).value) if prev_r >= 1 else ""
            vertical = False
            if prev_val:
                for rng in ws.merged_cells.ranges:
                    if (rng.min_col <= col_idx <= rng.max_col
                            and rng.min_row <= prev_r and rng.max_row >= header_row):
                        vertical = True
                        break
            if prev_val and vertical:
                ws.cell(row=prev_r, column=target_col, value=new_name)
                ws.cell(row=header_row, column=target_col, value=None)
                try:
                    ws.merge_cells(start_row=prev_r, start_column=target_col,
                                   end_row=header_row, end_column=target_col)
                except Exception:
                    pass
            else:
                ws.cell(row=header_row, column=target_col, value=new_name)
        for i, v in enumerate(values):
            if v == "" or v is None:
                continue
            cell = ws.cell(row=header_row + 1 + i, column=target_col)
            if isinstance(cell.value, str) and cell.value.startswith("="):
                continue  # 保护公式单元格（如不含税合计行），不覆写
            cell.value = v
        written.append(("(覆写)" if overwrite else "") + new_name)
    try:
        wb.save(path)
    except PermissionError:
        wb.close()
        raise PermissionError(f"文件被占用（可能正在 WPS/Excel 中打开）：{path}")
    wb.close()
    return written


# =================== 两行式（合并）表头拆分 ===================

def _norm_ws_text(s):
    import re as _re
    return _re.sub(r"\s+", " ", str(s or "")).strip()


def split_two_row_header(file_bytes, filename, sheet, header_row):
    """检测两行式（合并）表头并生成拆分列名。

    结构：上一行=供应商名（竖向合并 Row1:Row2，或横向合并跨多列），本行=左侧固定列表头。
    命名规则：上一行有值 → 「上一行值 + 本行值」（本行空则只用上一行值）；
             上一行空 → 用本行值；两行都空 → 回填左邻最后一个非空名（消除 Unnamed）。
    返回 (names, is_two_row)：names 与 pandas 列顺序一一对应（已去重）；
    上一行无实质内容 → (None, False)；xls 不支持 → (None, False)。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm"):
        return None, False
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(file_bytes))
    if sheet not in wb.sheetnames:
        wb.close()
        return None, False
    ws = wb[sheet]
    prev_r = header_row - 1
    if prev_r < 1:
        wb.close()
        return None, False

    def cellv(r, c):
        v = ws.cell(row=r, column=c).value
        return _norm_ws_text(v)

    if not any(cellv(prev_r, c) for c in range(1, ws.max_column + 1)):
        wb.close()
        return None, False

    # 横向合并块前缀回填：如「合同信息」横跨 D1:F1，E1/F1 空格补上左上角值，
    # 使子列名带完整前缀（合同信息 2026年11月），而不是光秃秃的「2026年11月」
    prev_fill = {}
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= prev_r <= rng.max_row and rng.max_col > rng.min_col:
            v = _norm_ws_text(ws.cell(row=rng.min_row, column=rng.min_col).value)
            if v:
                for c in range(rng.min_col, rng.max_col + 1):
                    prev_fill[c] = v

    names, last = [], ""
    for c in range(1, ws.max_column + 1):
        p = cellv(prev_r, c) or prev_fill.get(c, "")
        cu = cellv(header_row, c)
        if p:
            nm = f"{p} {cu}" if cu else p
        elif cu:
            nm = cu
        else:
            nm = last or f"列{c}"
        names.append(nm)
        if p or cu:
            last = nm
    wb.close()

    seen = {}
    for i, nm in enumerate(names):
        if nm in seen:
            seen[nm] += 1
            names[i] = f"{nm}({seen[nm]})"
        else:
            seen[nm] = 0
    return names, True


def detect_header_block_bottom(file_bytes, filename, sheet, top_row):
    """判断 top_row 是否是两行式（合并）表头的「顶行」；是则返回底行 top_row+1，否则 None。

    判定依据（满足其一，且 top_row+1 行有实质内容）：
    - 存在跨 top_row..top_row+1 的竖向合并（如 事业部 A1:A2）
    - top_row 行内存在跨 ≥2 列的横向合并（如 合同信息 D1:F1，下面是各月份子列）
    用途：用户/自动检测把表头行点在了顶行时，自动降级到底行再拆分。
    仅支持 xlsx/xlsm；csv/xls 返回 None。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm"):
        return None
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(file_bytes))
    except Exception:
        return None
    if sheet not in wb.sheetnames:
        wb.close()
        return None
    ws = wb[sheet]
    bottom = top_row + 1
    has_vert = False   # 跨 top..bottom 的竖向合并（标签在顶行，底行因合并而为空）
    has_horiz = False  # top 行内的横向合并块（组名+第二行子表头）
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= top_row and rng.max_row >= bottom:
            if rng.max_col > rng.min_col:
                has_horiz = True
            else:
                has_vert = True
        if has_vert and has_horiz:
            break
    if (has_vert or has_horiz) and bottom <= ws.max_row:
        # 竖向合并的底行为空是正常的（值在顶行）；横向合并则要求底行有子表头
        has_sub = has_vert or any(
            _norm_ws_text(ws.cell(row=bottom, column=c).value)
            for c in range(1, ws.max_column + 1))
    else:
        has_sub = False
    wb.close()
    return bottom if (has_vert or has_horiz) and has_sub else None


def unmerge_fill_data(file_bytes, filename, sheet, header_row, df):
    """把数据区（物理行 > header_row）的合并单元格按「Excel 看起来的效果」填满。

    如类别「消防维保」竖向合并 4 行 → 每行都带上该值（否则只有第一格有值、
    其余是空，做钥匙列/补充列会缺值）。只填空格，不动已有数据；表头区不管
    （表头由 split_two_row_header 处理）。
    返回 (df, 填充的空格数)；df 就地修改并返回。仅支持 xlsx/xlsm，其余原样返回。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm") or df.empty:
        return df, 0
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(file_bytes))
    except Exception:
        return df, 0
    if sheet not in wb.sheetnames:
        wb.close()
        return df, 0
    ws = wb[sheet]
    n_fill = 0
    for rng in ws.merged_cells.ranges:
        if rng.max_row <= header_row:
            continue
        v = ws.cell(row=rng.min_row, column=rng.min_col).value
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        for r in range(rng.min_row, rng.max_row + 1):
            i = r - header_row - 1          # df 行下标（0-based）
            if i < 0 or i >= len(df):
                continue
            for c in range(rng.min_col, rng.max_col + 1):
                j = c - 1                    # df 列下标（0-based，与物理列对齐）
                if j >= len(df.columns):
                    continue
                cur = df.iat[i, j]
                if pd.isna(cur) or (isinstance(cur, str) and not cur.strip()):
                    df.iat[i, j] = v
                    n_fill += 1
    wb.close()
    return df, n_fill


# =================== 公式行检测（保护 SUM/公式不被换算覆写） ===================

def sheet_formula_rows(file_bytes, filename, sheet, cols=None):
    """返回该 Sheet 中含公式的物理行号集合（1-based）。

    pandas 读表用 data_only=True（公式返回缓存值），无法凭文本判断公式行；
    这里用 openpyxl(data_only=False) 读原始公式，供换算跳过与写回保护。

    cols：只检查这些列（1-based 列号集合）；None=检查全部列。
    ⚠️ 比价表常有「最小值 =MIN()」这类每行公式列——必须传 cols 限定为
    所选价格列，否则整张数据区都会被误判为公式行。
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm"):
        return set()
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(file_bytes), data_only=False, read_only=True)
    if sheet not in wb.sheetnames:
        wb.close()
        return set()
    ws = wb[sheet]
    rows = set()
    for row in ws.iter_rows():
        for cell in row:
            if cols is not None and getattr(cell, "column", None) not in cols:
                continue
            v = cell.value
            if isinstance(v, str) and v.startswith("="):
                rows.add(cell.row)
                break
    wb.close()
    return rows
