"""Canonical exam categories used by the question bank."""

EXAMS = ["JEE", "NEET", "BOARDS"]


def normalize_exam(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return {"board": "BOARDS", "boards": "BOARDS", "jee": "JEE", "neet": "NEET"}.get(
        value.lower(), value or None)
