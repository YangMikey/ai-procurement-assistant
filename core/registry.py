# -*- coding: utf-8 -*-
"""技能注册表：每个能力模块挂一张元数据卡，V1 的任务模式是预置组合，V2 的技能入口把组合权交给 LLM 意图路由。

设计原则（PRD §6）：
- 模块即技能：core 下每个函数带 SKILL_CARD 注册
- 卡片格式对齐 SKILL.md 思想（名字/描述/输入输出 schema），V2 意图路由直接读卡片做 function calling
- 新技能 = 新函数 + 一张卡，不碰已有功能
"""

REGISTRY = {}


def skill(name, desc, inputs, outputs, task_modes=()):
    """注册装饰器：把函数登记进 REGISTRY。"""

    def deco(fn):
        REGISTRY[name] = {
            "name": name,
            "desc": desc,
            "inputs": inputs,
            "outputs": outputs,
            "task_modes": list(task_modes),
            "fn": fn,
        }
        return fn

    return deco


def list_skills(task_mode=None):
    """列出技能卡；可按任务模式过滤（V1 UI 用）。"""
    cards = list(REGISTRY.values())
    if task_mode:
        cards = [c for c in cards if task_mode in c["task_modes"]]
    return cards


def run_skill(name, **kwargs):
    if name not in REGISTRY:
        raise KeyError(f"未注册的技能：{name}（可用：{list(REGISTRY)}）")
    return REGISTRY[name]["fn"](**kwargs)
