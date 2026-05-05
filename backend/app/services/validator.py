def validate_output(data):
    errors = []

    for i, row in enumerate(data):
        if not row.get("title"):
            errors.append({
                "row": i,
                "error": "Missing title"
            })

    return {
        "status": "passed" if not errors else "failed",
        "errors": errors
    }