
import copy
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from tutor import prompts
from tutor.material import normalize, validate_material
from tutor.provider import ModelError


class Client(Protocol):
    def complete(self, system: str, payload: dict, *, max_tokens: int = 1800) -> str: ...


def parse_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        if not raw.startswith("{") or not raw.endswith("}"):
            raise
        from json_repair import repair_json
        result = repair_json(raw, return_objects=True, skip_json_loads=True)
    if not isinstance(result, dict):
        raise ValueError("Нужен JSON-объект.")
    return result


def required_text(data: dict, name: str, limit: int = 1500) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Некорректное поле {name}.")
    return value.strip()


def question_similarity(left: str, right: str) -> float:
    a = set(re.findall(r"\w+", left.casefold()))
    b = set(re.findall(r"\w+", right.casefold()))
    return len(a & b) / len(a | b) if a or b else 1.0


def validate_plan(plan: dict, material: str) -> dict:
    validate_outline(plan, material)
    ids = {goal["id"] for goal in plan["goals"]}
    questions = set()
    for goal in plan["goals"]:
        tasks = goal.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 3:
            raise ValueError("Для навыка нужны три разных упражнения.")
        kinds = set()
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError("Некорректное упражнение.")
            for name in ("id", "kind", "question", "expected", "hint", "contrast", "explanation"):
                required_text(task, name)
            if task["id"] in ids:
                raise ValueError("Идентификаторы упражнений повторяются.")
            ids.add(task["id"])
            question = normalize(task["question"]).casefold()
            if question in questions or question.count("?") > 1:
                raise ValueError("Вопросы должны быть разными и содержать один основной вопрос.")
            questions.add(question)
            kinds.add(task["kind"])
        if kinds != {"diagnostic", "practice", "assessment"}:
            raise ValueError("Нужны diagnostic, practice и assessment.")
    return plan


def validate_evaluation(data: dict) -> dict:
    if data.get("kind") not in {"answer", "help", "frustrated", "off_topic", "stop", "unclear"}:
        raise ValueError("Некорректный тип сообщения.")
    score = data.get("score")
    if score is not None and (type(score) is not int or score not in (0, 1, 2)):
        raise ValueError("Некорректный балл.")
    if data.get("confidence") not in {"high", "low"}:
        raise ValueError("Нет оценки уверенности.")
    if not isinstance(data.get("misconception"), str):
        raise ValueError("Некорректное поле misconception.")
    if data["kind"] in {"off_topic", "unclear", "stop"} and score is not None:
        raise ValueError("Неучебное сообщение не должно получать балл.")
    return data


def structured(client: Client, system: str, payload: dict, validator, max_tokens=1800):
    for attempt in range(2):
        raw = client.complete(system, payload, max_tokens=max_tokens)
        try:
            return validator(parse_json(raw))
        except (ValueError, TypeError, KeyError) as exc:
            if attempt:
                raise ModelError("Модель дважды вернула некорректную структуру. Прогресс не изменен; повторите запрос.") from exc
            payload = {**payload, "format_error": str(exc),
                       "retry_instruction": "Повтори ответ в требуемой JSON-структуре, исправив указанную ошибку."}


def prepare(client: Client, material: str) -> dict:
    material = validate_material(material)
    fragments = source_fragments(material)
    def check_outline(data):
        for goal in data.get("goals", []):
            if not isinstance(goal, dict) or goal.get("source_id") not in fragments:
                raise ValueError("Укажи существующий source_id из source_fragments для каждого навыка.")
            goal["source_quote"] = fragments[goal["source_id"]]
        return validate_outline(data, material)
    plan = structured(client, prompts.OUTLINE, {"material": material, "source_fragments": fragments},
                      check_outline, max_tokens=2200)
    for goal in plan["goals"]:
        goal["tasks"] = []
        for kind in ("diagnostic", "practice", "assessment"):
            previous = [t["question"] for g in plan["goals"] for t in g.get("tasks", [])]
            def check_task(data):
                for field_name in ("question", "expected", "hint", "contrast", "explanation"):
                    required_text(data, field_name)
                if data["question"].count("?") > 1 or any(question_similarity(data["question"], q) > 0.8 for q in previous):
                    raise ValueError("Нужен другой конкретный пример, а не повтор или перефразировка предыдущего вопроса.")
                return data
            task = structured(client, prompts.TASK_SINGLE,
                              {"material": material, "goal": {k:v for k,v in goal.items() if k != "tasks"},
                               "kind": kind, "previous_questions": previous}, check_task, max_tokens=1000)
            task["id"], task["kind"] = goal["id"] + "_" + kind, kind
            goal["tasks"].append(task)
    return validate_plan(plan, material)


def source_fragments(material: str) -> dict:
    lines = material.splitlines()
    parts = []
    for i, line in enumerate(lines):
        part = line.strip()
        if len(part) >= 15:
            if part.endswith(":"):
                part = "\n".join(lines[i:i+3]).strip()
            parts.append(part)
    if len(parts) < 2:
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", material) if len(part.strip()) >= 15]
    if not parts:
        parts = [material]
    return {f"S{i}": part for i, part in enumerate(parts, 1)}


def validate_outline(data: dict, material: str) -> dict:
    required_text(data, "title", 120)
    goals = data.get("goals")
    if not isinstance(goals, list) or not 2 <= len(goals) <= 4:
        raise ValueError("Верни от 2 до 4 отдельных навыков; один общий навык не подходит.")
    ids = set()
    for goal in goals:
        if not isinstance(goal, dict):
            raise ValueError("Некорректный навык.")
        for name in ("id", "name", "criterion", "source_quote"):
            required_text(goal, name)
        if goal["id"] in ids:
            raise ValueError("Идентификаторы навыков должны быть разными.")
        ids.add(goal["id"])
        if len(normalize(goal["source_quote"])) < 15 or normalize(goal["source_quote"]) not in normalize(material):
            raise ValueError("Цитата навыка не найдена в исходном материале.")
    return data


@dataclass
class Session:
    material: str
    plan: dict
    goal_index: int = 0
    task: dict = field(default_factory=dict)
    help_level: int = 0
    failures: int = 0
    assisted: bool = False
    turns: int = 0
    finished: bool = False
    finish_reason: str = ""
    statuses: dict = field(default_factory=dict)
    used_tasks: list = field(default_factory=list)
    history: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    evidence: list = field(default_factory=list)

    @property
    def goal(self):
        return self.plan["goals"][self.goal_index]


def set_task(session: Session, task: dict) -> None:
    session.task = task
    session.help_level, session.failures, session.assisted = 0, 0, False
    if re.search(r"\bподсказка\s*:", task["question"], flags=re.IGNORECASE):
        session.assisted = True
    session.used_tasks.append(task["id"])


def start(material: str, plan: dict) -> Session:
    session = Session(material=material, plan=copy.deepcopy(plan))
    session.statuses = {g["id"]: "unknown" for g in plan["goals"]}
    set_task(session, next(t for t in session.goal["tasks"] if t["kind"] == "diagnostic"))
    session.history.append({"role": "assistant", "content":
        "Разберем материал на примерах. Попробуй объяснять ход мысли; если будет сложно, я помогу.\n\n" + session.task["question"]})
    return session


def summary(session: Session) -> str:
    labels = {"unknown": "пока не проверено", "needs_practice": "нужна практика",
              "demonstrated_with_help": "получилось с помощью",
              "demonstrated_independently": "подтверждено на новом примере без подсказки"}
    lines = ["Сессия завершена. Вот что показали твои ответы:"]
    for goal in session.plan["goals"]:
        gid = goal["id"]
        lines.append(f"• {goal['name']}: {labels[session.statuses[gid]]}.")
        records = [e for e in session.evidence if e["goal_id"] == gid and e["confidence"] == "high"]
        if records:
            record = records[-1]
            if record["evidence"]:
                lines.append(f"  Из твоего ответа: «{record['evidence'][:240]}».")
            if record["score"] in (0, 1) and record["misconception"]:
                lines.append(f"  Стоит разобрать: {record['misconception'][:240]}.")
    if session.finish_reason == "limit":
        lines.append("Достигнут предел длины сессии. Непроверенные темы остаются непроверенными.")
    lines.append("Это оценка по ответам в этой сессии, а не проверка долговременного запоминания.")
    return "\n\n".join(lines)


def finish(session: Session, reason: str = "user") -> Session:
    result = copy.deepcopy(session)
    if not result.finished:
        result.finished, result.finish_reason = True, reason
        result.history.append({"role": "assistant", "content": summary(result)})
    return result


def next_goal(session: Session) -> None:
    if session.goal_index + 1 == len(session.plan["goals"]):
        session.finished, session.finish_reason = True, "complete"
        return
    session.goal_index += 1
    set_task(session, next(t for t in session.goal["tasks"] if t["kind"] == "diagnostic"))


def choose_action(session: Session, evaluation: dict) -> str:
    kind, score = evaluation["kind"], evaluation["score"]
    gid = session.goal["id"]
    if kind == "stop":
        session.finished, session.finish_reason = True, "user"
        return "summary"
    if kind == "off_topic":
        return "redirect"
    if evaluation["confidence"] == "low" and kind == "answer" and score == 0 and evaluation["misconception"]:
        session.assisted = True
        session.help_level = min(3, session.help_level + 1)
        return help_action(session)
    if kind == "unclear" or (evaluation["confidence"] == "low" and kind not in {"help", "frustrated"}):
        return "clarify"
    if score is not None and evaluation["confidence"] == "high":
        session.evidence.append({**evaluation, "goal_id": gid, "task_id": session.task["id"],
                                 "assisted": session.assisted})
    if kind in {"help", "frustrated"}:
        if score in (0, 1) and evaluation["confidence"] == "high":
            session.statuses[gid] = "needs_practice"
        elif score == 2 and evaluation["confidence"] == "high":
            session.statuses[gid] = "demonstrated_with_help"
        session.assisted = True
        session.help_level = 3 if kind == "frustrated" else min(3, session.help_level + 1)
        return help_action(session)
    if score is None:
        return "clarify"
    if score == 2:
        if session.task["kind"] != "diagnostic" and not session.assisted:
            session.statuses[gid] = "demonstrated_independently"
            next_goal(session)
        else:
            session.statuses[gid] = "demonstrated_with_help" if session.assisted else "needs_practice"
            fresh = next((t for t in session.goal["tasks"] if t["kind"] == "assessment" and t["id"] not in session.used_tasks), None)
            fresh = fresh or next((t for t in session.goal["tasks"] if t["id"] not in session.used_tasks), None)
            if fresh:
                set_task(session, fresh)
            else:
                next_goal(session)
        return "summary" if session.finished else "new_task"
    session.statuses[gid] = "needs_practice"
    if score == 1 and session.failures == 0:
        session.failures += 1
        return "probe"
    session.failures += 1
    session.help_level = min(3, session.help_level + 1)
    session.assisted = True
    return help_action(session)


def help_action(session: Session) -> str:
    if session.help_level < 3:
        return "hint" if session.help_level == 1 else "contrast"
    fresh = next((t for t in session.goal["tasks"] if t["kind"] == "practice" and t["id"] not in session.used_tasks), None)
    if fresh:
        set_task(session, fresh)
    else:
        next_goal(session)
    return "summary" if session.finished else "explain"


def fallback(action: str, task: dict, next_task: dict, evaluation: dict) -> str:
    question = next_task["question"]
    if action == "hint":
        return task["hint"] + "\n\n" + task["question"]
    if action == "contrast":
        return task["contrast"] + "\n\n" + task["question"]
    if action == "explain":
        return "Давай разберем это вместе. " + task["explanation"] + "\n\n" + question
    if action == "probe":
        return "Поясни, пожалуйста, на каком признаке из материала основан твой ответ?"
    if action == "clarify":
        return "Я пока не уверен, как понять твою мысль. Можешь пояснить ответ на этот пример?"
    if action == "redirect":
        return "Вернемся к учебному примеру.\n\n" + question
    return "Проверим понимание на следующем примере.\n\n" + question


def turn(client: Client, original: Session, answer: str) -> Session:
    if original.finished:
        raise ValueError("Сессия завершена.")
    answer = answer.strip()
    if not answer or len(answer) > 4000:
        raise ValueError("Ответ должен содержать от 1 до 4000 символов.")
    old_task, old_goal = original.task, original.goal
    def check_evaluation(data):
        data["evidence"] = answer if data.get("score") is not None else ""
        return validate_evaluation(data)
    evaluation = structured(client, prompts.ASSESS, {
        "material": original.material, "criterion": old_goal["criterion"],
        "task": old_task, "student_answer": answer,
        "current_tutor_message": original.history[-1]["content"],
        "history": original.history[-8:], "support_used": original.assisted,
    }, check_evaluation)
    lowered = answer.casefold()
    if re.search(r"\bобъясни(?:те)?\b", lowered) and any(word in lowered for word in ("проще", "без вопросов", "нормально")):
        evaluation["kind"] = "frustrated"
        evaluation["intent_override"] = "explicit_explanation_request"
    session = copy.deepcopy(original)
    session.turns += 1
    session.history.append({"role": "user", "content": answer})
    action = choose_action(session, evaluation)
    if session.turns >= 20 and not session.finished:
        session.finished, session.finish_reason = True, "limit"
        action = "summary"
    used_fallback = False
    if session.finished:
        reply = summary(session)
    else:
        support = {"hint": old_task["hint"], "contrast": old_task["contrast"],
                   "explain": old_task["explanation"]}.get(action, "Новая информация не разрешена.")
        context = {
            "material": session.material, "criterion": old_goal["criterion"],
            "current_goal": old_goal["name"],
            "next_goal": {"name": session.goal["name"], "criterion": session.goal["criterion"]},
            "task": old_task, "next_task": session.task, "action": action,
            "allowed_support": support, "evaluation": evaluation,
            "student_answer": answer,
        }
        append_question = action in {"new_task", "explain", "hint", "contrast", "redirect"}
        context["question_will_be_appended"] = append_question
        reply = ""
        for attempt in range(2):
            candidate = client.complete(prompts.TEACH, context, max_tokens=600)
            if not candidate or len(candidate) > 900 or candidate.count("?") > 1:
                context["revision_reason"] = "Нужна краткая реплика с одним основным вопросом."
                continue
            if append_question and "?" in candidate:
                context["revision_reason"] = "Верни только комментарий без вопроса: код добавит исходную задачу целиком."
                continue
            if append_question:
                candidate = candidate + "\n\n" + session.task["question"]
            verdict = structured(client, prompts.CHECK, {**context, "candidate": candidate},
                                 validate_verdict, max_tokens=400)
            if verdict["ok"]:
                reply = candidate
                break
            context["revision_reason"] = verdict["reason"]
        if not reply:
            reply = fallback(action, old_task, session.task, evaluation)
            used_fallback = True
    session.history.append({"role": "assistant", "content": reply})
    session.trace.append({"turn": session.turns, "goal_id": old_goal["id"],
                          "task_id": old_task["id"], "evaluation": evaluation,
                          "action": action, "next_task_id": session.task["id"],
                          "help_level": original.help_level,
                          "next_help_level": session.help_level,
                          "fallback": used_fallback})
    return session


def validate_verdict(data: dict) -> dict:
    if type(data.get("ok")) is not bool or not isinstance(data.get("reason"), str):
        raise ValueError("Некорректный результат проверки реплики.")
    return data
