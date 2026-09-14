# -*- coding: utf-8 -*-
"""AI 采购助理 · 网页版（V1）

任务模式：
- 两表匹配补缺（核心，复用已验证的三轮瀑布引擎；支持写回匹配表原文件）
- 仅换算（含税↔不含税；新列插在原价格列右侧；支持写回原文件）
- 完整比价（开发中）

交互：全部预览点选（表头行点行、列选择点列头）；文件来源支持「粘贴路径（可写回原文件）」与「上传（仅下载）」。
保险机制：写回前自动在原工作簿内建「ai副本+原sheet名」备份，已存在则 (1)(2) 递增。
运行：双击 启动采购助理.bat（内部 py -m streamlit run app.py）
"""
import os
import sys
import threading
import traceback
from datetime import datetime as _dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
import pandas as pd

import core as _core
from core.advisor_rule import advise
from core.advisor_llm import advise_llm
from core.aligner import align_quotes
from core.excel_io import (backup_sheet_numbered, detect_header_block_bottom,
                           export_df_bytes, get_sheet_names, list_sheets_from_bytes,
                           read_table, read_table_from_bytes, sheet_formula_rows,
                           split_two_row_header, unmerge_fill_data,
                           write_result_in_sheet, writeback_convert)
from core.converter import convert_tax, parse_rate, parse_rate_from_name
from core.experience import ExperienceStore
from core.highlighter import PRESETS, export_highlighted
from core.llm_client import (CACHE_PATH as LLM_CACHE_PATH, DEFAULT_MODEL,
                             LLMClient, PROVIDER_PRESETS, load_config, save_config)
from core.matcher import run_match
from core.parser_rule import manual_quote, parse_quote_file
from core.parser_llm import parse_quote_auto
from core.templater import fill_template
from core.registry import list_skills
from core.table_filler import COLORS as FILL_COLORS, export_filled, fill_multi
from core.theme import beautify_bytes, beautify_file_in_place
from ui_components import browse_file_path, file_key, pick_columns, pick_header_row

st.set_page_config(page_title="AI 采购助理", page_icon="🧰", layout="wide")
st.title("🧰 AI 采购助理")


def _core_stale():
    """True = core/ 下的 .py 在服务启动后被改过（内存里仍是旧模块）。

    Streamlit 只热重载主脚本 app.py，不重载 core/ 包 → 改了 core 不重启会跑旧逻辑，
    表现为「参数不认识 / 行为不一致」这类误导性报错。这里主动检测并提示重启。
    """
    try:
        return abs(_core.core_files_mtime() - _core.CORE_LOADED_MTIME) > 1e-6
    except Exception:
        return False


CORE_STALE = _core_stale()
if CORE_STALE:
    st.error("⚠️ 检测到 core 代码已更新，但当前服务仍在用旧模块。"
             "请**关闭正在运行的黑窗口**，再双击「启动采购助理.bat」重启服务；"
             "重启前匹配/换算/写回已暂时停用。")

BUILD = "2026-09-14.10"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "app.log")
LOG_MAX_BYTES = 1_000_000       # 超过 ~1MB 自动轮转：app.log → app.log.1（只留一份，占用封顶）


def _rotate_log_if_needed():
    """日志自动清理：超过 LOG_MAX_BYTES 就把 app.log 轮转为 app.log.1（旧的覆盖）。

    启动时执行一次；平时日志每天几百字节，几乎不会触发。
    """
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            bak = LOG_PATH + ".1"
            try:
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(LOG_PATH, bak)
            except Exception:
                open(LOG_PATH, "w", encoding="utf-8").close()   # 兜底：截断重来
    except Exception:
        pass


def log_line(msg):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_dt.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def log_exception(context, e):
    """异常连同 traceback 追加写入 logs/app.log（卡退/报错后可回溯，不用盯黑窗口）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{_dt.now():%Y-%m-%d %H:%M:%S}] {context}\n{traceback.format_exc()}\n")
    except Exception:
        pass


# 全局兜底：主线程/后台线程的未捕获异常也写日志（页面闪退/白屏时留证据）
def _excepthook(exc_type, exc, tb):
    log_exception("未捕获异常（主线程）", exc)
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _excepthook
try:
    def _thread_hook(args):
        log_exception(f"未捕获异常（线程 {getattr(args.thread, 'name', '?')}）", args.exc_value)
    threading.excepthook = _thread_hook
except Exception:
    pass

# 每次会话首次进入：先轮转清理，再记录一行启动信息
if not st.session_state.get("_boot_logged"):
    _rotate_log_if_needed()
    log_line(f"app started · build {BUILD} · python {sys.version.split()[0]} · streamlit {st.__version__}")
    st.session_state["_boot_logged"] = True

mode = st.sidebar.radio("任务模式", ["两表匹配补缺", "仅换算", "仅对齐", "完整比价", "多表补全", "表格美化"])
st.sidebar.caption(f"build {BUILD}")
st.sidebar.caption("数据本地处理，不上传任何服务器；LLM 功能默认关闭。")
st.sidebar.caption("写回原文件前自动建「ai副本」备份；文件被 WPS 占用会提示。")
with st.sidebar.expander("🧩 技能注册表（V2 意图路由地基）"):
    st.json({c["name"]: {"desc": c["desc"], "modes": c["task_modes"]}
             for c in list_skills()}, expanded=False)

store = ExperienceStore()
_llm_client = LLMClient()      # 读 data/llm_config.json（默认关，不发请求）

UP_TYPES = ["xlsx", "xlsm", "xls", "csv"]

_llm_cfg = load_config()
with st.sidebar.expander("🤖 LLM（可选 · 默认关）", expanded=False):
    st.caption("用于非标解析/语义兜底；**默认关闭**，开启后相关数据会发送到所选服务商。")
    _provs = list(PROVIDER_PRESETS.keys())
    _prov0 = _llm_cfg.get("provider") or "opencode-go"
    _prov = st.selectbox("服务商", _provs,
                         index=_provs.index(_prov0) if _prov0 in _provs else 0, key="llm_prov")
    _mdl = st.text_input("模型", value=_llm_cfg.get("model") or DEFAULT_MODEL.get(_prov, ""),
                         key="llm_model")
    _en = st.checkbox("启用 LLM", value=bool(_llm_cfg.get("enabled", False)), key="llm_en")
    if st.button("💾 保存 LLM 配置", key="llm_save"):
        try:
            save_config({"provider": _prov, "model": _mdl, "enabled": _en,
                         "base_url": _llm_cfg.get("base_url")})
            st.success("已保存到 data/llm_config.json")
        except Exception as e:
            st.error(f"保存失败：{e}")
    if st.button("🔌 连通性自检", key="llm_test"):
        try:
            _ok, _msg = LLMClient(provider=_prov, model=_mdl, cfg_path="__none__").self_test()
            (st.success if _ok else st.error)(_msg)
        except Exception as e:
            st.error(f"自检失败：{e}")
    if st.button("🧹 清空 LLM 缓存", key="llm_clear"):
        try:
            os.remove(LLM_CACHE_PATH)
            st.success("已清空 LLM 缓存")
        except Exception:
            st.caption("无缓存可清")
    st.caption("缓存 data/llm_cache.json · 调用日志 logs/llm.log · 单次运行有调用上限")

with st.sidebar.expander(f"🧠 经验库（{len(store.pairs)} 条）"):
    st.caption("人工确认沉淀、**兜底**用（精确/模糊都配不上时才生效）；可随时删除/清空。")
    _pairs = store.list_pairs()
    if _pairs:
        st.dataframe(pd.DataFrame([{"左键": p["a"], "右键": p["b"],
                                    "命中": p.get("hits", 1), "来源": p.get("src", "")}
                                   for p in _pairs]), height=160, width="stretch")
        _opts = [f"{i + 1}. {p['a']} → {p['b']}" for i, p in enumerate(_pairs)]
        _sel = st.selectbox("选择要删除的一条", _opts, key="_exp_del_sel")
        if st.button("🗑 删除这条", key="_exp_del"):
            try:
                store.remove(_pairs[_opts.index(_sel)]["a"], raw=True)
                st.rerun()
            except Exception as e:
                st.caption(f"删除失败：{e}")
        if st.button("🧹 清空全部", key="_exp_clear"):
            try:
                store.clear()
                st.rerun()
            except Exception as e:
                st.caption(f"清空失败：{e}")
    else:
        st.caption("暂无记录（人工确认后在此沉淀）")

with st.sidebar.expander("🐞 运行日志（logs/app.log · 超 1MB 自动轮转）"):
    if os.path.exists(LOG_PATH):
        try:
            _kb = os.path.getsize(LOG_PATH) / 1024
            st.caption(f"当前 {_kb:.1f} KB（超过 1MB 自动归档为 app.log.1）")
            with open(LOG_PATH, encoding="utf-8", errors="replace") as _f:
                _tail = _f.readlines()[-80:]
            st.code("".join(_tail) or "(空)", language="text")
        except Exception as _e:
            st.caption(f"读取日志失败：{_e}")
    else:
        st.caption("暂无日志（启动后会自动写入；出现异常会记录 traceback）")
    if st.button("🗑 清空日志", key="_clear_log"):
        try:
            open(LOG_PATH, "w", encoding="utf-8").close()
            log_line(f"日志已清空 · build {BUILD}")
            st.rerun()
        except Exception as _e:
            st.caption(f"清空失败：{_e}")


def file_input(sid, label):
    """文件来源二选一：路径（可写回原文件）或上传（仅下载）。返回 src dict 或 None。"""
    src_kind = st.radio(label + " · 文件来源",
                        ["粘贴文件路径（可写回原文件）", "上传文件（仅出下载件）"],
                        key=f"src_{sid}", horizontal=True)
    if src_kind.startswith("粘贴"):
        sel = browse_file_path(key=f"browse_{sid}")
        if sel:
            st.session_state[f"path_{sid}"] = sel
        p = st.text_input(label + " · 完整文件路径", key=f"path_{sid}",
                          placeholder=r"例如 C:\Users\zq130\Desktop\华二-欠款.xlsx")
        p = (p or "").strip().strip('"').strip("'")
        if not p:
            st.info("粘贴完整路径：资源管理器里按住 Shift 右键文件 → 「复制文件地址」")
            return None
        if not os.path.exists(p):
            st.error("路径不存在，检查一下（注意扩展名是否被隐藏）")
            return None
        with open(p, "rb") as fh:
            data = fh.read()
        return {"mode": "path", "path": p, "bytes": data, "name": os.path.basename(p)}
    f = st.file_uploader(label, type=UP_TYPES, key=f"up_{sid}")
    if not f:
        return None
    return {"mode": "upload", "path": None, "bytes": f.getvalue(), "name": f.name}


@st.cache_data(show_spinner=False)
def _load_table_cached(src_bytes, src_name, sheet, hdr):
    """读表 + 两行合并表头拆分 + 数据区合并填充（纯数据部分，结果按文件内容缓存）。

    交互（点选列/钥匙）会整页重跑，缓存避免每次重复读盘/解析工作簿——否则
    每次交互一个表要完整加载 4~5 遍，大文件明显卡顿。
    返回 (df, names, is_two, n_fill)。
    """
    df = read_table_from_bytes(src_bytes, src_name, sheet, hdr)
    try:
        names, is_two = split_two_row_header(src_bytes, src_name, sheet, hdr)
    except Exception:
        names, is_two = None, False
    if names and is_two:
        n = min(len(names), len(df.columns))
        ren = {df.columns[j]: names[j] for j in range(n)
               if str(names[j]) and not str(names[j]).startswith("Unnamed")}
        df = df.rename(columns=ren)
    try:
        df, n_fill = unmerge_fill_data(src_bytes, src_name, sheet, hdr, df)
    except Exception:
        n_fill = 0
    return df, names, is_two, n_fill


def load_table(src, sid):
    """Sheet 选择 → 表头行预览点选 → 读表（缓存）→ 拆分重命名 + 合并填充。
    返回 (df, sheet, header_row)。"""
    try:
        if src["mode"] == "path":
            sheets = get_sheet_names(src["path"])
        else:
            sheets = list_sheets_from_bytes(src["bytes"], src["name"])
    except Exception:
        # 文件被 WPS/Excel 占用等场景：改用内存字节流兜底
        try:
            sheets = list_sheets_from_bytes(src["bytes"], src["name"])
        except Exception as e:
            log_exception(f"读 Sheet 列表失败 {src['name']}", e)
            st.error(f"读取 Sheet 列表失败：{e}")
            return None, None, None
    sheet = st.selectbox("Sheet", sheets, key=f"sheet_{sid}")
    hdr = pick_header_row(src["bytes"], src["name"], sheet, key=file_key("hdr", sid, sheet))
    # 用户/自动检测把表头行点在了两行合并表头的「顶行」→ 自动降级到底行
    try:
        bottom = detect_header_block_bottom(src["bytes"], src["name"], sheet, hdr)
    except Exception as e:
        log_exception(f"顶行检测失败 {src['name']}/{sheet}", e)
        bottom = None
    if bottom and bottom > hdr:
        st.caption(f"💡 检测到第 {hdr}~{bottom} 行是合并的两行表头，已自动按第 {bottom} 行作为表头行读取。")
        hdr = bottom
    try:
        df, names, is_two, n_fill = _load_table_cached(
            src["bytes"], src["name"], sheet, hdr)
        df = df.copy(deep=True)   # 缓存返回值不被交互改写
        if names and is_two and any(str(c).startswith("Unnamed") for c in df.columns):
            st.caption("💡 已自动拆分合并表头（供应商名+子表头合并显示）；个别 Unnamed 列可能是纯空列。")
        if n_fill:
            st.caption(f"💡 已按 Excel 显示效果填充合并单元格 {n_fill} 个空格（数据区，如竖向合并的类别列）。")
        return df, sheet, hdr
    except Exception as e:
        log_exception(f"读表失败 {src['name']}/{sheet}", e)
        st.error(f"读取失败：{e}")
        return None, sheet, hdr


def build_skip_mask(df, formula_rows=None, header_row=None):
    """合计行/公式行 → True（换算跳过、写回不动原值，保护 SUM/公式区）。

    formula_rows：sheet_formula_rows() 返回的物理行号集合（1-based）。
    pandas 用 data_only 读表拿不到公式文本（公式返回缓存值），
    必须靠它识别「不含税合计行」这类公式行。
    """
    mask = []
    for i, (_, r) in enumerate(df.iterrows()):
        txts = [str(v) for v in r.values]
        skip = ("合计" in " ".join(txts)) or any(t.strip().startswith("=") for t in txts)
        if formula_rows and header_row and (header_row + 1 + i) in formula_rows:
            skip = True
        mask.append(skip)
    return mask


def can_writeback(src):
    return (src is not None and src["mode"] == "path"
            and os.path.splitext(src["name"])[1].lower() in (".xlsx", ".xlsm"))


if mode == "两表匹配补缺":
    st.header("🔗 两表匹配补缺（VLOOKUP 类）")
    st.caption("匹配表=要补缺的表；钥匙表=提供补缺数据的表。钥匙列两侧各点选、**按点击顺序一一对应**，列名不必相同、内容对得上即可。")
    c1, c2 = st.columns(2)
    with c1:
        m_src = file_input("m", "① 匹配表（要补缺的表，可被写回）")
    with c2:
        k_src = file_input("k", "② 钥匙表（提供补缺数据的表）")

    if m_src and k_src:
        cL, cR = st.columns(2)
        with cL:
            st.markdown("**① 匹配表 · Sheet 与表头行**")
            try:
                m_df, m_sheet, m_hdr = load_table(m_src, file_key("m", m_src["name"], m_src["path"]))
            except Exception as e:
                m_df, m_sheet, m_hdr = None, None, None
                log_exception(f"匹配表面板 {m_src['name']}", e)
                st.error(f"匹配表读取出错：{e}")
        with cR:
            st.markdown("**② 钥匙表 · Sheet 与表头行**")
            try:
                k_df, k_sheet, k_hdr = load_table(k_src, file_key("k", k_src["name"], k_src["path"]))
            except Exception as e:
                k_df, k_sheet, k_hdr = None, None, None
                log_exception(f"钥匙表面板 {k_src['name']}", e)
                st.error(f"钥匙表读取出错：{e}")

        if m_df is not None and k_df is not None:
            st.caption(f"匹配表 {len(m_df)} 行 × {len(m_df.columns)} 列（表头第 {m_hdr} 行）｜"
                       f"钥匙表 {len(k_df)} 行 × {len(k_df.columns)} 列（表头第 {k_hdr} 行）")

            st.subheader("③ 在预览中配置字段")
            st.markdown("**钥匙列**（两侧各点选，数量需一致，点击顺序即配对顺序）")
            c1, c2 = st.columns(2)
            with c1:
                m_keys = pick_columns(m_df, "🔑 匹配表·钥匙列",
                                      key=file_key("mk", m_src["name"], m_sheet, m_hdr))
            with c2:
                k_keys = pick_columns(k_df, "🔑 钥匙表·钥匙列",
                                      key=file_key("kk", k_src["name"], k_sheet, k_hdr))
            if m_keys and k_keys and len(m_keys) != len(k_keys):
                st.warning(f"钥匙列两侧数量不一致：匹配表 {len(m_keys)} 个 / 钥匙表 {len(k_keys)} 个")

            use_supp = st.checkbox("启用补充列（钥匙没配上的行用更多列进一步甄别，可选）")
            m_supp, k_supp = [], []
            if use_supp:
                st.markdown("**补充列**（同样按点击顺序配对）")
                c1, c2 = st.columns(2)
                with c1:
                    m_supp = pick_columns(m_df, "➕ 匹配表·补充列",
                                          key=file_key("ms", m_src["name"], m_sheet, m_hdr))
                with c2:
                    k_supp = pick_columns(k_df, "➕ 钥匙表·补充列",
                                          key=file_key("ks", k_src["name"], k_sheet, k_hdr))
                if m_supp and k_supp and len(m_supp) != len(k_supp):
                    st.warning(f"补充列两侧数量不一致：{len(m_supp)} / {len(k_supp)}")

            st.markdown("**查补列**（从钥匙表点选要补进匹配表的列，可多选）")
            fetch_fields = pick_columns(k_df, "📤 查补列",
                                        key=file_key("ff", k_src["name"], k_sheet, k_hdr))
            alias = {}
            if fetch_fields:
                st.caption("每个查补列的**写入位置**：默认新建列；也可填入匹配表已有列（如空着的「事业部」列，就地更新）。")
                for j, fname in enumerate(fetch_fields):
                    choice = st.selectbox(
                        f"「{fname}」写入位置",
                        ["新建列"] + list(m_df.columns),
                        key=file_key("alias", k_src["name"], fname, j))
                    if choice != "新建列":
                        alias[fname] = choice

            st.subheader("④ 匹配规则")
            rule = st.radio("匹配规则", ["智能匹配（精确 → 模糊 → 补充列）", "仅精确"], horizontal=True)
            threshold = st.slider("模糊阈值（%）", 50, 100, 80,
                                  help="钥匙列各列相似度需 ≥ 阈值；仅精确模式不生效") if rule.startswith("智能") else 100.0

            valid = (m_keys and k_keys and len(m_keys) == len(k_keys)
                     and fetch_fields
                     and (not use_supp or (m_supp and k_supp and len(m_supp) == len(k_supp))))
            if st.button("⑤ 开始匹配", type="primary", disabled=(not valid) or CORE_STALE):
                logs = []
                try:
                    with st.spinner("匹配中…"):
                        res = run_match(
                            m_df, k_df, m_keys, k_keys, m_supp, k_supp, fetch_fields,
                            mode="fuzzy" if rule.startswith("智能") else "exact",
                            threshold=float(threshold), log=logs.append,
                            fetch_alias=alias or None,
                            experience=store,
                            llm=_llm_client,
                        )
                    st.session_state["last_match"] = res
                except Exception as e:
                    log_exception(f"匹配失败 m={m_src['name']} k={k_src['name']}", e)
                    st.error(f"匹配出错（已记日志）：{e}")
                    res = None
                st.session_state["last_match_logs"] = logs
            res = st.session_state.get("last_match")
            if res:
                with st.expander("运行日志"):
                    st.text("\n".join(st.session_state.get("last_match_logs", [])))
                s1, s2, s3, s4, s5, s6, s7, s8 = st.columns(8)
                stats = res["stats"]
                s1.metric("经验库兜底", stats.get("经验库", 0))
                s2.metric("LLM兜底", stats.get("LLM语义", 0))
                s3.metric("钥匙精确", stats["钥匙精确"])
                s4.metric("钥匙模糊", stats["钥匙模糊"])
                s5.metric("补充列辅助", stats["补充列辅助"])
                s6.metric("重复预警", stats["重复预警"])
                s7.metric("人工忽略", stats.get("人工忽略", 0))
                s8.metric("未匹配", stats["未匹配"])
                st.dataframe(res["result"], height=480, width="stretch")

                # ---- 人工确认（经验库兜底）：重复预警挑一条 / 未匹配忽略 / 模糊项可选记住 ----
                _rs = res["row_status"]
                _dup_rows = [i for i, s in _rs.items() if s["status"] == "重复预警"]
                _un_rows = [i for i, s in _rs.items() if s["status"] == "未匹配"]
                _fz_rows = [i for i, s in _rs.items() if s["status"] in ("钥匙模糊", "补充列辅助", "LLM语义")]
                if _dup_rows or _un_rows or _fz_rows:
                    with st.expander(f"🧠 人工确认（重复预警 {len(_dup_rows)} / 未匹配 {len(_un_rows)} / 模糊 {len(_fz_rows)}）"):
                        st.caption("经验库只在**精确/模糊都配不上时兜底**；这里确认的配对下次兜底用，**可随时覆盖/删除**。")

                        def _mlab(i, cols):
                            return "/".join(str(m_df.iloc[i][c]) for c in cols)

                        for i in _dup_rows:
                            combos = _rs[i]["combos"]  # [(钥匙行下标, 匹配率)]
                            m_left = [m_df.iloc[i][c] for c in m_keys]

                            def _klab(j):
                                kv = [k_df.iloc[j][c] for c in k_keys]
                                fv = [k_df.iloc[j][c] for c in fetch_fields]
                                return " ｜ ".join(str(x) for x in (kv + fv))

                            sel = st.selectbox(
                                f"第 {i + 1} 行「{_mlab(i, m_keys)}」对上 {len(combos)} 条，选正确的：",
                                options=list(range(len(combos))),
                                format_func=lambda x, _c=combos: _klab(_c[x][0]),
                                key=file_key("confdup", m_src["name"], k_src["name"], i))
                            if st.button("✅ 确认这条", key=file_key("confdupbtn", m_src["name"], k_src["name"], i)):
                                try:
                                    k_right = [k_df.iloc[combos[sel][0]][c] for c in k_keys]
                                    store.record(m_left, k_right, src="manual")
                                    st.success("已记入经验库（同键会覆盖旧值）；重跑后该行走「经验库兜底」。")
                                except Exception as e:
                                    log_exception("经验库写入失败", e)
                                    st.error(f"写入失败：{e}")

                        if _un_rows:
                            st.markdown("**未匹配行**（确属无对应 → 标记忽略，下次不再打扰）")
                            for i in _un_rows[:30]:
                                m_left = [m_df.iloc[i][c] for c in m_keys]
                                if st.button(f"🚫 标记忽略：{_mlab(i, m_keys)}",
                                             key=file_key("confun", m_src["name"], k_src["name"], i)):
                                    try:
                                        store.record(m_left, ignore=True, src="manual")
                                        st.success("已标记忽略；重跑后该行显示「人工忽略」。")
                                    except Exception as e:
                                        log_exception("经验库忽略写入失败", e)
                                        st.error(f"写入失败：{e}")

                        if _fz_rows:
                            st.markdown("**模糊命中的行**（已填结果，默认**不入库**；核对无误可「记住」供下次兜底）")
                            for i in _fz_rows[:30]:
                                j = _rs[i]["combos"][0][0] if _rs[i]["combos"] else None
                                if j is None:
                                    continue
                                m_left = [m_df.iloc[i][c] for c in m_keys]
                                k_right = [k_df.iloc[j][c] for c in k_keys]
                                c1, c2 = st.columns([5, 1])
                                c1.markdown(f"{_rs[i]['status']}：`{_mlab(i, m_keys)}` → "
                                            f"`{'/'.join(str(x) for x in k_right)}`")
                                if c2.button("☑ 仍要记住", key=file_key("conffz", m_src["name"], k_src["name"], i)):
                                    try:
                                        store.record(m_left, k_right, src="manual")
                                        st.success("已记住；精确/模糊都配不上时用它兜底。")
                                    except Exception as e:
                                        log_exception("经验库模糊写入失败", e)
                                        st.error(f"写入失败：{e}")

                try:
                    buf = export_df_bytes(res["result"], sheet_name="补缺结果")
                    st.download_button("⬇️ 下载结果 Excel", buf, file_name="匹配结果.xlsx",
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                except Exception as e:
                    log_exception("导出下载件失败", e)
                    st.error(f"生成下载件出错（已记日志）：{e}")
                if can_writeback(m_src):
                    if st.button("✍️ 写回匹配表原文件（自动先建 ai副本）", type="primary", disabled=CORE_STALE):
                        try:
                            with st.spinner("备份 + 写回中…"):
                                bk = backup_sheet_numbered(m_src["path"], m_sheet)
                                row_map = list(range(len(m_df)))
                                written, overwritten = write_result_in_sheet(
                                    m_src["path"], m_sheet, m_hdr, row_map,
                                    res["row_status"], res["fetch_records"],
                                    [alias.get(f, f) for f in fetch_fields])
                            st.success(f"已写回原文件！备份 Sheet：{bk}；写入列：{written}")
                            if overwritten:
                                st.warning(f"以下已有列被就地更新（原值都在备份里）：{overwritten}")
                        except Exception as e:
                            log_exception(f"写回失败 {m_src['path']} / {m_sheet}", e)
                            st.error(f"写回失败（已记日志 logs/app.log）：{e}（可改用「下载结果」手动处理）")
                elif m_src and m_src["mode"] == "path":
                    st.info("匹配表不是 xlsx/xlsm（xls/csv），不支持原位写回，请用下载结果。")
                with st.expander("🧠 匹配经验库"):
                    st.caption("人工确认的配对会沉淀进来（V2 全量接入流水线第①级），越用越快。")
                    st.json(store.stats(), expanded=False)

elif mode == "仅换算":
    st.header("🧮 仅换算（含税 ↔ 不含税）")
    st.caption("新列会插在**每个原价格列的右侧**；**每列独立换算**——税率从列名自动识别（表头带「税率13%」「（1%）」这类字样会自动填好），识别不到用全局税率，随时手动改。合并表头（供应商名+子表头）会自动拆分命名。")
    src = file_input("c", "价格表")
    if src:
        df, sheet, hdr = load_table(src, file_key("c", src["name"], src["path"]))
        if df is not None:
            price_cols = pick_columns(df, "💰 要换算的价格列（可多选）",
                                      key=file_key("pc", src["name"], sheet, hdr))

            direction = st.radio("换算方向", ["含税 → 不含税", "不含税 → 含税"], horizontal=True)
            rate_choice = st.selectbox("全局税率（列名识别不到时的默认值）", ["13%", "9%", "6%", "3%", "0%", "自定义"])
            if rate_choice == "自定义":
                tax_rate = st.number_input("自定义税率（%）", 0.0, 100.0, 13.0) / 100
            else:
                tax_rate = float(rate_choice.rstrip("%")) / 100

            rates_list, rate_bad = [], []
            if price_cols:
                st.markdown("**每列税率**（已按列名自动识别，可直接修改）。"
                            "📌 规则：**框内数字 = 百分比数值**——填 `1` 就是 1%、填 `13` 就是 13%；带 % 号也认。")
                rcols = st.columns(min(len(price_cols), 4))
                for j, pc in enumerate(price_cols):
                    auto = parse_rate_from_name(pc)
                    default_pct = round((auto if auto is not None else tax_rate) * 100, 4)
                    with rcols[j % len(rcols)]:
                        v = st.text_input(f"{pc}", value=f"{default_pct:g}",
                                          key=file_key("rate", src["name"], sheet, hdr, pc))
                        try:
                            r_parsed = parse_rate(v)
                            st.caption(f"→ 解析为 {r_parsed * 100:g}%")
                            rates_list.append(r_parsed)
                        except Exception:
                            st.caption(f"⚠️ 看不懂，按全局 {rate_choice} 处理")
                            rates_list.append(tax_rate)
                            rate_bad.append(pc)
                if rate_bad:
                    st.warning(f"这些列的税率填法没看懂（已按全局 {rate_choice} 处理，可修改后重算）：{rate_bad}")
                st.caption("💡 改全局税率不会自动覆盖已生成的税率框，需要的话逐个改即可。")

            try:
                price_positions = [df.columns.get_loc(pc) + 1 for pc in price_cols]
                f_rows = sheet_formula_rows(src["bytes"], src["name"], sheet,
                                            cols=set(price_positions))
            except Exception:
                f_rows = set()
            skip_mask = build_skip_mask(df, formula_rows=f_rows, header_row=hdr)
            n_skip = sum(skip_mask)
            if price_cols and n_skip:
                st.caption(f"⛔ 自动跳过 {n_skip} 行（合计行/公式行）：这些行不换算，写回时原值不动。")

            if price_cols and st.button("开始换算", type="primary", disabled=CORE_STALE):
                with st.spinner("换算中…"):
                    out, added = convert_tax(
                        df, price_cols, tax_rate=tax_rate,
                        direction="to_ex" if direction.startswith("含税") else "to_in",
                        rates=rates_list, skip_mask=skip_mask,
                    )
                st.session_state["last_convert"] = {"out": out, "added": added}
            conv = st.session_state.get("last_convert")
            if conv:
                out, added = conv["out"], conv["added"]
                st.success(f"已新增 {len(added)} 列（插在各自原价格列右侧）：{added}")
                st.dataframe(out, height=420, width="stretch")
                buf = export_df_bytes(out, sheet_name="换算结果")
                st.download_button("⬇️ 下载换算结果", buf, file_name="换算结果.xlsx",
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                if can_writeback(src):
                    if st.button("✍️ 写回原文件（自动先建 ai副本）", type="primary", disabled=CORE_STALE):
                        try:
                            with st.spinner("备份 + 写回中…"):
                                bk = backup_sheet_numbered(src["path"], sheet)
                                items = [(df.columns.get_loc(pc) + 1, added[j], out[added[j]].tolist())
                                         for j, pc in enumerate(price_cols) if j < len(added)]
                                written = writeback_convert(src["path"], sheet, hdr, items)
                            st.success(f"已写回原文件！备份 Sheet：{bk}；新增列：{written}")
                        except Exception as e:
                            log_exception(f"换算写回失败 {src['path']} / {sheet}", e)
                            st.error(f"写回失败（已记日志 logs/app.log）：{e}（可改用「下载结果」手动处理）")
                elif src["mode"] == "path":
                    st.info("该文件不是 xlsx/xlsm（xls/csv），不支持原位写回，请用下载结果。")

elif mode in ("仅对齐", "完整比价"):
    _align_only = (mode == "仅对齐")
    if _align_only:
        st.header("🔗 仅对齐（多份报价 → 对齐成矩阵）")
        st.caption("只做解析 + 对齐，产出比价矩阵（不标红/不出建议）；默认按**不含税**对齐。")
    else:
        st.header("📊 完整比价")
        st.caption("多份报价单 → 解析 → 对齐成矩阵 → 口径统一 → 最低价标红 → 采购建议。"
                   "默认按**不含税**比价；报价单不出本机。")

    _ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
    _RAW_DIR = os.path.join(_ROOT_DIR, "data", "raw_quotes")
    _OUT_DIR = os.path.join(_ROOT_DIR, "data", "outputs")

    st.subheader("① 选择报价单")
    _src = st.radio("报价单来源", ["从项目 raw_quotes 目录选", "上传文件", "粘贴文件路径"],
                    horizontal=True, key="cmp_src")
    _paths, _uploads = [], []
    if _src.startswith("从项目"):
        _files = sorted(f for f in os.listdir(_RAW_DIR)
                        if f.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv"))) if os.path.isdir(_RAW_DIR) else []
        _pick = st.multiselect("选择文件（可多选）", _files, default=_files, key="cmp_pick")
        _paths = [os.path.join(_RAW_DIR, f) for f in _pick]
        if not _files:
            st.info(f"目录为空：把报价单放进 `{_RAW_DIR}`（项目内，分类存放）")
    elif _src.startswith("上传"):
        _ups = st.file_uploader("上传报价单（可多选）", type=UP_TYPES,
                                accept_multiple_files=True, key="cmp_up")
        _uploads = [(u.name, u.getvalue()) for u in (_ups or [])]
    else:
        _txt = st.text_area("每行一个完整文件路径", height=120, key="cmp_paths",
                            placeholder=r"例如 C:\Users\zq130\Desktop\工作台\9.10\001.xlsx")
        for _line in (_txt or "").splitlines():
            _p = _line.strip().strip('"').strip("'")
            if _p:
                _paths.append(_p)

    _use_llm_parse = st.checkbox("🤖 非标文件用 LLM 解析（规则认不出时；需侧栏已启用 LLM）",
                                 value=bool(_llm_client.enabled), key="cmp_llmparse")

    with st.expander("✍️ 手动录入报价（F16 · 口头/微信报价等）", expanded=False):
        _msup = st.text_input("供应商名", key="cmp_msup", placeholder="如：利源通")
        _mrow = st.data_editor(
            pd.DataFrame([{"品名": "", "规格": "", "单位": "", "数量": "", "价格": "", "税率": "13"}]),
            num_rows="dynamic", width="stretch", key="cmp_meditor")
        _mfield = st.radio("价格口径", ["含税单价", "不含税单价"], horizontal=True, key="cmp_mfield")
        if st.button("＋ 加入报价", key="cmp_madd"):
            _rows = [dict(r) for _, r in _mrow.iterrows()
                     if str(r.get("品名", "")).strip() or str(r.get("价格", "")).strip()]
            if not _rows:
                st.warning("请至少填一行（品名/价格）")
            else:
                _mq = manual_quote(_msup or "手动录入", _rows, price_field=_mfield)
                _cur = st.session_state.get("cmp_quotes") or []
                _cur.append(_mq)
                st.session_state["cmp_quotes"] = _cur
                st.success(f"已加入手动报价：{_mq['supplier']}（{len(_mq['df'])} 行）")

    if st.button("解析报价单", type="primary", disabled=not (_paths or _uploads), key="cmp_parse"):
        _quotes = []
        _used_llm = False
        _llm_arg = _llm_client if (_use_llm_parse and _llm_client.enabled) else None
        try:
            with st.spinner("解析中…（非标文件可能调用 LLM，稍等）"):
                for _p in _paths:
                    if not os.path.exists(_p):
                        log_line(f"比价：路径不存在 {_p}")
                        continue
                    _qs, _ul = parse_quote_auto(path=_p, llm=_llm_arg)
                    _used_llm = _used_llm or _ul
                    _quotes.extend(_qs)
                for _nm, _data in _uploads:
                    _qs, _ul = parse_quote_auto(data=_data, filename=_nm, llm=_llm_arg)
                    _used_llm = _used_llm or _ul
                    _quotes.extend(_qs)
            st.session_state["cmp_quotes"] = _quotes
            st.session_state["cmp_used_llm"] = _used_llm
        except Exception as e:
            log_exception("比价解析失败", e)
            st.error(f"解析出错（已记日志）：{e}")

    _quotes = st.session_state.get("cmp_quotes")
    if _quotes:
        if st.session_state.get("cmp_used_llm"):
            st.caption("🤖 本次有文件走 **LLM 解析**（规则认不出），已标注来源，建议核对映射。")
        _prev = []
        for _q in _quotes:
            _prev.append({"供应商": _q["supplier"], "来源": f"{_q['source']} · {_q['sheet']}",
                          "解析方式": "LLM" if _q.get("via") == "llm" else "规则",
                          "表头行": _q["header_row"], "有效行": len(_q["df"]),
                          "识别字段": "、".join(_q["mapping"].keys()),
                          "警告": "；".join(_q["warnings"])})
        st.dataframe(pd.DataFrame(_prev), width="stretch")
        with st.expander("修正：供应商名（解析后仍可改）"):
            for _i, _q in enumerate(_quotes):
                _q["supplier"] = st.text_input(f"第 {_i + 1} 份供应商名", value=_q["supplier"],
                                               key=file_key("cmp_sup", _i, _q["source"], _q["sheet"]))

        st.subheader("② 对齐配置")
        _allf = [f for f in ("品名", "规格", "单位", "数量")
                 if any(f in _q["df"].columns for _q in _quotes)]
        _c1, _c2, _c3 = st.columns([3, 2, 3])
        with _c1:
            _keyf = st.multiselect("对齐键字段（组合键）", _allf,
                                   default=[f for f in ("品名", "规格") if f in _allf], key="cmp_keyf")
        with _c2:
            _pf = st.radio("比价口径", ["不含税单价", "含税单价"], index=0,
                           horizontal=True, key="cmp_pf")
        with _c3:
            _thr = st.slider("模糊阈值（越低越'尽量填充'）", 30, 100, 60, key="cmp_thr",
                             help="去格式后完全相同→精确命中（不备注）；其余命中都会在"
                                  "「对齐备注」写清方式+相似度，供你复查")
        if st.button("对齐生成矩阵", type="primary", disabled=not _keyf, key="cmp_align"):
            try:
                with st.spinner("对齐中…"):
                    _res = align_quotes(_quotes, price_field=_pf, key_fields=tuple(_keyf),
                                        threshold=float(_thr), experience=store)
                _res["price_field"] = _pf
                st.session_state["cmp_res"] = _res
            except Exception as e:
                log_exception("比价对齐失败", e)
                st.error(f"对齐出错（已记日志）：{e}")

    _cres = st.session_state.get("cmp_res")
    if _cres:
        _m, _conf = _cres["matrix"], _cres["confidence"]
        st.subheader("③ 比价矩阵")
        st.caption(f"口径：{_cres['price_field']}｜{len(_m)} 个品类 × {len(_cres['suppliers'])} 家")
        st.dataframe(_m, height=420, width="stretch")

        if _cres["low_confidence"]:
            with st.expander(f"🧠 人工确认 · 低/中置信度对齐（{len(_cres['low_confidence'])} 项）"):
                st.caption("确认后写入本地经验库；下次同样的异名走**经验库兜底**。")
                for _i, _it in enumerate(_cres["low_confidence"][:40]):
                    _c1, _c2 = st.columns([5, 1])
                    _c1.markdown(f"**{_it['supplier']}**：`{_it['品名']}` → 对齐到 `{_it['对齐到']}`"
                                 f"（{_it['置信度']} · 相似度 {_it['相似度']} · {_it['原因']}）")
                    if _c2.button("✅ 确认", key=file_key("cmp_al", _i, _it["supplier"], _it["品名"])):
                        try:
                            store.record(_it["alias_key"], _it["canon_key"], src="manual", raw=True)
                            st.success("已记入经验库；点「对齐生成矩阵」重跑即命中。")
                        except Exception as e:
                            log_exception("比价经验库写入失败", e)
                            st.error(f"写入失败：{e}")

        if _align_only:
            st.subheader("④ 导出对齐矩阵")
            try:
                os.makedirs(_OUT_DIR, exist_ok=True)
                _fn = f"对齐矩阵_{_dt.now():%Y%m%d_%H%M%S}.xlsx"
                _bytes = export_df_bytes(_m, sheet_name="对齐矩阵")
                with open(os.path.join(_OUT_DIR, _fn), "wb") as _fh:
                    _fh.write(_bytes)
                st.download_button("⬇️ 下载对齐矩阵", _bytes, file_name=_fn,
                                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                   key="cmp_align_dl")
                st.success(f"已导出到项目内：data\\outputs\\{_fn}")
            except Exception as e:
                log_exception("对齐矩阵导出失败", e)
                st.error(f"导出出错（已记日志）：{e}")
        else:
            st.subheader("④ 标红颜色")
            _names = [p[0] for p in PRESETS]
            _c1, _c2, _c3 = st.columns([2, 2, 2])
            with _c1:
                _pickc = st.selectbox("配色（浅色彩虹）", _names + ["自定义"],
                                      index=_names.index("浅黄"), key="cmp_color")
            if _pickc == "自定义":
                with _c2:
                    _fill = st.color_picker("底色", "#FFEB9C", key="cmp_fill")
                with _c3:
                    _font = st.color_picker("字色", "#9C6500", key="cmp_font")
            else:
                _fill = dict((p[0], p[1]) for p in PRESETS)[_pickc]
                _font = dict((p[0], p[2]) for p in PRESETS)[_pickc]
                with _c2:
                    st.markdown(f"<span style='background:{_fill};color:{_font};padding:4px 10px;"
                                f"border-radius:4px'>示例：最低价</span>", unsafe_allow_html=True)

            if st.button("标红并导出比价表", type="primary", key="cmp_export"):
                try:
                    os.makedirs(_OUT_DIR, exist_ok=True)
                    _fn = f"比价结果_{_dt.now():%Y%m%d_%H%M%S}.xlsx"
                    _out = os.path.join(_OUT_DIR, _fn)
                    export_highlighted(_m, _cres["suppliers"], _out, min_fill=_fill, min_font=_font)
                    log_line(f"比价导出：{_out}")
                    with open(_out, "rb") as _fh:
                        st.download_button("⬇️ 下载比价表", _fh.read(), file_name=_fn,
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           key="cmp_dl")
                    st.success(f"已导出到项目内：data\\outputs\\{_fn}")
                except Exception as e:
                    log_exception("比价导出失败", e)
                    st.error(f"导出出错（已记日志）：{e}")

            with st.expander("📄 按模板整合输出（F17 · 套上级模板）", expanded=False):
                _tpl = st.file_uploader("上传模板 xlsx（预留表头/样式）", type=["xlsx"], key="cmp_tpl")
                if _tpl:
                    try:
                        _tsheets = list_sheets_from_bytes(_tpl.getvalue(), _tpl.name)
                    except Exception:
                        _tsheets = ["Sheet1"]
                    _c1, _c2, _c3 = st.columns([2, 2, 3])
                    with _c1:
                        _tsheet = st.selectbox("写入 Sheet", _tsheets, key="cmp_tpl_sheet")
                    with _c2:
                        _tcell = st.text_input("起始单元格", value="A3", key="cmp_tpl_cell")
                    with _c3:
                        _ttitle = st.text_input("标题（可选）", value="", key="cmp_tpl_title")
                    if st.button("生成模板文件", key="cmp_tpl_go"):
                        try:
                            _bytes, _info = fill_template(
                                _tpl.getvalue(), _m, sheet=_tsheet, start_cell=_tcell,
                                title=(_ttitle or None))
                            _fn = f"比价_模板输出_{_dt.now():%Y%m%d_%H%M%S}.xlsx"
                            with open(os.path.join(_OUT_DIR, _fn), "wb") as _fh:
                                _fh.write(_bytes)
                            st.download_button("⬇️ 下载模板输出", _bytes, file_name=_fn,
                                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                               key="cmp_tpl_dl")
                            st.success(f"已写入 {_info['sheet']} {_info['start']}~{_info['end']}"
                                       f"（{_info['rows']} 行 × {_info['cols']} 列）")
                        except Exception as e:
                            log_exception("模板输出失败", e)
                            st.error(f"模板输出出错：{e}")

            st.subheader("⑤ 采购建议")
            try:
                _adv = advise(_m, _cres["suppliers"], low_confidence=_cres.get("low_confidence"))
                for _t in _adv["text"]:
                    st.markdown(_t)
                st.dataframe(_adv["rows"], height=260, width="stretch")
                if _llm_client.enabled:
                    if st.button("🤖 用 LLM 生成建议（F09 · 额外消耗约 $0.002~0.005/次）", key="cmp_advllm"):
                        with st.spinner("LLM 分析中…"):
                            _r = advise_llm(_m, _cres["suppliers"], _llm_client, rule_advice=_adv)
                        if _r.get("error"):
                            st.error(f"LLM 建议失败：{_r['error']}")
                        else:
                            st.markdown("##### 🤖 LLM 采购建议")
                            st.markdown(_r["text"])
                            st.caption(f"消耗约 ${_r['cost_usd']:.4f}｜缓存命中：{_r.get('cached')}")
                            try:
                                _fn = f"采购建议_LLM_{_dt.now():%Y%m%d_%H%M%S}.txt"
                                with open(os.path.join(_OUT_DIR, _fn), "w", encoding="utf-8") as _f:
                                    _f.write(_r["text"])
                                st.caption(f"已存 data\\outputs\\{_fn}")
                            except Exception:
                                pass
                else:
                    st.caption("（侧栏启用 LLM 后，可一键用 LLM 生成建议）")
            except Exception as e:
                log_exception("采购建议失败", e)
                st.error(f"建议生成出错：{e}")
    else:
        st.caption("样例数据：tests/synth_quotes.py（4 家 × 格式变体 + 脏数据 + ground truth）")

elif mode == "多表补全":
    st.header("🧩 多表补全（按模板汇总多张表）")
    st.caption("你给模板（第 1 列=钥匙，其余=要补的列）+ 若干源表（各含钥匙列）；"
               "自动发现哪张源表能供哪个列 → 按钥匙填充 → **按置信度上色** → 表格下方备注。"
               "只产出新文件，不动原表。")

    _ROOT_DIR2 = os.path.dirname(os.path.abspath(__file__))
    _RAW_DIR2 = os.path.join(_ROOT_DIR2, "data", "raw_quotes")
    _OUT_DIR2 = os.path.join(_ROOT_DIR2, "data", "outputs")

    st.subheader("① 模板表")
    _tpl_src = file_input("tpl", "模板表")
    _tpl_df, _tk = None, None
    if _tpl_src:
        _tpl_df, _tpl_sheet, _tpl_hdr = load_table(_tpl_src, file_key("tpl", _tpl_src["name"], _tpl_src["path"]))
        if _tpl_df is not None and len(_tpl_df.columns):
            _tk = pick_columns(_tpl_df, "🔑 模板·钥匙列（一般第 1 列）", mode="single",
                               key=file_key("tplkey", _tpl_src["name"], _tpl_sheet, _tpl_hdr))
            _tk = _tk[0] if _tk else _tpl_df.columns[0]
            st.caption(f"模板：{len(_tpl_df)} 行 × {len(_tpl_df.columns)} 列；钥匙列 = **{_tk}**；"
                       f"待补列：{'、'.join(c for c in _tpl_df.columns if c != _tk)}")

    st.subheader("② 源表（可多张）")
    _src_kind = st.radio("源表来源", ["上传文件（可多选）", "从项目 raw_quotes 目录选"],
                         horizontal=True, key="mf_srckind")
    _src_files = []      # [(name, bytes_or_path, is_path)]
    if _src_kind.startswith("上传"):
        _ups2 = st.file_uploader("上传源表（xlsx/xls/csv，可多选）", type=UP_TYPES,
                                 accept_multiple_files=True, key="mf_up")
        _src_files = [(u.name, u.getvalue(), False) for u in (_ups2 or [])]
    else:
        _files2 = sorted(f for f in os.listdir(_RAW_DIR2)
                         if f.lower().endswith((".xlsx", ".xlsm", ".xls", ".csv"))) if os.path.isdir(_RAW_DIR2) else []
        _pick2 = st.multiselect("选择源表文件（可多选）", _files2, default=_files2, key="mf_pick")
        _src_files = [(f, os.path.join(_RAW_DIR2, f), True) for f in _pick2]

    _sources = []
    for _i, (_nm, _data, _is_path) in enumerate(_src_files):
        _s = ({"mode": "path", "path": _data, "bytes": open(_data, "rb").read(), "name": _nm}
              if _is_path else {"mode": "upload", "path": None, "bytes": _data, "name": _nm})
        with st.expander(f"源表 {_i + 1}：{_nm}", expanded=(len(_src_files) == 1)):
            _df2, _sh2, _hdr2 = load_table(_s, file_key("mf", _nm, _i))
            if _df2 is None or not len(_df2.columns):
                continue
            _kc = pick_columns(_df2, "🔑 该源表·钥匙列", mode="single",
                               key=file_key("mfkey", _nm, _sh2, _hdr2, _i))
            _kc = _kc[0] if _kc else _df2.columns[0]
            st.caption(f"钥匙列 = **{_kc}**｜可用列：{'、'.join(str(c) for c in _df2.columns if c != _kc)}")
            _sources.append({"name": _nm, "df": _df2, "key_col": _kc})

    if _tpl_df is not None and _tk and _sources:
        st.subheader("③ 列供给（自动发现，可改）")
        _thr2 = st.slider("列名匹配阈值（默认 70；越低越容易对上）", 50, 100, 70, key="mf_thr")
        _kmin2 = st.slider("钥匙最低相似度（低于此视为未匹配→留空）", 0, 60, 20, key="mf_kmin")
        from core.table_filler import discover_supply as _disc
        _sup = _disc([c for c in _tpl_df.columns if c != _tk], _sources, float(_thr2))
        _mapping2 = {}
        for _tcol in [c for c in _tpl_df.columns if c != _tk]:
            _cands = _sup.get(_tcol) or []
            _opts = ["不补"] + [f"{_c['name']}【{_c['col']}】({_c['how']},{_c['score']:.0f})"
                                for _c in _cands]
            _def = 1 if _cands else 0
            _pickc2 = st.selectbox(f"「{_tcol}」←", _opts, index=_def,
                                   key=file_key("mfmap", _tcol, _thr2))
            if _pickc2 != "不补":
                _idx2 = _opts.index(_pickc2) - 1
                _c2 = _cands[_idx2]
                _mapping2[_tcol] = (_c2["source"], _c2["col"])
        st.caption("颜色图例：" + "；".join([
            "无色=完全匹配(100%)", "浅黄=高置信(80–99%)", "浅灰=中置信(40–80%)",
            "浅蓝=低置信(20–40%)", "浅红底空格=未匹配"]))

        if st.button("生成补全表", type="primary", key="mf_go"):
            try:
                with st.spinner("填充中…"):
                    _res2 = fill_multi(_tpl_df, _tk, _sources, mapping=_mapping2,
                                       col_threshold=float(_thr2), key_min=float(_kmin2))
                st.session_state["mf_res"] = _res2
            except Exception as e:
                log_exception("多表补全失败", e)
                st.error(f"补全出错（已记日志）：{e}")

        _mfres = st.session_state.get("mf_res")
        if _mfres:
            st.subheader("④ 结果（预览见下方；下载件带颜色与备注）")
            _st2 = _mfres["stats"]
            st.caption(f"完全匹配 {_st2['完全匹配(100%)']}｜高置信 {_st2['高置信(80-99%)']}｜"
                       f"中置信 {_st2['中置信(40-80%)']}｜低置信 {_st2['低置信(20-40%)']}｜"
                       f"未匹配(留空) {_st2['未匹配(留空)']}｜有未补全格的行：{_st2['未补全行数']}")
            st.dataframe(_mfres["result"], height=420, width="stretch")
            try:
                os.makedirs(_OUT_DIR2, exist_ok=True)
                _fn2 = f"多表补全_{_dt.now():%Y%m%d_%H%M%S}.xlsx"
                _out2 = os.path.join(_OUT_DIR2, _fn2)
                export_filled(_mfres["result"], _mfres["confidence"], _out2, stats=_st2)
                with open(_out2, "rb") as _fh2:
                    st.download_button("⬇️ 下载补全表（带颜色与备注）", _fh2.read(), file_name=_fn2,
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                       key="mf_dl")
                st.success(f"已导出到项目内：data\\outputs\\{_fn2}")
            except Exception as e:
                log_exception("多表补全导出失败", e)
                st.error(f"导出出错（已记日志）：{e}")

elif mode == "表格美化":
    st.header("🎨 表格美化")
    st.caption("把任意 xlsx 一键美化：**表头深底白字 / 列宽自适应 / 冻结首行 / 自动筛选 / 数字格式**"
               "（金额两位小数、百分比、日期）；可选斑马纹、每行最低值高亮。")
    _bsrc = file_input("bt", "要美化的表格")
    if _bsrc:
        try:
            _bsheets = (get_sheet_names(_bsrc["path"]) if _bsrc["mode"] == "path"
                        else list_sheets_from_bytes(_bsrc["bytes"], _bsrc["name"]))
        except Exception:
            try:
                _bsheets = list_sheets_from_bytes(_bsrc["bytes"], _bsrc["name"])
            except Exception as e:
                st.error(f"读取 Sheet 失败：{e}")
                st.stop()
        _bsheet = st.selectbox("Sheet", _bsheets, key=file_key("bt_sheet", _bsrc["name"]))
        _bhdr = pick_header_row(_bsrc["bytes"], _bsrc["name"], _bsheet,
                                key=file_key("bt_hdr", _bsrc["name"], _bsheet))
        _c1, _c2, _c3 = st.columns(3)
        with _c1:
            _banded = st.checkbox("斑马纹（隔行浅底）", key="bt_banded")
        with _c2:
            _hmin = st.checkbox("每行最低值高亮（可选）", key="bt_hmin",
                                help="只在可比的数值列之间比（自动跳过序号/日期/时间/数量等）")
        with _c3:
            _inplace = st.checkbox("写回原 Sheet（先自动备份）", key="bt_inplace")
        if _inplace:
            st.caption("⚠️ 写回会**重写该文件**（openpyxl 可能丢图表/图片/批注等元素）；"
                       "已自动建 `ai副本<sheet>` 备份；建议先下载确认效果。")
        if st.button("开始美化", type="primary", key="bt_go"):
            _opts = {"banded": _banded, "highlight_min": _hmin, "in_place": bool(_inplace)}
            try:
                with st.spinner("美化中…"):
                    if _inplace and can_writeback(_bsrc):
                        _bk, _info = beautify_file_in_place(_bsrc["path"], _bsheet, _bhdr, _opts)
                        st.success(f"已就地美化并写回原文件；备份 Sheet：**{_bk}**"
                                   f"（与美化后的 {_bsheet} 同在工作簿内）")
                    else:
                        if _inplace:
                            st.warning("该文件不是 xlsx/xlsm，无法写回 → 已改为下载。")
                        _bytes, _info = beautify_bytes(_bsrc["bytes"], _bsrc["name"],
                                                       _bsheet, _bhdr, _opts)
                        _fn = f"美化_{os.path.splitext(_bsrc['name'])[0]}.xlsx"
                        st.download_button("⬇️ 下载美化后的文件", _bytes, file_name=_fn,
                                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                           key="bt_dl")
                        st.success(f"已生成新 Sheet「{_info['sheet']}」（原表未动，两 sheet 都在文件里）")
            except Exception as e:
                log_exception("表格美化失败", e)
                st.error(f"美化出错（已记日志）：{e}")
