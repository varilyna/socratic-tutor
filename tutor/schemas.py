
from tutor import prompts


def object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
GOAL = object_schema({"id": TEXT, "name": TEXT, "source_id": TEXT, "criterion": TEXT})


def for_prompt(system):
    if system == prompts.TASK_SINGLE:
        return object_schema({k: TEXT for k in ("question", "expected", "hint", "contrast", "explanation")})
    if system == prompts.OUTLINE:
        return object_schema({"title": TEXT, "goals": {"type": "array", "items": GOAL, "minItems": 2, "maxItems": 4}})
    if system == prompts.ASSESS:
        return object_schema({"kind": {"type": "string", "enum": ["answer", "help", "frustrated", "off_topic", "stop", "unclear"]},
                              "score": {"type": ["integer", "null"], "enum": [0, 1, 2, None]},
                              "misconception": TEXT,
                              "confidence": {"type": "string", "enum": ["high", "low"]}})
    if system == prompts.CHECK:
        return object_schema({"ok": {"type": "boolean"}, "reason": TEXT})
    return None
