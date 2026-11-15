"""Prompt text with {{variable}} placeholders."""

import re

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class MissingVariable(ValueError):
    def __init__(self, name: str):
        super().__init__(f"The prompt needs a value for '{name}'.")
        self.name = name


def variables_in(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER.findall(text)))


def render(text: str, variables: dict) -> str:
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in variables:
            raise MissingVariable(name)
        value = variables[name]
        return value if isinstance(value, str) else str(value)

    return PLACEHOLDER.sub(fill, text)
