# -*- coding: utf-8 -*-
"""核心匹配引擎：钥匙列/补充列三轮瀑布匹配（补缺语义）。

流程（2026-09-13 修订：经验库改为**兜底**，不抢答）：
  钥匙精确 → 钥匙模糊 → 补充列辅助 → **经验库兜底** → 空白
  - 钥匙列精确命中（绝大多数行在此 100% 命中）：最优先、可放心用
  - 模糊/补充列命中：能填就填，来源标注【模糊/补充列】，建议复核
  - 经验库兜底：规则都没结果时才用（含"对上多条"时消歧）；来源标【经验库】
  - 所有方法都用不上 → 留空
  - 结果表「匹配轮次」标来源、「备注」写说明（相似度/需核实等）
精确模式只跑「钥匙精确」。

补缺语义：多条匹配表行对上同一条钥匙表行是正常的（多条合同补同一个组织信息），
钥匙行不占用、可重复使用；只有「一条匹配表行对上多条钥匙表行、无法确定补哪个值」
才需要甄别——先用补充列缩小范围，最终仍有多条时全部列出并标注「重复预警」。
"""
import datetime as _dt
import re

import numpy as np
import pandas as pd

from .excel_io import RATE_COL

LVL_EXPERIENCE = "经验库"
LVL_LLM = "LLM语义"
LVL_EXACT = "钥匙精确"
LVL_KEY_FUZZY = "钥匙模糊"
LVL_SUPP_FUZZY = "补充列辅助"
LVL_DUP = "重复预警"
LVL_UNMATCHED = "未匹配"
LVL_IGNORED = "人工忽略"

MAX_CAND = 10    # 每行最多保留多少候选（按相似度取前 N）——防"候选爆炸→结果表爆炸"

CLEAN_OPTIONS = [
    ("strip", "忽略前后空格"),
    ("ignore_case", "忽略大小写"),
    ("remove_invisible", "清除不可见字符"),
    ("numeric_normalize", "数字格式统一(1.0→1、去千分位)"),
    ("date_normalize", "日期格式统一(时间戳→日期)"),
]
DEFAULT_CLEAN = {k: True for k, _ in CLEAN_OPTIONS}

_INVISIBLE_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\ufeff\u00ad\x00-\x08\x0b\x0c\x0e-\x1f]"
)
_NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def clean_value(v, opts=None):
    """把单元格值清洗为用于匹配的标准字符串（输出结果仍用原始值，不经此函数）。"""
    opts = DEFAULT_CLEAN if opts is None else opts
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if opts.get("date_normalize") and isinstance(v, (_dt.datetime, _dt.date, pd.Timestamp)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    else:
        s = str(v)
    if opts.get("remove_invisible"):
        s = s.replace("\u00a0", " ").replace("\u3000", " ")
        s = _INVISIBLE_RE.sub("", s)
    if opts.get("strip"):
        s = s.strip()
    if opts.get("numeric_normalize"):
        s2 = (
            s.replace(",", "")
            .replace("，", "")
            .replace("¥", "")
            .replace("￥", "")
            .replace("$", "")
            .strip()
        )
        if _NUM_RE.fullmatch(s2):
            f = float(s2)
            if abs(f) < 1e15:
                if f.is_integer():
                    s = str(int(f))
                else:
                    s = f"{f:.6f}".rstrip("0").rstrip(".")
            else:
                s = s2
    if opts.get("ignore_case"):
        s = s.upper()
    return s


def blank_row_mask(df):
    """逐行标记是否整行全空（用于写回原文件时的行对位：空行跳过但不丢位）。"""
    return [
        all(pd.isna(v) or str(v).strip() == "" for v in row)
        for row in df.itertuples(index=False, name=None)
    ]


def drop_blank_rows(df):
    """去掉整行全空的行（Excel 常见尾部空行）。"""
    return df[[not b for b in blank_row_mask(df)]]


def _composite(values):
    if all(v == "" for v in values):
        return None
    return "\x1f".join(values)


def _extract_json(text):
    """从模型输出里抠出第一段 JSON 对象（容忍 ```json 包裹与前后解释）。"""
    import json as _json
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        return _json.loads(t[a:b + 1])
    except Exception:
        return None


def _original_records(df, cols=None):
    """原始单元格值（输出用，不清洗），NaN/空 → 空串；cols 为空时取全部列。"""
    cols = list(df.columns) if cols is None else list(cols)
    if not cols:
        return [[] for _ in range(len(df))]
    sub = df[cols].astype(object)
    sub = sub.where(pd.notna(sub), "")
    return sub.values.tolist()


def _fuzzy_sim_matrix(a_vals_2d, b_vals_2d, threshold, log, col_names):
    """多列模糊相似度：逐列 cdist 后取逐元素最小值；低于阈值记 0。

    a_vals_2d / b_vals_2d: [列 -> [该列逐行清洗值]]
    返回 float64 矩阵 (len(a), len(b))。
    """
    try:
        from rapidfuzz import fuzz, process
        use_rapidfuzz = True
    except ImportError:
        use_rapidfuzz = False
        log("提示：未安装 rapidfuzz，回退到标准库 difflib（大数据量时会较慢）")

    n, m = len(a_vals_2d[0]), len(b_vals_2d[0])
    if n * m > 200_000_000:
        log(f"提示：相似度矩阵较大（{n}×{m}），内存约 {n * m * 4 / 1e9:.1f}GB，请耐心等待")
    sim = np.full((n, m), 100.0, dtype=np.float32)
    for i, (a_col, b_col) in enumerate(zip(a_vals_2d, b_vals_2d)):
        if use_rapidfuzz:
            s = process.cdist(a_col, b_col, scorer=fuzz.ratio,
                              score_cutoff=threshold, workers=-1, dtype=np.float32)
        else:
            from difflib import SequenceMatcher
            if n * m > 2_000_000:
                raise ValueError("数据量较大且缺少 rapidfuzz，模糊匹配会很慢；"
                                 "请先 pip install rapidfuzz 后重试")
            s = np.zeros((n, m), dtype=np.float32)
            for x, av in enumerate(a_col):
                for y, bv in enumerate(b_col):
                    r = SequenceMatcher(None, av, bv).ratio() * 100.0
                    s[x, y] = r if r >= threshold else 0.0
        sim = np.minimum(sim, s)
        log(f"  第{i + 1}/{len(a_vals_2d)}列「{col_names[i]}」相似度计算完成")
    return sim


def run_match(match_df, key_df, match_keys, key_keys, match_supp, key_supp,
              fetch_fields, mode="fuzzy", threshold=80.0, clean_opts=None,
              fetch_alias=None, log=None, experience=None, llm=None,
              llm_threshold=0.6):
    """三轮瀑布匹配，返回 {'result': DataFrame, 'stats': 统计 dict}。

    参数：
    - match_df：匹配表（要补缺的表）；key_df：钥匙表（提供补缺数据的表）
    - match_keys / key_keys：钥匙列（两边顺序一一对应，1-2 列）
    - match_supp / key_supp：补充列（可选，两边顺序一一对应，可为空）
    - fetch_fields：钥匙表中要补进匹配表的列
    - mode：'fuzzy' 三轮瀑布（默认）/ 'exact' 仅第一轮
    - threshold：模糊阈值 0-100（第二轮、第三轮适用）
    - fetch_alias：{钥匙表原列名: 输出表头别名}
    - experience：ExperienceStore 实例（传入则启用「经验库兜底」/人工忽略）
    - llm：LLM 客户端（duck-typed：需 .enabled 与 .chat）；传入且启用则启用「LLM 语义兜底」
    - llm_threshold：LLM 置信度阈值（低于则视为未匹配）
    - log：日志回调 fn(msg)
    """
    log = log or (lambda msg: None)
    fetch_alias = fetch_alias or {}
    clean_opts = dict(DEFAULT_CLEAN, **(clean_opts or {}))

    # ---- 校验 ----
    if len(match_keys) != len(key_keys):
        raise ValueError(f"钥匙列两侧数量不一致：匹配表 {len(match_keys)} / 钥匙表 {len(key_keys)}")
    if not match_keys:
        raise ValueError("请至少选择 1 对钥匙列")
    if len(match_supp) != len(key_supp):
        raise ValueError(f"补充列两侧数量不一致：匹配表 {len(match_supp)} / 钥匙表 {len(key_supp)}")
    if not fetch_fields:
        raise ValueError("请至少选择 1 个要补进匹配表的列（查补列）")
    for df, cols, tag in (
        (match_df, list(match_keys) + list(match_supp), "匹配表"),
        (key_df, list(key_keys) + list(key_supp) + list(fetch_fields), "钥匙表"),
    ):
        not_found = [c for c in cols if c not in df.columns]
        if not_found:
            raise ValueError(f"{tag}中找不到字段：{not_found}")
    if mode == "fuzzy" and not (0 <= threshold <= 100):
        raise ValueError("模糊阈值需为 0-100 之间的数字")

    n_match, n_key = len(match_df), len(key_df)
    log(f"开始匹配：匹配表 {n_match} 行 × 钥匙表 {n_key} 行，"
        f"模式={'三轮瀑布(精确→钥匙模糊→补充列辅助)' if mode == 'fuzzy' else '仅精确'}")

    # ---- 清洗所有参与匹配的列 ----
    def clean_cols(df, cols):
        return {c: [clean_value(v, clean_opts) for v in df[c].tolist()] for c in cols}

    mk = clean_cols(match_df, match_keys)
    kk = clean_cols(key_df, key_keys)
    ms = clean_cols(match_df, match_supp) if match_supp else {}
    ks = clean_cols(key_df, key_supp) if key_supp else {}

    match_records = _original_records(match_df)
    key_records = _original_records(key_df, fetch_fields)

    # 钥匙列全空的行不参与匹配（避免空值互配）
    match_key_empty = [_composite([mk[c][i] for c in match_keys]) is None for i in range(n_match)]
    key_key_empty = [_composite([kk[c][i] for c in key_keys]) is None for i in range(n_key)]

    remaining = [i for i in range(n_match) if not match_key_empty[i]]
    avail = [j for j in range(n_key) if not key_key_empty[j]]  # 钥匙行不占用，全程可用
    matched = {}      # match_idx -> (轮次, [(key_idx, rate), ...] 单条)
    ambiguous = {}    # match_idx -> {key_idx: rate}（多候选，待下一轮甄别）
    ignored = {}      # match_idx -> True（经验库标记「无匹配项」）
    row_note = {}     # match_idx -> 备注文本（LLM 兜底用）

    # （经验库不在此处抢答——见流水线结束后的「经验库兜底」）

    def col_lists(pairs_m, pairs_k, rows_m, rows_k):
        """取子集列值：返回 (a_2d, b_2d, col_names)。"""
        a = [[pairs_m[c][i] for i in rows_m] for c in pairs_m]
        b = [[pairs_k[c][i] for i in rows_k] for c in pairs_k]
        return a, b, list(pairs_m.keys())

    def candidates_of(level_cols_m, level_cols_k, exact, rows_m, rows_k):
        """返回 ({m: [k...]}, {(m,k): rate}, {m: 省略候选数})。

        **每行最多保留 MAX_CAND 条候选（按相似度取前 N）**——这是"数据一多就卡"的根治点：
        低区分度钥匙（如长编号、通用项目名）会让每行命中成百上千条候选，
        旧实现把它们全塞进结果表（实测 5000 行 → 315 万行结果、6 秒+），
        现在只列前 N 条，其余记在备注里（"另有 N 条未列出"）。
        """
        cand = {}
        rate_map = {}
        omitted = {}
        if exact:
            idx = {}
            for j in rows_k:
                k = _composite([level_cols_k[c][j] for c in level_cols_k])
                if k is not None:
                    idx.setdefault(k, []).append(j)
            for i in rows_m:
                k = _composite([level_cols_m[c][i] for c in level_cols_m])
                js = idx.get(k, []) if k is not None else []
                if len(js) > MAX_CAND:
                    omitted[i] = len(js) - MAX_CAND
                    js = js[:MAX_CAND]
                cand[i] = js
        else:
            a, b, names = col_lists(level_cols_m, level_cols_k, rows_m, rows_k)
            sim = _fuzzy_sim_matrix(a, b, threshold, log, names)
            for x, i in enumerate(rows_m):
                row = sim[x]
                nz = np.nonzero(row)[0]
                if nz.size > MAX_CAND:
                    top = nz[np.argpartition(-row[nz], MAX_CAND - 1)[:MAX_CAND]]
                    top = top[np.argsort(-row[top])]
                    omitted[i] = int(nz.size - MAX_CAND)
                else:
                    top = nz
                cand[i] = [rows_k[y] for y in top]
                for y in top:
                    rate_map[(i, rows_k[y])] = float(row[y])
        return cand, rate_map, omitted

    # ---- 三轮瀑布 ----
    levels = [(LVL_EXACT, True)]
    if mode == "fuzzy":
        levels.append((LVL_KEY_FUZZY, False))
        if match_supp:
            levels.append((LVL_SUPP_FUZZY, False))

    final_level = levels[-1]
    dup_map = {}
    dup_omitted = {}      # 重复预警行：被省略的候选数（备注里写明）
    unmatched_reason = {}
    for level_name, exact in levels:
        if not remaining:
            break
        if level_name == LVL_SUPP_FUZZY:
            cols_m, cols_k = dict(mk, **ms), dict(kk, **ks)
        else:
            cols_m, cols_k = mk, kk
        log(f"第{levels.index((level_name, exact)) + 1}轮「{level_name}」："
            f"待配 {len(remaining)} 行 × 候选钥匙 {len(avail)} 行")
        cand, rates, omitted = candidates_of(cols_m, cols_k, exact, remaining, avail)
        still = []
        is_final = (level_name, exact) == final_level
        for i in remaining:
            cs = cand.get(i, [])
            if len(cs) == 1:
                j = cs[0]
                matched[i] = (level_name, [(j, 100.0 if exact else rates[(i, j)])])
            elif len(cs) >= 2:
                if is_final:
                    dup_map[i] = {j: (100.0 if exact else rates[(i, j)]) for j in cs}
                    if omitted.get(i):
                        dup_omitted[i] = omitted[i]
                else:
                    still.append(i)  # 多候选 → 下一轮用更多列甄别
            else:
                if is_final:
                    unmatched_reason[i] = (
                        "钥匙列为空" if match_key_empty[i] else "无达标候选"
                    )
                else:
                    still.append(i)
        if omitted:
            log(f"  候选过多：{len(omitted)} 行按相似度只保留前 {MAX_CAND} 条"
                f"（共省略 {sum(omitted.values())} 条，写入备注）")
        remaining = still

    # ---- 经验库兜底（规则之后）：无候选的补答案；多候选的消歧 ----
    if experience is not None:
        try:
            from .experience import IGNORE
            lut = experience.build_lookup(clean_opts)
        except Exception:
            lut = {}
        if lut:
            right_index = {}
            for j in avail:
                rc = _composite([kk[c][j] for c in key_keys])
                right_index.setdefault(rc, []).append(j)
            n_exp = 0
            # (a) 规则都没结果的行（末轮无候选 → 本会未匹配的）→ 库给答案 / 忽略
            pending = [i for i in range(n_match)
                       if i not in matched and i not in dup_map and i not in ignored
                       and not match_key_empty[i]]
            for i in pending:
                tgt = lut.get(_composite([mk[c][i] for c in match_keys]))
                if tgt is None:
                    continue
                if tgt == IGNORE:
                    ignored[i] = True
                    unmatched_reason.pop(i, None)
                    continue
                js = right_index.get(tgt, [])
                if len(js) == 1:
                    matched[i] = (LVL_EXPERIENCE, [(js[0], 100.0)])
                    unmatched_reason.pop(i, None)
                    n_exp += 1
            # (b) 重复预警（多候选）→ 库消歧：**直查钥匙表索引**（right_index），
            #     不依赖候选列表——这样"每行候选上限(MAX_CAND)"只影响展示条数，不影响自动命中
            for i in list(dup_map.keys()):
                tgt = lut.get(_composite([mk[c][i] for c in match_keys]))
                if tgt is None or tgt == IGNORE:
                    continue
                js = right_index.get(tgt, [])
                if len(js) == 1:
                    matched[i] = (LVL_EXPERIENCE, [(js[0], 100.0)])
                    n_exp += 1
                    del dup_map[i]
            log(f"经验库兜底：命中 {n_exp} 行，忽略 {len(ignored)} 行")

    # ---- LLM 语义兜底（可选，最后一级；只吃规则/经验库都没结果的尾部小批量）----
    if llm is not None and getattr(llm, "enabled", False) and avail:
        pending = [i for i in range(n_match)
                   if i not in matched and i not in dup_map and i not in ignored
                   and not match_key_empty[i]]
        if pending:
            cand_idx = avail[:100]
            cand_lines = []
            for j in cand_idx:
                kv = " | ".join(str(kk[c][j]) for c in key_keys)
                fvv = " | ".join(str(key_records[j][k]) for k in range(len(fetch_fields)))
                cand_lines.append(f"{j}: {kv}" + (f"  ->  {fvv}" if fvv.strip(" |") else ""))
            row_lines = []
            for i in pending:
                kv = " | ".join(str(mk[c][i]) for c in match_keys)
                sv = " | ".join(str(ms[c][i]) for c in match_supp) if match_supp else ""
                row_lines.append(f"{i}: {kv}" + (f"  [补充:{sv}]" if sv.strip(" |") else ""))
            sys_p = ("你是采购数据匹配助手。给定【待配行】与【候选钥匙行】，"
                     "为每个待配行选最匹配的候选钥匙行（允许语义等价，如 螺钉M4 ≈ 紧固件-螺钉M4×10）。"
                     '只输出 JSON：{"matches":[{"row":整数,"key":整数,"confidence":0~1,"reason":"简短理由"}]}；'
                     "没有合适候选则 key=-1。")
            usr = "【候选钥匙行】\n" + "\n".join(cand_lines) + "\n\n【待配行】\n" + "\n".join(row_lines)
            try:
                _r = llm.chat([{"role": "system", "content": sys_p},
                               {"role": "user", "content": usr}],
                              max_tokens=4000, temperature=0)
                data = _extract_json(_r.get("text") or "")
                n_llm = 0
                for mm in (data or {}).get("matches", []):
                    try:
                        ri, ki = int(mm.get("row")), int(mm.get("key"))
                        conf = float(mm.get("confidence", 0) or 0)
                    except Exception:
                        continue
                    if ri in pending and ki in avail and conf >= llm_threshold:
                        matched[ri] = (LVL_LLM, [(ki, round(conf * 100, 1))])
                        row_note[ri] = f"LLM兜底(置信度{conf:.2f})：{str(mm.get('reason', ''))[:60]}"
                        n_llm += 1
                log(f"LLM 语义兜底：命中 {n_llm} 行（待配 {len(pending)}）"
                    + (f"；注意：{_r.get('error')}" if _r.get("error") else ""))
            except Exception as e:
                log(f"LLM 语义兜底失败：{type(e).__name__}: {e}")

    # ---- 汇总统计 ----
    stats = {
        "匹配表总行数": n_match,
        "钥匙表总行数": n_key,
        LVL_EXPERIENCE: 0, LVL_LLM: 0, LVL_EXACT: 0, LVL_KEY_FUZZY: 0, LVL_SUPP_FUZZY: 0,
        LVL_DUP: 0, LVL_UNMATCHED: 0, LVL_IGNORED: 0,
    }
    for i, (lvl, _) in matched.items():
        stats[lvl] += 1
    stats[LVL_DUP] = len(dup_map)
    stats[LVL_IGNORED] = len(ignored)
    unmatched_set = [i for i in range(n_match)
                     if i not in matched and i not in dup_map and i not in ignored]
    stats[LVL_UNMATCHED] = len(unmatched_set)
    for i in unmatched_set:
        unmatched_reason.setdefault(
            i, "钥匙列为空" if match_key_empty[i] else "无达标候选")

    # ---- 逐行状态（供写回匹配表原 Sheet：key=输入匹配表行下标） ----
    row_status = {}
    for i in range(n_match):
        if i in matched:
            lvl, combos = matched[i]
            row_status[i] = {"status": lvl, "combos": combos, "reason": ""}
        elif i in ignored:
            row_status[i] = {"status": LVL_IGNORED, "combos": [],
                             "reason": "经验库已标记「无匹配项」"}
        elif i in dup_map:
            row_status[i] = {"status": LVL_DUP, "combos": list(dup_map[i].items()),
                             "reason": f"对上{len(dup_map[i])}条，请人工核实"}
        else:
            row_status[i] = {"status": LVL_UNMATCHED, "combos": [],
                             "reason": unmatched_reason.get(i, "")}

    # ---- 组装结果（按匹配表原行顺序） ----
    match_cols = list(match_df.columns)
    match_col_set = set(match_cols)
    pos_of = {c: k for k, c in enumerate(match_cols)}
    # 别名指向匹配表已有列（如空着的「事业部」列）→ 结果里直接把补缺值填进那一列
    # （就地更新语义），不再单列成「事业部(钥匙表)」；写回时也写到该列。
    to_existing = {}    # fetch 下标 -> 匹配表已有列名
    fetch_cols = []     # 需要新增的查补列
    fetch_idx_new = []  # 与 fetch_cols 一一对应的 fetch 下标
    for k, c in enumerate(fetch_fields):
        name = fetch_alias.get(c, c)
        if name in match_col_set and name not in to_existing.values():
            to_existing[k] = name
            continue
        nm = name
        if nm in fetch_cols or nm in match_col_set:
            nm = f"{nm}(钥匙表)"
        fetch_cols.append(nm)
        fetch_idx_new.append(k)

    def merged_rec(i, fv):
        rec = list(match_records[i])
        for k, col in to_existing.items():
            rec[pos_of[col]] = fv[k]
        return rec

    def _note(lvl, rate, i=None):
        if lvl == LVL_EXPERIENCE:
            return "经验库兜底命中（人工确认过，建议复核）"
        if lvl == LVL_LLM:
            return row_note.get(i, "LLM 语义兜底（建议复核）")
        if lvl == LVL_KEY_FUZZY:
            return f"模糊命中（相似度 {round(rate, 1)}，建议复核）"
        if lvl == LVL_SUPP_FUZZY:
            return f"补充列辅助（相似度 {round(rate, 1)}，建议复核）"
        if lvl == LVL_DUP:
            return "对上多条，需人工核实"
        if lvl == LVL_IGNORED:
            return "经验库已标记忽略"
        return ""

    rows = []
    for i in range(n_match):
        if i in matched:
            lvl, pairs = matched[i]
            for j, rate in pairs:
                fv = key_records[j]
                rows.append(merged_rec(i, fv) + [fv[k] for k in fetch_idx_new]
                            + [lvl, round(rate, 1), "", _note(lvl, rate, i)])
        elif i in dup_map:
            _dup_note = _note(LVL_DUP, 0)
            if dup_omitted.get(i):
                _dup_note += (f"；另有 {dup_omitted[i]} 条候选未列出"
                              f"（按相似度只列前 {MAX_CAND} 条）")
            for j, rate in dup_map[i].items():
                fv = key_records[j]
                rows.append(merged_rec(i, fv) + [fv[k] for k in fetch_idx_new]
                            + [LVL_DUP, round(rate, 1), "", _dup_note])
        elif i in ignored:
            rows.append(list(match_records[i]) + [""] * len(fetch_cols)
                        + [LVL_IGNORED, 0.0, "经验库已标记：无匹配项", _note(LVL_IGNORED, 0)])
        else:
            _r = unmatched_reason.get(i, "")
            rows.append(list(match_records[i]) + [""] * len(fetch_cols)
                        + [LVL_UNMATCHED, 0.0, _r, ("所有方法均无结果" if not _r else _r)])
    result = pd.DataFrame(
        rows,
        columns=match_cols + fetch_cols + ["匹配轮次", RATE_COL, "未匹配原因", "备注"],
    )
    log(f"匹配完成：经验库 {stats[LVL_EXPERIENCE]}，钥匙精确 {stats[LVL_EXACT]}，"
        f"钥匙模糊 {stats[LVL_KEY_FUZZY]}，补充列辅助 {stats[LVL_SUPP_FUZZY]}，"
        f"重复预警 {stats[LVL_DUP]}，人工忽略 {stats[LVL_IGNORED]}，未匹配 {stats[LVL_UNMATCHED]}")
    return {"result": result, "stats": stats,
            "row_status": row_status, "fetch_records": key_records}
