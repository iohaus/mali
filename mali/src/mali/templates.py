"""Deterministic question templates and answer normalization."""

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from hashlib import sha256
from itertools import product
from math import gcd

from mali.errors import InvalidTemplate

_MIN_VARIANT_COUNT = 8
_MAX_VARIANT_COUNT = 10_000
_MAX_QUESTION_LENGTH = 800
_QUESTION_MARKER = "?"
_META_PHRASES = ("as an ai", "markdown", "question_text")


class AnswerType(StrEnum):
    """The machine-gradable form expected from a learner."""

    INTEGER = "integer"
    FRACTION = "fraction"
    EXACT = "exact"
    CHOICE = "choice"


class ConstraintKind(StrEnum):
    """A relation that every generated parameter set must satisfy."""

    DISTINCT = "distinct"
    COPRIME = "coprime"
    GREATER = "greater"


@dataclass(frozen=True, slots=True)
class ParameterDomain:
    """A finite set of exact values for one question parameter."""

    name: str
    values: tuple[int | Fraction, ...]

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise InvalidTemplate("parameter names must be valid identifiers")
        if not self.values:
            raise InvalidTemplate("parameter domains must not be empty")
        if any(type(value) not in (int, Fraction) for value in self.values):
            raise InvalidTemplate(
                "parameter values must be exact integers or fractions"
            )


@dataclass(frozen=True, slots=True)
class Constraint:
    """A declarative condition on generated parameter values."""

    kind: ConstraintKind
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.names) < 2:
            raise InvalidTemplate("constraints need at least two parameter names")


@dataclass(frozen=True, slots=True)
class DisplayValue:
    """A named value that can be shown in a question."""

    name: str
    expression: str

    def __post_init__(self) -> None:
        if not self.name.isidentifier():
            raise InvalidTemplate("display names must be valid identifiers")


@dataclass(frozen=True, slots=True)
class QuestionInstance:
    """A deterministic question with its computed answer."""

    values: tuple[tuple[str, Fraction], ...]
    key: str
    text: str
    answer_type: AnswerType
    options: tuple[str, ...]
    plain_text_contains_key: bool


@dataclass(frozen=True, slots=True)
class RenderingVerdict:
    """The result of checking generated question prose."""

    accepted: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class QuestionTemplate:
    """A verified source of deterministic, machine-gradable questions."""

    parameters: tuple[ParameterDomain, ...]
    key_expression: str
    plain_template: str
    answer_type: AnswerType
    constraints: tuple[Constraint, ...] = ()
    display_values: tuple[DisplayValue, ...] = ()
    options: tuple[str, ...] = ()
    _variants: tuple[QuestionInstance, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _validate_template_shape(self)
        variants = tuple(self._build_variants())
        if not _MIN_VARIANT_COUNT <= len(variants) <= _MAX_VARIANT_COUNT:
            raise InvalidTemplate("template must provide between 8 and 10000 variants")
        for variant in variants:
            verdict = validate_rendering(variant, variant.text)
            if not verdict.accepted:
                raise InvalidTemplate(f"plain question is invalid: {verdict.reason}")
        object.__setattr__(self, "_variants", variants)

    def instance(self, seed: int) -> QuestionInstance:
        """Select one verified question deterministically from a supplied seed."""
        if type(seed) is not int:
            raise InvalidTemplate("question seed must be an integer")
        digest = sha256(str(seed).encode("utf-8")).digest()
        index = int.from_bytes(digest, byteorder="big") % len(self._variants)
        return self._variants[index]

    def _build_variants(self) -> tuple[QuestionInstance, ...]:
        names = tuple(parameter.name for parameter in self.parameters)
        variants: list[QuestionInstance] = []
        for raw_values in product(*(parameter.values for parameter in self.parameters)):
            values = {
                name: Fraction(value)
                for name, value in zip(names, raw_values, strict=True)
            }
            if not _constraints_hold(values, self.constraints):
                continue
            displays = dict(values)
            for display_value in self.display_values:
                displays[display_value.name] = _evaluate(
                    display_value.expression, displays
                )
            key_value = _evaluate(self.key_expression, displays)
            key, options = _answer_key(self.answer_type, key_value, self.options)
            text = _render(self.plain_template, displays, options)
            if text.count(_QUESTION_MARKER) != 1:
                raise InvalidTemplate(
                    "each rendered question must contain exactly one question mark"
                )
            variants.append(
                QuestionInstance(
                    values=tuple(sorted(displays.items())),
                    key=key,
                    text=text,
                    answer_type=self.answer_type,
                    options=options,
                    plain_text_contains_key=_contains_value(text, key),
                )
            )
        return tuple(variants)


def canonical_answer(answer_type: AnswerType, raw: object) -> str | None:
    """Normalize a learner response into a comparable exact value."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if answer_type is AnswerType.CHOICE:
        return value
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    if answer_type is AnswerType.INTEGER and parsed.denominator != 1:
        return None
    return _format_fraction(parsed)


def validate_rendering(instance: QuestionInstance, text: object) -> RenderingVerdict:
    """Check that generated question prose preserves its verified values."""
    if not isinstance(text, str):
        return RenderingVerdict(False, "question text must be text")
    if not 1 <= len(text) <= _MAX_QUESTION_LENGTH:
        return RenderingVerdict(False, "question text has an invalid length")
    if text.count(_QUESTION_MARKER) != 1:
        return RenderingVerdict(
            False, "question text must contain exactly one question mark"
        )
    lowered = text.lower()
    if any(phrase in lowered for phrase in _META_PHRASES):
        return RenderingVerdict(False, "question text contains meta language")
    if not instance.plain_text_contains_key and _contains_value(text, instance.key):
        return RenderingVerdict(False, "question text reveals the answer")
    missing = [
        name
        for name, value in instance.values
        if not _contains_value(text, _format_fraction(value))
    ]
    if missing:
        return RenderingVerdict(False, "question text omits required values")
    return RenderingVerdict(True, None)


def _validate_template_shape(template: QuestionTemplate) -> None:
    names = tuple(parameter.name for parameter in template.parameters)
    if len(set(names)) != len(names):
        raise InvalidTemplate("parameter names must be unique")
    display_names = tuple(value.name for value in template.display_values)
    if len(set(display_names)) != len(display_names) or set(names) & set(display_names):
        raise InvalidTemplate("display names must be unique")
    known_names = set(names) | set(display_names)
    for constraint in template.constraints:
        if set(constraint.names) - set(names):
            raise InvalidTemplate("constraints may only use parameters")
    _parse_expression(template.key_expression, known_names)
    for display_value in template.display_values:
        _parse_expression(display_value.expression, known_names)
    if template.answer_type is AnswerType.CHOICE:
        if len(template.options) < 2 or len(set(template.options)) != len(
            template.options
        ):
            raise InvalidTemplate("choice questions need distinct options")
    elif template.options:
        raise InvalidTemplate("only choice questions may declare options")


def _constraints_hold(
    values: dict[str, Fraction], constraints: tuple[Constraint, ...]
) -> bool:
    for constraint in constraints:
        selected = tuple(values[name] for name in constraint.names)
        if constraint.kind is ConstraintKind.DISTINCT and len(set(selected)) != len(
            selected
        ):
            return False
        if constraint.kind is ConstraintKind.GREATER and not all(
            left > right for left, right in zip(selected, selected[1:], strict=False)
        ):
            return False
        if constraint.kind is ConstraintKind.COPRIME:
            if any(value.denominator != 1 for value in selected):
                raise InvalidTemplate("coprime constraints require integer parameters")
            if gcd(*(abs(value.numerator) for value in selected)) != 1:
                return False
    return True


def _answer_key(
    answer_type: AnswerType, value: Fraction, options: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    if answer_type is AnswerType.INTEGER:
        if value.denominator != 1:
            raise InvalidTemplate("integer answers must evaluate to an integer")
        return str(value.numerator), ()
    if answer_type is AnswerType.CHOICE:
        if value.denominator != 1 or not 0 <= value.numerator < len(options):
            raise InvalidTemplate("choice answer index must select one option")
        return options[value.numerator], options
    return _format_fraction(value), ()


def _render(
    template: str, values: dict[str, Fraction], options: tuple[str, ...]
) -> str:
    rendered_values = {name: _format_fraction(value) for name, value in values.items()}
    rendered_values["options"] = " | ".join(options)
    try:
        return template.format(**rendered_values)
    except (KeyError, ValueError) as error:
        raise InvalidTemplate(
            "question template contains an invalid placeholder"
        ) from error


def _parse_expression(expression: str, names: set[str]) -> ast.Expression:
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise InvalidTemplate("expression is not valid arithmetic") from error
    for node in ast.walk(parsed):
        if isinstance(node, ast.Name) and node.id not in names:
            raise InvalidTemplate("expression references an unknown value")
        if not isinstance(
            node,
            (
                ast.Expression,
                ast.BinOp,
                ast.Name,
                ast.Load,
                ast.UnaryOp,
                ast.Constant,
                ast.Add,
                ast.Sub,
                ast.Mult,
                ast.Div,
                ast.Mod,
                ast.FloorDiv,
                ast.USub,
                ast.UAdd,
            ),
        ):
            raise InvalidTemplate(
                f"expression uses unsupported syntax: {type(node).__name__}"
            )
    return parsed


def _evaluate(expression: str, values: dict[str, Fraction]) -> Fraction:
    parsed = _parse_expression(expression, set(values))

    def visit(node: ast.expr) -> Fraction:
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return Fraction(node.value)
        if isinstance(node, ast.UnaryOp):
            operand = visit(node.operand)
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return operand
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.Div, ast.Mod, ast.FloorDiv)):
                if not right:
                    raise InvalidTemplate("expression divides by zero")
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Mod):
                    return left % right
                return Fraction(left // right)
        raise InvalidTemplate("expression could not be evaluated")

    return visit(parsed.body)


def _format_fraction(value: Fraction) -> str:
    return (
        str(value.numerator)
        if value.denominator == 1
        else f"{value.numerator}/{value.denominator}"
    )


def _contains_value(text: str, value: str) -> bool:
    start = 0
    while True:
        index = text.find(value, start)
        if index < 0:
            return False
        before = text[index - 1] if index else " "
        after_index = index + len(value)
        after = text[after_index] if after_index < len(text) else " "
        if not before.isdigit() and not after.isdigit():
            return True
        start = index + 1
