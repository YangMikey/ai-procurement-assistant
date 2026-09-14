# -*- coding: utf-8 -*-
"""匹配经验库：人工确认/修正的配对沉淀为本地别名对照，越用越快越准（PRD §6 五级流水线第①级 + ⑥回写）。

数据结构（data/experience.json）：
  {"pairs": [{"a": 左组合键, "b": 右组合键, "hits": 命中次数, "src": "manual|llm", "ts": ...}]}

组合键：多把钥匙列 → 各自清洗后用 COMPOSITE_SEP 连接（与 matcher.run_match 同口径）。
忽略标记：人工确认「本条没有可匹配项」→ b=IGNORE，下次直接跳过（不再进模糊/LLM 轮）。

定位（2026-09-13 修订：经验库改为**兜底**，不再抢答）：
- 兜底：**钥匙精确 → 模糊 → 补充列 → 经验库兜底 → 空白**；库只在规则都配不上时用（含"对上多条"时消歧）
- 回写：人工确认（含模糊项"仍要记住"）→ 入库；**同一左键覆盖**（后确认者赢，随时可改）
- 管理：查看 / 删除单条 / 清空（list_pairs / remove / clear）
- 全本地 JSON，零成本、零上传
"""
import json
import os
from datetime import datetime

from .matcher import clean_value

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "experience.json")

COMPOSITE_SEP = "\x1f"      # 组合键分隔符（与 matcher 一致）
IGNORE = "__IGNORE__"       # 人工确认「无可匹配项」的哨兵值


def norm_vals(vals, clean_opts=None, raw=False):
    """把 单值/列表 归一化为组合键字符串（各值清洗后连接）。

    raw=True：输入已是规范化好的组合键（如 aligner 的 key），原样返回、不再清洗
    （否则 clean_value 会把组合键分隔符 \\x1f 当不可见字符删掉，导致键对不上）。
    """
    if raw:
        if isinstance(vals, str):
            return vals
        return COMPOSITE_SEP.join("" if v is None else str(v) for v in vals)
    if vals is None or isinstance(vals, str):
        vals = [vals]
    return COMPOSITE_SEP.join(clean_value(v, clean_opts) for v in vals)


class ExperienceStore:
    def __init__(self, path=None):
        self.path = path or DEFAULT_PATH
        self.pairs = []
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.pairs = data.get("pairs", [])

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"pairs": self.pairs}, f, ensure_ascii=False, indent=2)

    # ---------- ① 兜底查询 ----------
    def build_lookup(self, clean_opts=None):
        """返回 {左组合键: 右组合键} 的精确命中字典（供流水线「经验库兜底」用）。"""
        table = {}
        for p in self.pairs:
            table.setdefault(p["a"], p["b"])
        return table

    def lookup_pair(self, left, right, clean_opts=None):
        """判断 (left, right) 是否为经验库已确认配对。"""
        a, b = norm_vals(left, clean_opts), norm_vals(right, clean_opts)
        for p in self.pairs:
            if p["a"] == a and p["b"] == b:
                return p
        return None

    # ---------- ⑥ 回写（覆盖语义：同一左键只保留一条） ----------
    def record(self, left, right=None, src="manual", clean_opts=None, ignore=False, raw=False):
        """记录/覆盖一条配对（**同一左键只保留一条：本次确认即覆盖旧值**）。

        left/right：单值或列表（多钥匙列传列表，自动组合）。
        ignore=True：记录「无可匹配项」（右值=IGNORE 哨兵），下次直接跳过。
        raw=True：左右已是规范化组合键（如 aligner 的 key），原样存。
        """
        a = norm_vals(left, clean_opts, raw=raw)
        if not a:
            return False
        b = IGNORE if ignore else norm_vals(right, clean_opts, raw=raw)
        if not b:
            return False
        if not ignore and a == b:
            return False  # 无信息量（恒等）
        now = datetime.now().isoformat(timespec="seconds")
        same_a = [p for p in self.pairs if p["a"] == a]
        # 命中同一条（a,b）→ 强化；否则覆盖（删掉同左键的旧记录，写入新值）
        hit = next((p for p in same_a if p["b"] == b), None)
        if hit is not None:
            hit["hits"] = hit.get("hits", 1) + 1
            hit["ts"] = now
            self.pairs = [p for p in self.pairs if p["a"] != a or p is hit]
        else:
            self.pairs = [p for p in self.pairs if p["a"] != a]
            self.pairs.append({"a": a, "b": b, "hits": 1, "src": src, "ts": now})
        self._save()
        return True

    # ---------- 管理：查看 / 删除单条 / 清空 ----------
    def list_pairs(self):
        return [dict(p) for p in self.pairs]

    def remove(self, left, clean_opts=None, raw=False):
        """按左键删除记录（撤销/改错的入口）。返回删除条数。"""
        a = norm_vals(left, clean_opts, raw=raw)
        before = len(self.pairs)
        self.pairs = [p for p in self.pairs if p["a"] != a]
        removed = before - len(self.pairs)
        if removed:
            self._save()
        return removed

    def clear(self):
        """清空全部经验（回到零）。"""
        self.pairs = []
        self._save()

    # ---------- 统计 ----------
    def stats(self):
        manual = sum(1 for p in self.pairs if p.get("src") == "manual")
        llm = sum(1 for p in self.pairs if p.get("src") == "llm")
        ign = sum(1 for p in self.pairs if p.get("b") == IGNORE)
        hits = sum(p.get("hits", 1) for p in self.pairs)
        return {"别名对总数": len(self.pairs), "人工确认": manual,
                "LLM沉淀": llm, "忽略标记": ign, "累计命中": hits}
