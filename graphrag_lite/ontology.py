"""Versioned, project-local ontology for typed evidence assertions."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def schema():
    data = json.loads(Path(__file__).with_name("ontology.json").read_text(encoding="utf-8"))
    types = data["types"]
    for name, relation in data["relations"].items():
        for role in ("subject", "object"):
            if not relation[role] or any(kind not in types for kind in relation[role]):
                raise ValueError(f"Invalid ontology relation {name}: {role}")
    return data


def validate(predicate, subject_type, object_type):
    """Unknown predicates and unspecified concept types remain valid in open-world mode."""
    definition = schema()["relations"].get(predicate)
    if not definition:
        return "unmapped"
    for role, actual in (("subject", subject_type), ("object", object_type)):
        actual = actual.strip().casefold() or "concept"
        if actual != "concept" and actual not in definition[role]:
            allowed = "、".join(definition[role])
            raise ValueError(f"本体约束：关系「{predicate}」的{('主体' if role == 'subject' else '客体')}类型应为 {allowed}，实际为 {actual}")
    return "typed" if subject_type != "concept" and object_type != "concept" else "partial"
