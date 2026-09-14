# -*- coding: utf-8 -*-
"""LLM 客户端（可插拔适配器）：OpenAI 兼容接口，供非标解析/语义兜底用。

设计（PRD 关键决策 3：同接口插拔、默认关、显式开关）：
- **厂商无关**：provider 预设 base_url；任意 OpenAI 兼容服务（opencode/深求/OpenAI/本地 Ollama…）
- **凭证**：优先显式/环境变量 → 否则读 `~/.local/share/opencode/auth.json`（**绝不打印 key**）
- **默认关**：`enabled=False` 时不发任何请求；开启才用
- **缓存**：相同请求按内容哈希缓存（`data/llm_cache.json`），重复调用零成本
- **留痕**：每次调用把 模型/耗时/用量 记 `logs/llm.log`（便于成本核查）
- **上限**：每次运行最多 max_calls 次（成本硬闸）
- 仅用标准库（urllib），不新增依赖

用法：
    c = LLMClient(provider="opencode-go", model="deepseek-v4-flash", enabled=True)
    r = c.chat([{"role": "user", "content": "..."}])
    r["text"] / r["error"] / r["cached"]
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ROOT, "data", "llm_config.json")
CACHE_PATH = os.path.join(_ROOT, "data", "llm_cache.json")
LLM_LOG = os.path.join(_ROOT, "logs", "llm.log")
AUTH_PATH = os.path.expanduser(r"~\.local\share\opencode\auth.json")
USER_AGENT = "opencode/1.0"

PROVIDER_PRESETS = {
    "opencode-go": {"base": "https://opencode.ai/zen/go/v1", "auth_key": "opencode-go"},
    "opencode":    {"base": "https://opencode.ai/zen/v1",    "auth_key": "opencode"},
    "deepseek":    {"base": "https://api.deepseek.com",      "auth_key": "deepseek"},
    "openai":      {"base": "https://api.openai.com/v1",     "auth_key": "openai"},
    "ollama":      {"base": "http://localhost:11434/v1",     "auth_key": None},
}
DEFAULT_MODEL = {
    "opencode-go": "deepseek-v4.1-flash", "opencode": "deepseek-v4-flash",
    "deepseek": "deepseek-chat", "openai": "gpt-4o-mini", "ollama": "qwen2.5:7b",
}
# 价格（每 1M tokens，USD）——用于估算消耗；来源：opencode Go 定价页
PRICING = {
    "deepseek-v4.1-flash": {"peak": {"in": 0.30, "out": 1.20, "cache": 0.006},
                            "off":  {"in": 0.15, "out": 0.60, "cache": 0.003}},
    "deepseek-v4-flash":   {"peak": {"in": 0.30, "out": 1.20, "cache": 0.006},
                            "off":  {"in": 0.15, "out": 0.60, "cache": 0.003}},
    "deepseek-v4-pro":     {"peak": {"in": 1.32, "out": 3.96, "cache": 0.044},
                            "off":  {"in": 0.66, "out": 1.98, "cache": 0.022}},
    "glm-5.3-flash":       {"flat": {"in": 0.15, "out": 0.50, "cache": 0.03}},
    "kimi-k3":             {"flat": {"in": 3.00, "out": 15.00, "cache": 0.30}},
}


def is_peak_utc(dt=None):
    """DeepSeek 高峰时段：周一~周五 01:00-04:00、06:00-10:00 UTC；其余（含周末）为平峰。"""
    d = dt or datetime.utcnow()
    return d.weekday() < 5 and (1 <= d.hour < 4 or 6 <= d.hour < 10)


def estimate_cost(model, usage, dt=None):
    """按 Go 定价估算本次消耗（USD）。usage 为 OpenAI 风格 usage 字典。"""
    if not usage:
        return 0.0
    p = PRICING.get(model)
    if not p:
        return 0.0
    tier = p.get("peak") if "peak" in p else p.get("flat")
    if "peak" in p:
        tier = p["peak"] if is_peak_utc(dt) else p["off"]
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    fresh = max(pt - cached, 0)
    return round(fresh / 1e6 * tier["in"] + cached / 1e6 * tier["cache"]
                 + ct / 1e6 * tier["out"], 6)


def load_config(path=None):
    p = path or CONFIG_PATH
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg, path=None):
    p = path or CONFIG_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _read_auth_key(provider):
    """从 auth.json 读取 provider 的 key（只返回，不打印）。"""
    if not os.path.exists(AUTH_PATH):
        return None
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        item = data.get(provider) or {}
        return item.get("key")
    except Exception:
        return None


class LLMClient:
    def __init__(self, provider=None, model=None, base_url=None, api_key=None,
                 enabled=None, timeout=60, max_calls=20, cache=True,
                 cache_path=None, log_path=None, cfg_path=None, post_fn=None):
        cfg = load_config(cfg_path)
        self.provider = provider or cfg.get("provider") or "opencode-go"
        preset = PROVIDER_PRESETS.get(self.provider, {})
        self.base_url = (base_url or cfg.get("base_url") or preset.get("base")
                         or "https://api.openai.com/v1")
        self.model = model or cfg.get("model") or DEFAULT_MODEL.get(self.provider, "")
        self.enabled = bool(cfg.get("enabled", False)) if enabled is None else bool(enabled)
        self.timeout = timeout
        self.max_calls = int(max_calls or 0)
        self.use_cache = cache
        self.cache_path = cache_path or CACHE_PATH
        self.log_path = log_path or LLM_LOG
        self._post = post_fn or self._http_post      # 便于测试注入
        self._calls = 0
        self.session_id = uuid.uuid4().hex          # Go 端要求 x-opencode-session（每会话稳定）
        key = api_key or os.environ.get("LLM_API_KEY")
        if not key and preset.get("auth_key"):
            key = _read_auth_key(preset["auth_key"])
        self._api_key = key

    # ---------- 状态 ----------
    def available(self):
        return bool(self._api_key) if self.provider != "ollama" else True

    def status(self):
        return {"provider": self.provider, "model": self.model, "enabled": self.enabled,
                "available": self.available(), "calls_this_run": self._calls,
                "max_calls": self.max_calls}

    # ---------- 缓存 ----------
    def _load_cache(self):
        if not self.use_cache or not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cache(self, cache):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def _key(model, messages, temperature, max_tokens):
        raw = json.dumps({"m": model, "msgs": messages, "t": temperature,
                          "mt": max_tokens}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # ---------- 日志 ----------
    def _log(self, **kw):
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                    "provider": self.provider, "model": self.model, **kw},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- HTTP ----------
    def _http_post(self, url, headers, body):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def http_get(self, url):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                   "x-opencode-session": self.session_id}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def self_test(self):
        """连通性自检：优先 GET /models，其次发一条极短消息。返回 (ok, message)。"""
        if self.provider != "ollama" and not self._api_key:
            return False, "未找到该 provider 的密钥（auth.json / 环境变量 LLM_API_KEY）"
        st, body = self.http_get(self.base_url.rstrip("/") + "/models")
        if st == 200:
            try:
                n = len(json.loads(body).get("data", []))
            except Exception:
                n = "?"
            return True, f"连通正常（/models 可用，模型数={n}）"
        msg = body[:160] if body else f"HTTP {st}"
        low = msg.lower()
        if "insufficient" in low or "balance" in low or "credits" in low:
            return False, "密钥可用但**余额不足**：" + msg
        if "auth" in low or "invalid" in low or st == 401 or st == 403:
            return False, "认证失败（key 无效/被拒）：" + msg
        return False, f"HTTP {st}：{msg}"

    # ---------- 主入口 ----------
    def chat(self, messages, model=None, temperature=0.0, max_tokens=None,
             use_cache=True, timeout=None):
        """返回 {text, usage, cached, error, ms}。任何失败都不抛异常，error 里说明。"""
        t0 = time.time()
        if not self.enabled:
            return {"text": "", "usage": None, "cached": False, "error": "LLM 未启用（默认关）", "ms": 0}
        if not self.available():
            return {"text": "", "usage": None, "cached": False, "error": "无可用凭证", "ms": 0}
        if self.max_calls and self._calls >= self.max_calls:
            return {"text": "", "usage": None, "cached": False,
                    "error": f"已达本次运行调用上限（{self.max_calls}）", "ms": 0}
        mdl = model or self.model
        ck = self._key(mdl, messages, temperature, max_tokens)
        if use_cache and self.use_cache:
            cache = self._load_cache()
            if ck in cache:
                e = cache[ck]
                self._log(cached=True, model=mdl, ms=0, cost_usd=0.0)
                return {"text": e.get("text", ""), "usage": e.get("usage"),
                        "cost_usd": e.get("cost", 0.0), "finish_reason": e.get("finish"),
                        "cached": True,
                        "error": (None if e.get("text") else "缓存内容为空"), "ms": 0}
        payload = {"model": mdl, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                   "Accept": "application/json", "x-opencode-session": self.session_id}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            st, body = self._post(self.base_url.rstrip("/") + "/chat/completions",
                                  headers, json.dumps(payload).encode("utf-8"))
            if st != 200:
                self._log(error=f"HTTP {st}", body=body[:200])
                return {"text": "", "usage": None, "cached": False,
                        "error": f"HTTP {st}: {body[:160]}", "ms": int((time.time() - t0) * 1000)}
            j = json.loads(body)
            ch = (j.get("choices") or [{}])[0]
            text = (ch.get("message") or {}).get("content") or ""
            fr = ch.get("finish_reason")
            usage = j.get("usage")
            cost = estimate_cost(mdl, usage)
            self._calls += 1
            err = None
            if not text:
                err = (f"模型未返回正文（finish={fr}；推理型模型可能把预算耗在 reasoning，"
                       f"请增大 max_tokens）")
            if use_cache and self.use_cache:
                cache = self._load_cache()
                cache[ck] = {"text": text, "usage": usage, "cost": cost, "finish": fr,
                             "ts": datetime.now().isoformat(timespec="seconds")}
                self._save_cache(cache)
            self._log(cached=False, model=mdl, usage=usage, cost_usd=cost,
                      finish=fr, peak=is_peak_utc(), ms=int((time.time() - t0) * 1000))
            return {"text": text, "usage": usage, "cost_usd": cost, "finish_reason": fr,
                    "cached": False, "error": err, "ms": int((time.time() - t0) * 1000)}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:160]
            self._log(error=f"HTTP {e.code}", body=body)
            return {"text": "", "usage": None, "cached": False,
                    "error": f"HTTP {e.code}: {body}", "ms": int((time.time() - t0) * 1000)}
        except Exception as e:
            self._log(error=f"{type(e).__name__}: {e}")
            return {"text": "", "usage": None, "cached": False,
                    "error": f"{type(e).__name__}: {e}", "ms": int((time.time() - t0) * 1000)}


def get_client(**kw):
    """按项目配置构造客户端（UI 用）。"""
    return LLMClient(**kw)
