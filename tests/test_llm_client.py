# -*- coding: utf-8 -*-
"""M3 LLM 适配层测试（**离线**，注入假 HTTP，不发真实请求）。

覆盖：默认关不发请求 / 缓存命中 / 调用上限 / 自检判读（余额/无效key/正常）/ 日志留痕。
运行：py tests/test_llm_client.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core.llm_client import LLMClient

TMP = os.path.join(os.environ["TEMP"], "opencode")
os.makedirs(TMP, exist_ok=True)
CACHE = os.path.join(TMP, "llm_cache_test.json")
LOG = os.path.join(TMP, "llm_test.log")
for p in (CACHE, LOG):
    if os.path.exists(p):
        os.remove(p)

ok = 0


def check(name, cond):
    global ok
    assert cond, f"[FAIL] {name}"
    ok += 1
    print(f"[PASS] {name}")


def fake_post_factory(counter):
    def _post(url, headers, body):
        counter["n"] += 1
        assert "Authorization" in headers and headers["Authorization"].startswith("Bearer ")
        return 200, json.dumps({"choices": [{"message": {"content": "OK"}}],
                                "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    return _post


# 1) 默认关：不发请求
_c = {"n": 0}
c0 = LLMClient(provider="openai", api_key="x", enabled=False,
               cache_path=CACHE, log_path=LOG, cfg_path="__none__", post_fn=fake_post_factory(_c))
r0 = c0.chat([{"role": "user", "content": "hi"}])
check("默认关：不发请求且返回提示", _c["n"] == 0 and "未启用" in r0["error"])

# 2) 开启 + 缓存：第二次命中缓存，真实调用只 1 次
c1 = LLMClient(provider="openai", api_key="x", enabled=True,
               cache_path=CACHE, log_path=LOG, cfg_path="__none__", post_fn=fake_post_factory(_c))
r1 = c1.chat([{"role": "user", "content": "hello"}])
r2 = c1.chat([{"role": "user", "content": "hello"}])
check("开启：首次返回文本", r1["text"] == "OK" and r1["error"] is None)
check("缓存：第二次命中、不再真实调用", r2["cached"] is True and _c["n"] == 1)

# 3) 调用上限
c2 = LLMClient(provider="openai", api_key="x", enabled=True, max_calls=1,
               cache=False, cache_path=CACHE, log_path=LOG, cfg_path="__none__",
               post_fn=fake_post_factory(_c))
c2.chat([{"role": "user", "content": "a"}])
r3 = c2.chat([{"role": "user", "content": "b"}])
check("上限：超过 max_calls 被拦", "上限" in (r3["error"] or ""))

# 4) 无凭证
c3 = LLMClient(provider="openai", api_key=None, enabled=True,
               cache_path=CACHE, log_path=LOG, cfg_path="__none__", post_fn=fake_post_factory(_c))
c3._api_key = None
check("无凭证：available=False", c3.available() is False)

# 5) 自检判读（注入 http_get，不打网络）
c4 = LLMClient(provider="opencode-go", api_key="k", enabled=True,
               cache_path=CACHE, log_path=LOG, cfg_path="__none__")
c4.http_get = lambda url: (200, '{"data":[{"id":"m1"},{"id":"m2"}]}')
o1, m1 = c4.self_test()
check("自检：正常返回 ok", o1 is True and "连通正常" in m1)
c4.http_get = lambda url: (401, '{"error":{"type":"CreditsError","message":"Insufficient balance"}}')
o2, m2 = c4.self_test()
check("自检：余额不足被识别", o2 is False and "余额不足" in m2)
c4.http_get = lambda url: (401, '{"error":{"message":"Invalid API key."}}')
o3, m3 = c4.self_test()
check("自检：无效 key 被识别", o3 is False and "认证失败" in m3)

# 6) 日志留痕 + 不泄露 key
check("留痕：产生日志文件", os.path.exists(LOG) and os.path.getsize(LOG) > 0)
log_text = open(LOG, encoding="utf-8").read()
c4._api_key = "sk-SECRET-TEST-123"
check("安全：状态不含密钥明文", "sk-SECRET-TEST-123" not in json.dumps(c4.status()))
check("安全：日志不含密钥明文", "sk-SECRET-TEST-123" not in log_text)
check("状态：不含 key 字段", "key" not in c4.status())

os.remove(CACHE)
os.remove(LOG)
print(f"\n===== M3 LLM 适配层测试通过：{ok} 项断言（离线，无真实请求）=====")
