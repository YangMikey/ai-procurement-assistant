# -*- coding: utf-8 -*-
"""预览式选择组件：表头行点选 + 列点选 + 浏览文件弹窗（仅换算 / 两表匹配两页共用）。"""
import io
import json
import os
import subprocess
import sys

import pandas as pd
import streamlit as st

PREVIEW_ROWS = 13

# 自动猜表头用的关键词库（命中越多越像表头行）
_GUESS_KWS = ["品类", "品名", "名称", "物料", "物资", "型号", "规格", "单位", "数量",
              "单价", "价格", "税率", "金额", "供应商", "序号", "日期", "合同", "总额", "余额"]


def _read_raw(file_bytes, filename, sheet, n=PREVIEW_ROWS):
    """按原始读取前 n 行（不指定表头），用于表头行预览。"""
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "csv":
        last = None
        for enc in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                return pd.read_csv(io.BytesIO(file_bytes), encoding=enc, nrows=n,
                                   dtype=object, keep_default_na=False, header=None)
            except (UnicodeDecodeError, UnicodeError) as e:
                last = e
                continue
        raise ValueError(f"无法识别该 CSV 文件的编码，请用 Excel 另存为 xlsx 后重试：{last}")
    return pd.read_excel(io.BytesIO(file_bytes), sheet_name=sheet,
                         header=None, nrows=n, dtype=object)


def guess_header_row(file_bytes, filename, sheet, max_scan=20):
    """按关键词命中数猜表头行（1-based）。"""
    raw = _read_raw(file_bytes, filename, sheet, max_scan)
    best, best_score = 1, -1
    for i in range(len(raw)):
        score = sum(1 for v in raw.iloc[i] if any(k in str(v) for k in _GUESS_KWS))
        if score > best_score:
            best_score, best = score, i + 1
    return best, best_score


def pick_header_row(file_bytes, filename, sheet, key):
    """原始预览（前 13 行，行号 1-based），点选某一行指定为表头行；不点则用自动猜测。"""
    raw = _read_raw(file_bytes, filename, sheet)
    # 预览只用于选“行”，统一转成字符串显示：
    # 避免列名混合类型(int+"行号")和单元格混合类型触发 pyarrow 序列化失败（前端报错/白屏隐患）
    disp = raw.astype(object).where(raw.notna(), "").astype(str)
    disp.columns = [f"列{c}" for c in range(1, len(disp.columns) + 1)]
    disp.insert(0, "行号", range(1, len(disp) + 1))
    disp.index = range(1, len(disp) + 1)
    guess, score = guess_header_row(file_bytes, filename, sheet)
    st.caption(f"👀 预览前 {len(disp)} 行（可横向滚动看全部列）。"
               f"自动检测疑似表头：**第 {guess} 行**。直接用就点下面按钮；不对就在预览里点选正确的行。")
    c1, c2 = st.columns([1, 3])
    with c1:
        use_guess = st.button(f"✔ 用自动检测（第 {guess} 行）", key=f"{key}_guess_btn")
    with c2:
        st.caption("（列头第一行若是公司标题/合并单元格，真实表头通常在第 2 行）")
    sel = st.dataframe(disp, on_select="rerun", selection_mode=["single-row"],
                       key=key, width="stretch", height=260)
    picked = []
    try:
        picked = list(sel.selection.rows)
    except AttributeError:
        picked = []
    if use_guess:
        return guess
    if picked:
        return int(picked[0]) + 1
    return guess


def pick_columns(df, label, key, mode="multi", height=260):
    """列选择预览：点击列头打勾。mode='multi' 可多选；'single' 单选。返回列名列表（点击顺序）。"""
    st.caption(f"{label}：在下方预览中**点击列头**打勾（前 {min(PREVIEW_ROWS, len(df))} 行预览，可横向滚动）。")
    disp = df.head(PREVIEW_ROWS).copy()
    disp.index = range(1, len(disp) + 1)
    sm = ["multi-column"] if mode == "multi" else ["single-column"]
    sel = st.dataframe(disp, on_select="rerun", selection_mode=sm,
                       key=key, width="stretch", height=height)
    cols = []
    try:
        cols = list(sel.selection.columns)
    except AttributeError:
        cols = []
    return cols


def file_key(*parts):
    """构造随文件/Sheet 变化自动重置的组件 key。"""
    return "_".join(str(p).replace(" ", "_") for p in parts)


def browse_file_path(key):
    """「📂 浏览选择文件」按钮：弹 Windows 原生文件选择框（tkinter 子进程，置顶），
    选中后把路径存入 session_state（{key}_value）。返回当前已选路径（可能为空串）。"""
    if st.button("📂 浏览选择文件", key=key):
        code = (
            "import tkinter as tk, tkinter.filedialog, json\n"
            "r = tk.Tk()\n"
            "r.attributes('-topmost', True)\n"
            "r.withdraw()\n"
            "r.focus_force()\n"
            "p = tkinter.filedialog.askopenfilename(\n"
            "    title='选择 Excel/CSV 文件',\n"
            "    filetypes=[('Excel/CSV', '*.xlsx *.xlsm *.xls *.csv'), ('所有文件', '*.*')])\n"
            "r.destroy()\n"
            "print(json.dumps(p))\n"
        )
        try:
            with st.spinner("已弹出文件选择窗口（若被遮挡请看任务栏）…"):
                out = subprocess.run([sys.executable, "-c", code],
                                     capture_output=True, text=True, timeout=600)
            path = ""
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line:
                    try:
                        path = json.loads(line)
                    except json.JSONDecodeError:
                        path = ""
                    break
            if path:
                st.session_state[f"{key}_value"] = path
                st.toast(f"已选择：{path}")
            else:
                st.info("未选择文件")
        except Exception as e:
            st.error(f"打开文件选择框失败：{e}")
    return st.session_state.get(f"{key}_value", "")


def _ask_paths(key, code_files, label):
    """通用：弹 tkinter 原生选择框，把结果路径列表存进 session_state（{key}_list）。"""
    code = ("import tkinter as tk, tkinter.filedialog, json\n"
            "r = tk.Tk()\n"
            "r.attributes('-topmost', True)\n"
            "r.withdraw()\n"
            "r.focus_force()\n"
            f"p = {code_files}\n"
            "r.destroy()\n"
            "print(json.dumps(p))\n")
    try:
        with st.spinner("已弹出文件选择窗口（若被遮挡请看任务栏）…"):
            out = subprocess.run([sys.executable, "-c", code],
                                 capture_output=True, text=True, timeout=600)
        picked = []
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line:
                try:
                    val = json.loads(line)
                except json.JSONDecodeError:
                    val = None
                if isinstance(val, str) and val:
                    picked = [val]
                elif isinstance(val, (list, tuple)):
                    picked = [str(x) for x in val if str(x).strip()]
                break
        if picked:
            st.session_state[f"{key}_list"] = picked
            st.toast(f"已选择 {len(picked)} 个文件")
        else:
            st.info("未选择文件")
    except Exception as e:
        st.error(f"打开文件选择框失败：{e}")
    return list(st.session_state.get(f"{key}_list", []))


def browse_file_paths(key, label="📂 浏览选择文件（可多选）"):
    """「浏览选择文件」按钮（**多选**）：一次可挑多份 Excel/CSV。

    返回本次累计已选路径列表（存在 session_state：{key}_list）。
    调用方一般把它追加进文本框/列表，再自行去重。
    """
    if st.button(label, key=key):
        return _ask_paths(
            key,
            "tkinter.filedialog.askopenfilenames(\n"
            "    title='选择 Excel/CSV 文件（可多选，Ctrl/Shift 多选）',\n"
            "    filetypes=[('Excel/CSV', '*.xlsx *.xlsm *.xls *.csv'), ('所有文件', '*.*')])",
            label)
    return list(st.session_state.get(f"{key}_list", []))


def browse_dir(key, label="📂 选择文件夹"):
    """弹原生「选择文件夹」对话框，选中后写入 session_state（{key}_dir）。返回该目录（可能为空）。"""
    if st.button(label, key=key):
        code = ("import tkinter as tk, tkinter.filedialog, json\n"
                "r = tk.Tk()\n"
                "r.attributes('-topmost', True)\n"
                "r.withdraw()\n"
                "r.focus_force()\n"
                "p = tkinter.filedialog.askdirectory(title='选择保存文件夹')\n"
                "r.destroy()\n"
                "print(json.dumps(p))\n")
        try:
            with st.spinner("已弹出文件夹选择窗口（若被遮挡请看任务栏）…"):
                out = subprocess.run([sys.executable, "-c", code],
                                     capture_output=True, text=True, timeout=600)
            path = ""
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line:
                    try:
                        path = json.loads(line)
                    except json.JSONDecodeError:
                        path = ""
                    break
            if path:
                st.session_state[f"{key}_dir"] = path
                st.toast(f"已选文件夹：{path}")
        except Exception as e:
            st.error(f"打开文件夹选择框失败：{e}")
    return st.session_state.get(f"{key}_dir", "")


def save_to_folder(file_bytes, file_name, sid, help_text=None):
    """结果「另存到指定文件夹」：输入框 + 选择文件夹 + 一键另存。

    与"下载"互补：下载走浏览器，这里直接写到本机指定目录（默认建议桌面）。
    """
    _home = os.path.expanduser("~")
    _desk = os.path.join(_home, "Desktop")
    _default = st.session_state.get(f"savedir_{sid}") or (_desk if os.path.isdir(_desk) else _home)
    c1, c2, c3 = st.columns([3, 1, 1])
    with c1:
        _dir = st.text_input("另存到文件夹（可直接粘贴完整路径）", value=_default,
                             key=f"savedir_{sid}", help=help_text)
    with c2:
        st.write("")
        _picked = browse_dir(f"browse_dir_{sid}")
        if _picked:
            st.session_state[f"savedir_{sid}"] = _picked
            _dir = _picked
    with c3:
        st.write("")
        if st.button("💾 另存到该文件夹", key=f"savebtn_{sid}"):
            try:
                _d = str(_dir or "").strip().strip('"').strip("'")
                if not _d:
                    st.warning("请先填/选一个文件夹")
                elif not os.path.isdir(_d):
                    st.error(f"文件夹不存在：{_d}")
                else:
                    _out = os.path.join(_d, file_name)
                    with open(_out, "wb") as _fh:
                        _fh.write(file_bytes)
                    st.success(f"已另存：{_out}")
            except Exception as e:
                st.error(f"另存失败：{e}")
