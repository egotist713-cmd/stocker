import copy

from pydantic import BaseModel


def strict_json_schema(model: type[BaseModel], constraints: dict[str, dict] | None = None) -> dict:
    """
    JSON schema для strict structured output, построенная из Pydantic-модели.

    Сама модель не меняется. В схеме запроса все поля помечены обязательными,
    а лишние запрещены: у наших моделей у всех полей есть значения по умолчанию,
    поэтому без этого пустой {} формально валиден.

    constraints — дополнительные ограничения для полей верхнего уровня, которые
    действуют только на ответ модели (например, minItems для keywords).
    """
    schema = copy.deepcopy(model.model_json_schema())

    def make_strict(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)

    for field, extra in (constraints or {}).items():
        schema["properties"][field].update(extra)

    return schema


def json_schema_response_format(name: str, schema: dict) -> dict:
    """response_format для OpenAI-compatible chat.completions (LM Studio)."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }
