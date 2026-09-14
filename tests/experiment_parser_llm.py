# -*- coding: utf-8 -*-
"""M3c 非标解析测试：规则优先、非标才用 LLM（离线假客户端，不发真实请求）。

运行：py tests/experiment_parser_llm.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from openpyxl import Workbook

from core.parser_llm import parse_quote_auto

SAMPLES = os.path.join(_ROOT, "data", "samples")
TMP = os.path.join(os.environ["TEMP"], "opencode", "weird_quote.xlsx")
os.makedirs(os.path.dirname(TMP), exist_ok=True)

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.enabled = True

    def chat(self, messages, **kw):
        self.calls += 1
        return {"text": self.payload, "usage": {}, "cost_usd": 0.0,
                "cached": False, "error": None, "ms": 1}


# 造一个"规则认不出"的表：描述/采购价/增值税 等列名不含标准关键词
wb = Workbook()
ws = wb.active
ws.title = "报价"
ws.append(["料号", "描述", "计量单位", "需求数量", "采购价", "增值税"])
ws.append(["A001", "不锈钢螺丝M4", "个", 100, 1.13, "13%"])
ws.append(["A002", "PVC管Φ20", "米", 50, 3.62, "13%"])
wb.save(TMP)

# 1) 非标 → 走 LLM
fake = FakeLLM('{"header_row":1,"mapping":{"品名":2,"单位":3,"数量":4,'
               '"含税单价":5,"税率":6},"supplier":"测试供应商"}')
q, used = parse_quote_auto(path=TMP, llm=fake)
check("非标：触发 LLM 解析（调用 1 次）", used is True and fake.calls == 1)
check("非标：LLM 字段映射生效（品名）",
      "品名" in q[0]["df"].columns and q[0]["df"]["品名"].iloc[0] == "不锈钢螺丝M4")
check("非标：价格/数量被正确解析",
      abs(q[0]["df"]["含税单价"].iloc[0] - 1.13) < 1e-9 and q[0]["df"]["数量"].iloc[0] == 100)
check("非标：供应商取 LLM 结果", q[0]["supplier"] == "测试供应商")
check("非标：via=llm 标记", q[0]["via"] == "llm")

# 2) 标准件 → 规则优先，不调用 LLM
fake2 = FakeLLM('{"header_row":1,"mapping":{},"supplier":""}')
q2, used2 = parse_quote_auto(path=os.path.join(SAMPLES, "报价单A_标准格式.xlsx"), llm=fake2)
check("标准件：规则优先且零 LLM 调用", used2 is False and fake2.calls == 0)
check("标准件：via=rule", q2[0]["via"] == "rule")

# 3) LLM 未启用 → 退回规则 + 警告
fake3 = FakeLLM('{}')
fake3.enabled = False
q3, used3 = parse_quote_auto(path=TMP, llm=fake3)
check("LLM 未启用：退回规则并附警告",
      used3 is False and any("LLM 未启用" in w for w in q3[0]["warnings"]))

# 4) LLM 返回乱码 → 退回规则且不崩
fake4 = FakeLLM("抱歉，我无法解析")
q4, used4 = parse_quote_auto(path=TMP, llm=fake4)
check("LLM 返回不可解析：安全退回规则",
      used4 is False and any("JSON" in " ".join(q4[0]["warnings"]) for w in [1]))

os.remove(TMP)
print(f"\n===== M3c 非标解析测试通过：{ok} 项断言（离线，无真实请求）=====")
