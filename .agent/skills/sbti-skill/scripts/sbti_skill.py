#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import textwrap
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_QUESTION_FILE = SKILL_ROOT / "sbti.md"
TYPE_ALIASES = {"FUCK": "FU?K"}
LETTER_MAP = {"A": 0, "B": 1, "C": 2, "D": 3}
QUESTION_HEADER_RE = re.compile(r"^\[(\d+)\]\s+([^\s]+)\s+·\s+(.+?)\s*$")
QUESTION_OPTION_RE = re.compile(r"^\s*([A-D])\.\s+(.*?)\s+\(value=(\d+)\)\s*$")
FALLBACK_DIM_META = {
    "S1": {"name": "S1 自尊自信", "model": "自我模型"},
    "S2": {"name": "S2 自我清晰度", "model": "自我模型"},
    "S3": {"name": "S3 核心价值", "model": "自我模型"},
    "E1": {"name": "E1 依恋安全感", "model": "情感模型"},
    "E2": {"name": "E2 情感投入度", "model": "情感模型"},
    "E3": {"name": "E3 边界与依赖", "model": "情感模型"},
    "A1": {"name": "A1 世界观倾向", "model": "态度模型"},
    "A2": {"name": "A2 规则与灵活度", "model": "态度模型"},
    "A3": {"name": "A3 人生意义感", "model": "态度模型"},
    "Ac1": {"name": "Ac1 动机导向", "model": "行动驱力模型"},
    "Ac2": {"name": "Ac2 决策风格", "model": "行动驱力模型"},
    "Ac3": {"name": "Ac3 执行模式", "model": "行动驱力模型"},
    "So1": {"name": "So1 社交主动性", "model": "社交模型"},
    "So2": {"name": "So2 人际边界感", "model": "社交模型"},
    "So3": {"name": "So3 表达与真实度", "model": "社交模型"},
}
FALLBACK_TYPE_MAP = {
    "CTRL": {"code": "CTRL", "cn": "拿捏者", "intro": "怎么样，被我拿捏了吧？", "desc": ""},
    "ATM-er": {"code": "ATM-er", "cn": "送钱者", "intro": "你以为我很有钱吗？", "desc": ""},
    "Dior-s": {"code": "Dior-s", "cn": "雕丝", "intro": "等着我逆袭。", "desc": ""},
    "BOSS": {"code": "BOSS", "cn": "领导者", "intro": "我来开。", "desc": ""},
    "THAN-K": {"code": "THAN-K", "cn": "感恩者", "intro": "我感谢苍天！我感谢大地！", "desc": ""},
    "OH-NO": {"code": "OH-NO", "cn": "哦不人", "intro": "哦不！", "desc": ""},
    "GOGO": {"code": "GOGO", "cn": "行人", "intro": "gogogo~出发咯", "desc": ""},
    "SEXY": {"code": "SEXY", "cn": "尤物", "intro": "您就是天生的尤物！", "desc": ""},
    "LOVE-R": {"code": "LOVE-R", "cn": "多情者", "intro": "爱意太满，现实显得有点贫瘠。", "desc": ""},
    "MUM": {"code": "MUM", "cn": "妈妈", "intro": "或许...我可以叫你妈妈吗....?", "desc": ""},
    "FAKE": {"code": "FAKE", "cn": "伪人", "intro": "已经，没有人类了。", "desc": ""},
    "OG8K": {"code": "OG8K", "cn": "无所谓人", "intro": "我说随便，是真的随便。", "desc": ""},
    "MALO": {"code": "MALO", "cn": "吗喽", "intro": "人生是个副本，而我只是一只吗喽。", "desc": ""},
    "JOKE-R": {"code": "JOKE-R", "cn": "小丑", "intro": "原来我们都是小丑。", "desc": ""},
    "WOC!": {"code": "WOC!", "cn": "握草人", "intro": "woc，我怎么是这个人格？", "desc": ""},
    "THIN-K": {"code": "THIN-K", "cn": "思考者", "intro": "已深度思考100s。", "desc": ""},
    "SHIT": {"code": "SHIT", "cn": "愤世者", "intro": "这个世界简直是shift。", "desc": ""},
    "ZZZZ": {"code": "ZZZZ", "cn": "装死者", "intro": "我没死，我只是在睡觉。", "desc": ""},
    "POOR": {"code": "POOR", "cn": "贫困者", "intro": "我穷，但我很专。", "desc": ""},
    "MONK": {"code": "MONK", "cn": "僧人", "intro": "没有那种世俗的欲望。", "desc": ""},
    "IMSB": {"code": "IMSB", "cn": "傻者", "intro": "认真的么？我真的是sb么？", "desc": ""},
    "SOLO": {"code": "SOLO", "cn": "孤儿", "intro": "我哭了，我怎么会是孤儿？", "desc": ""},
    "FU?K": {"code": "FU?K", "cn": "草者", "intro": "wtf?！这是什么人格？", "desc": ""},
    "DEAD": {"code": "DEAD", "cn": "死者", "intro": "我，还活着吗？", "desc": ""},
    "IMFW": {"code": "IMFW", "cn": "废物", "intro": "我真的...是废物吗？", "desc": ""},
    "HHHH": {"code": "HHHH", "cn": "傻乐者", "intro": "哈哈哈哈哈哈。", "desc": ""},
    "DRUNK": {"code": "DRUNK", "cn": "酒鬼", "intro": "烈酒烧喉，不得不醉。", "desc": ""},
}
FALLBACK_PATTERNS = [
    {"code": "CTRL", "pattern": "HHH-HMH-MHH-HHH-MHM"},
    {"code": "ATM-er", "pattern": "HHH-HHM-HHH-HMH-MHL"},
    {"code": "Dior-s", "pattern": "MHM-MMH-MHM-HMH-LHL"},
    {"code": "BOSS", "pattern": "HHH-HMH-MMH-HHH-LHL"},
    {"code": "THAN-K", "pattern": "MHM-HMM-HHM-MMH-MHL"},
    {"code": "OH-NO", "pattern": "HHL-LMH-LHH-HHM-LHL"},
    {"code": "GOGO", "pattern": "HHM-HMH-MMH-HHH-MHM"},
    {"code": "SEXY", "pattern": "HMH-HHL-HMM-HMM-HLH"},
    {"code": "LOVE-R", "pattern": "MLH-LHL-HLH-MLM-MLH"},
    {"code": "MUM", "pattern": "MMH-MHL-HMM-LMM-HLL"},
    {"code": "FAKE", "pattern": "HLM-MML-MLM-MLM-HLH"},
    {"code": "OG8K", "pattern": "MMH-MMM-HML-LMM-MML"},
    {"code": "MALO", "pattern": "MLH-MHM-MLH-MLH-LMH"},
    {"code": "JOKE-R", "pattern": "LLH-LHL-LML-LLL-MLM"},
    {"code": "WOC!", "pattern": "HHL-HMH-MMH-HHM-LHH"},
    {"code": "THIN-K", "pattern": "HHL-HMH-MLH-MHM-LHH"},
    {"code": "SHIT", "pattern": "HHL-HLH-LMM-HHM-LHH"},
    {"code": "ZZZZ", "pattern": "MHL-MLH-LML-MML-LHM"},
    {"code": "POOR", "pattern": "HHL-MLH-LMH-HHH-LHL"},
    {"code": "MONK", "pattern": "HHL-LLH-LLM-MML-LHM"},
    {"code": "IMSB", "pattern": "LLM-LMM-LLL-LLL-MLM"},
    {"code": "SOLO", "pattern": "LML-LLH-LHL-LML-LHM"},
    {"code": "FUCK", "pattern": "MLL-LHL-LLM-MLL-HLH"},
    {"code": "DEAD", "pattern": "LLL-LLM-LML-LLL-LHM"},
    {"code": "IMFW", "pattern": "LLH-LHL-LML-LLL-MLL"},
]
FALLBACK_DIM_DESCRIPTIONS = {
    "S1": {"L": "对自己下手比别人还狠，夸你两句你都想先验明真伪。", "M": "自信值随天气波动，顺风能飞，逆风先缩。", "H": "心里对自己大致有数，不太会被路人一句话打散。"},
    "S2": {"L": "内心频道雪花较多，常在“我是谁”里循环缓存。", "M": "平时还能认出自己，偶尔也会被情绪临时换号。", "H": "对自己的脾气、欲望和底线都算门儿清。"},
    "S3": {"L": "更在意舒服和安全，没必要天天给人生开冲刺模式。", "M": "想上进，也想躺会儿，价值排序经常内部开会。", "H": "很容易被目标、成长或某种重要信念推着往前。"},
    "E1": {"L": "感情里警报器灵敏，已读不回都能脑补到大结局。", "M": "一半信任，一半试探，感情里常在心里拉锯。", "H": "更愿意相信关系本身，不会被一点风吹草动吓散。"},
    "E2": {"L": "感情投入偏克制，心门不是没开，是门禁太严。", "M": "会投入，但会给自己留后手，不至于全盘梭哈。", "H": "一旦认定就容易认真，情绪和精力都给得很足。"},
    "E3": {"L": "容易黏人也容易被黏，关系里的温度感很重要。", "M": "亲密和独立都要一点，属于可调节型依赖。", "H": "空间感很重要，再爱也得留一块属于自己的地。"},
    "A1": {"L": "看世界自带防御滤镜，先怀疑，再靠近。", "M": "既不天真也不彻底阴谋论，观望是你的本能。", "H": "更愿意相信人性和善意，遇事不急着把世界判死刑。"},
    "A2": {"L": "规则能绕就绕，舒服和自由往往排在前面。", "M": "该守的时候守，该变通的时候也不死磕。", "H": "秩序感较强，能按流程来就不爱即兴炸场。"},
    "A3": {"L": "意义感偏低，容易觉得很多事都像在走过场。", "M": "偶尔有目标，偶尔也想摆烂，人生观处于半开机。", "H": "做事更有方向，知道自己大概要往哪边走。"},
    "Ac1": {"L": "做事先考虑别翻车，避险系统比野心更先启动。", "M": "有时想赢，有时只想别麻烦，动机比较混合。", "H": "更容易被成果、成长和推进感点燃。"},
    "Ac2": {"L": "做决定前容易多转几圈，脑内会议常常超时。", "M": "会想，但不至于想死机，属于正常犹豫。", "H": "拍板速度快，决定一下就不爱回头磨叽。"},
    "Ac3": {"L": "执行力和死线有深厚感情，越晚越像要觉醒。", "M": "能做，但状态看时机，偶尔稳偶尔摆。", "H": "推进欲比较强，事情不落地心里都像卡了根刺。"},
    "So1": {"L": "社交启动慢热，主动出击这事通常得攒半天气。", "M": "有人来就接，没人来也不硬凑，社交弹性一般。", "H": "更愿意主动打开场子，在人群里不太怕露头。"},
    "So2": {"L": "关系里更想亲近和融合，熟了就容易把人划进内圈。", "M": "既想亲近又想留缝，边界感看对象调节。", "H": "边界感偏强，靠太近会先本能性后退半步。"},
    "So3": {"L": "表达更直接，心里有啥基本不爱绕。", "M": "会看气氛说话，真实和体面通常各留一点。", "H": "对不同场景的自我切换更熟练，真实感会分层发放。"},
}
FALLBACK_DIM_ORDER = ["S1", "S2", "S3", "E1", "E2", "E3", "A1", "A2", "A3", "Ac1", "Ac2", "Ac3", "So1", "So2", "So3"]
FALLBACK_DRINK_TRIGGER_ID = "drink_gate_q2"


@dataclass(frozen=True)
class Dataset:
    dim_meta: Dict[str, Dict[str, str]]
    questions: List[Dict[str, Any]]
    special_questions: List[Dict[str, Any]]
    type_map: Dict[str, Dict[str, str]]
    patterns: List[Dict[str, str]]
    dim_descriptions: Dict[str, Dict[str, str]]
    dim_order: List[str]
    drink_trigger_id: str


class SBTISkillError(Exception):
    """Raised when the questionnaire data or answers are invalid."""


def _parse_questions_from_markdown(question_file: Path) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    content = Path(question_file).read_text(encoding="utf-8")
    regular_questions: List[Dict[str, Any]] = []
    special_questions: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    def flush_current() -> None:
        nonlocal current
        if not current:
            return

        text = "\n".join(current.pop("_text_lines", [])).strip()
        if not text or not current.get("options"):
            raise SBTISkillError(f"`{question_file.name}` 中题目 `{current.get('id', 'UNKNOWN')}` 格式不完整。")

        current["text"] = text
        if current.pop("special", False):
            special_questions.append(current)
        else:
            regular_questions.append(current)
        current = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        header_match = QUESTION_HEADER_RE.match(line.strip())
        if header_match:
            flush_current()
            _, question_id, dim = header_match.groups()
            current = {
                "id": question_id,
                "options": [],
                "_text_lines": [],
                "special": dim == "SPECIAL",
            }
            if dim != "SPECIAL":
                current["dim"] = dim
            continue

        if not current:
            continue

        option_match = QUESTION_OPTION_RE.match(line)
        if option_match:
            _, label, value = option_match.groups()
            current["options"].append({"label": label.strip(), "value": int(value)})
            continue

        stripped = line.strip()
        if stripped:
            current["_text_lines"].append(stripped)

    flush_current()
    return regular_questions, special_questions


@lru_cache(maxsize=1)
def load_dataset(question_file: Optional[Path] = DEFAULT_QUESTION_FILE) -> Dataset:
    if not question_file or not Path(question_file).exists():
        raise SBTISkillError("未找到题库来源：请提供 `sbti.md` 文件。")

    questions, special_questions = _parse_questions_from_markdown(Path(question_file))

    return Dataset(
        dim_meta=FALLBACK_DIM_META,
        questions=questions,
        special_questions=special_questions,
        type_map=FALLBACK_TYPE_MAP,
        patterns=FALLBACK_PATTERNS,
        dim_descriptions=FALLBACK_DIM_DESCRIPTIONS,
        dim_order=FALLBACK_DIM_ORDER,
        drink_trigger_id=FALLBACK_DRINK_TRIGGER_ID,
    )


def _special_question_map(dataset: Dataset) -> Dict[str, Dict[str, Any]]:
    return {question["id"]: question for question in dataset.special_questions}


def _all_question_map(dataset: Dataset) -> Dict[str, Dict[str, Any]]:
    merged = {question["id"]: question for question in dataset.questions}
    merged.update(_special_question_map(dataset))
    return merged


def _active_questions(dataset: Dataset, answers: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    answers = answers or {}
    questions = list(dataset.questions)
    special_map = _special_question_map(dataset)

    drink_gate_q1 = special_map.get("drink_gate_q1")
    drink_gate_q2 = special_map.get("drink_gate_q2")

    if drink_gate_q1:
        questions.append(drink_gate_q1)
    if drink_gate_q2 and answers.get("drink_gate_q1") == 3:
        questions.append(drink_gate_q2)

    return questions


def _level_from_score(score: int) -> str:
    if score <= 3:
        return "L"
    if score == 4:
        return "M"
    return "H"


def _level_to_number(level: str) -> int:
    return {"L": 1, "M": 2, "H": 3}[level]


def _pattern_to_levels(pattern: str) -> List[str]:
    return list(pattern.replace("-", ""))


def _resolve_type_info(dataset: Dataset, code: str) -> Dict[str, Any]:
    if code in dataset.type_map:
        return dict(dataset.type_map[code])

    alias = TYPE_ALIASES.get(code)
    if alias and alias in dataset.type_map:
        info = dict(dataset.type_map[alias])
        info.setdefault("raw_code", alias)
        return info

    return {
        "code": code,
        "cn": code,
        "intro": "",
        "desc": "",
    }


def _normalize_single_answer(question: Dict[str, Any], raw_answer: Any) -> int:
    if isinstance(raw_answer, bool):
        raise SBTISkillError(f"题目 {question['id']} 的答案不能是布尔值。")

    if isinstance(raw_answer, int):
        valid_values = {option["value"] for option in question["options"]}
        if raw_answer in valid_values:
            return raw_answer

    text = str(raw_answer).strip().upper()
    if not text:
        raise SBTISkillError(f"题目 {question['id']} 的答案为空。")

    if text in LETTER_MAP:
        index = LETTER_MAP[text]
        options = question["options"]
        if index >= len(options):
            raise SBTISkillError(f"题目 {question['id']} 没有选项 {text}。")
        return int(options[index]["value"])

    if text.isdigit():
        value = int(text)
        valid_values = {option["value"] for option in question["options"]}
        if value in valid_values:
            return value

    raise SBTISkillError(
        f"无法识别题目 {question['id']} 的答案 {raw_answer!r}。请使用 A/B/C/D 或题目对应的数值。"
    )


def normalize_answers(dataset: Dataset, answers: Mapping[str, Any]) -> Dict[str, int]:
    question_map = _all_question_map(dataset)
    normalized: Dict[str, int] = {}

    for question_id, raw_answer in answers.items():
        if question_id not in question_map:
            raise SBTISkillError(f"未知题目 ID: {question_id}")
        normalized[question_id] = _normalize_single_answer(question_map[question_id], raw_answer)

    required_ids = [question["id"] for question in dataset.questions]
    required_ids.append("drink_gate_q1")

    missing = [question_id for question_id in required_ids if question_id not in normalized]
    if missing:
        raise SBTISkillError(f"缺少必答题答案: {', '.join(missing)}")

    if normalized.get("drink_gate_q1") == 3 and "drink_gate_q2" not in normalized:
        raise SBTISkillError("当 `drink_gate_q1=3` 时，必须继续回答 `drink_gate_q2`。")

    return normalized


def score_answers(
    answers: Mapping[str, Any],
    question_file: Optional[Path] = DEFAULT_QUESTION_FILE,
) -> Dict[str, Any]:
    dataset = load_dataset(Path(question_file) if question_file else None)
    normalized = normalize_answers(dataset, answers)

    raw_scores = {dim: 0 for dim in dataset.dim_meta}
    for question in dataset.questions:
        raw_scores[question["dim"]] += int(normalized.get(question["id"], 0))

    levels = {dim: _level_from_score(score) for dim, score in raw_scores.items()}
    actual_vector = [_level_to_number(levels[dim]) for dim in dataset.dim_order]

    ranked: List[Dict[str, Any]] = []
    for pattern in dataset.patterns:
        target_levels = _pattern_to_levels(pattern["pattern"])
        target_vector = [_level_to_number(level) for level in target_levels]
        distance = sum(abs(left - right) for left, right in zip(actual_vector, target_vector))
        exact = sum(left == right for left, right in zip(actual_vector, target_vector))
        similarity = max(0, round((1 - distance / 30) * 100))

        type_info = _resolve_type_info(dataset, pattern["code"])
        ranked.append(
            {
                **pattern,
                **type_info,
                "distance": distance,
                "exact": exact,
                "similarity": similarity,
            }
        )

    ranked.sort(key=lambda item: (item["distance"], -item["exact"], -item["similarity"]))
    best_normal = ranked[0]

    drink_override = normalized.get(dataset.drink_trigger_id) == 2
    special = False
    secondary_type = None

    if drink_override:
        final_type = _resolve_type_info(dataset, "DRUNK")
        mode_kicker = "隐藏人格已激活"
        badge = "匹配度 100% · 酒精异常因子已接管"
        sub = "乙醇亲和性过强，系统已直接跳过常规人格审判。"
        special = True
        secondary_type = best_normal
    elif best_normal["similarity"] < 60:
        final_type = _resolve_type_info(dataset, "HHHH")
        mode_kicker = "系统强制兜底"
        badge = f"标准人格库最高匹配仅 {best_normal['similarity']}%"
        sub = "标准人格库对你的脑回路集体罢工了，于是系统把你强制分配给了 HHHH。"
        special = True
    else:
        final_type = best_normal
        mode_kicker = "你的主类型"
        badge = f"匹配度 {best_normal['similarity']}% · 精准命中 {best_normal['exact']}/15 维"
        sub = "维度命中度较高，当前结果可视为你的第一人格画像。"

    top3 = [
        {
            "code": item.get("code", "UNKNOWN"),
            "cn": item.get("cn", ""),
            "similarity": item["similarity"],
            "exact": item["exact"],
            "pattern": item["pattern"],
        }
        for item in ranked[:3]
    ]

    dim_summaries = {
        dim: {
            "name": dataset.dim_meta[dim]["name"],
            "model": dataset.dim_meta[dim]["model"],
            "score": raw_scores[dim],
            "level": levels[dim],
            "summary": dataset.dim_descriptions[dim][levels[dim]],
        }
        for dim in dataset.dim_order
    }

    return {
        "answers": normalized,
        "rawScores": raw_scores,
        "levels": levels,
        "dimSummaries": dim_summaries,
        "ranked": ranked,
        "bestNormal": best_normal,
        "finalType": final_type,
        "modeKicker": mode_kicker,
        "badge": badge,
        "sub": sub,
        "special": special,
        "secondaryType": secondary_type,
        "top3": top3,
    }


def _format_question(question: Dict[str, Any], index: int) -> str:
    lines = [f"[{index}] {question['id']} · {question.get('dim', 'SPECIAL')}", question["text"]]
    for option_index, option in enumerate(question["options"]):
        letter = chr(ord("A") + option_index)
        lines.append(f"  {letter}. {option['label']} (value={option['value']})")
    return "\n".join(lines)


def dump_questions(
    fmt: str = "markdown",
    question_file: Optional[Path] = DEFAULT_QUESTION_FILE,
) -> str:
    dataset = load_dataset(Path(question_file) if question_file else None)
    questions = _active_questions(dataset, {"drink_gate_q1": 3})

    if fmt == "json":
        return json.dumps(questions, ensure_ascii=False, indent=2)

    if question_file and Path(question_file).exists():
        return Path(question_file).read_text(encoding="utf-8").strip()

    parts = [
        "# SBTI 题库",
        "",
        "说明：`drink_gate_q2` 只有在 `drink_gate_q1=3`（选择饮酒）时才需要回答。",
        "",
    ]
    for index, question in enumerate(questions, start=1):
        parts.append(_format_question(question, index))
        parts.append("")
    return "\n".join(parts).strip()


def render_report(result: Mapping[str, Any]) -> str:
    final_type = result["finalType"]
    lines = [
        f"主类型：{final_type.get('code', 'UNKNOWN')}（{final_type.get('cn', '未知')}）",
        f"判定：{result['modeKicker']}",
        f"说明：{result['badge']}",
        f"补充：{result['sub']}",
        "",
        "Top 3 匹配：",
    ]

    for item in result["top3"]:
        cn = f"（{item['cn']}）" if item.get("cn") else ""
        lines.append(
            f"- {item['code']}{cn} · {item['similarity']}% · 精准命中 {item['exact']}/15 · {item['pattern']}"
        )

    lines.extend(["", "15 维结果："])
    for dim, info in result["dimSummaries"].items():
        lines.append(
            f"- {dim} {info['name']}: {info['level']} / {info['score']}分 · {info['summary']}"
        )

    description = final_type.get("desc")
    if description:
        lines.extend(["", "人格描述：", textwrap.fill(description, width=88)])

    return "\n".join(lines)


def _prompt_for_answer(question: Dict[str, Any], index: int) -> int:
    print()
    print(_format_question(question, index))
    while True:
        choice = input("请选择 A/B/C/D（或直接输入数值）: ").strip()
        try:
            return _normalize_single_answer(question, choice)
        except SBTISkillError as exc:
            print(f"输入无效：{exc}")


def run_interactive(
    question_file: Optional[Path] = DEFAULT_QUESTION_FILE,
) -> Dict[str, Any]:
    dataset = load_dataset(Path(question_file) if question_file else None)
    answers: Dict[str, int] = {}

    index = 1
    for question in dataset.questions:
        answers[question["id"]] = _prompt_for_answer(question, index)
        index += 1

    special_map = _special_question_map(dataset)
    drink_q1 = special_map.get("drink_gate_q1")
    if drink_q1:
        answers[drink_q1["id"]] = _prompt_for_answer(drink_q1, index)
        index += 1

    if answers.get("drink_gate_q1") == 3:
        drink_q2 = special_map.get("drink_gate_q2")
        if drink_q2:
            answers[drink_q2["id"]] = _prompt_for_answer(drink_q2, index)

    return score_answers(answers, question_file=question_file)


def _load_answers_from_args(raw_json: Optional[str], answers_file: Optional[str]) -> Dict[str, Any]:
    if raw_json and answers_file:
        raise SBTISkillError("`--answers` 和 `--answers-file` 只能二选一。")

    if raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise SBTISkillError(f"`--answers` 不是合法 JSON：{exc}") from exc
    elif answers_file:
        payload = json.loads(Path(answers_file).read_text(encoding="utf-8"))
    else:
        raise SBTISkillError("请提供 `--answers` 或 `--answers-file`。")

    if not isinstance(payload, dict):
        raise SBTISkillError("答案必须是 JSON 对象，例如 {\"q1\": \"A\"}。")

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SBTI 题目评分工具")
    parser.add_argument("--question-file", default=str(DEFAULT_QUESTION_FILE), help="题库 Markdown 路径，默认读取仓库里的 `sbti.md`")

    subparsers = parser.add_subparsers(dest="command")

    dump_parser = subparsers.add_parser("dump", help="导出题库")
    dump_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")

    score_parser = subparsers.add_parser("score", help="根据答案计算结果")
    score_parser.add_argument("--answers", help="内联 JSON 答案，例如 '{\"q1\": \"A\"}'")
    score_parser.add_argument("--answers-file", help="答案 JSON 文件路径")
    score_parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")

    subparsers.add_parser("interactive", help="交互式答题")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    question_file = Path(args.question_file) if args.question_file else None

    try:
        command = args.command or "interactive"

        if command == "dump":
            print(dump_questions(fmt=args.format, question_file=question_file))
            return

        if command == "score":
            answers = _load_answers_from_args(args.answers, args.answers_file)
            result = score_answers(answers, question_file=question_file)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(render_report(result))
            return

        result = run_interactive(question_file=question_file)
        print()
        print(render_report(result))
    except SBTISkillError as exc:
        parser.exit(status=1, message=f"错误：{exc}\n")


if __name__ == "__main__":
    main()
