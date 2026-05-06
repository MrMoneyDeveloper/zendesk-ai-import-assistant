from pydantic import ValidationError

from app.models.schemas import GeneratedRecord


def validate_output(data: list[dict]) -> dict:
    errors: list[dict] = []

    for i, row in enumerate(data):
        try:
            GeneratedRecord.model_validate(row)
        except ValidationError as exc:
            errors.append(
                {
                    "row": i,
                    "error": exc.errors()[0]["msg"],
                }
            )

    return {
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }
