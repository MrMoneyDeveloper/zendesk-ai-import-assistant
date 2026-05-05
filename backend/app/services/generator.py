def run_generator(plan: dict):
    return [
        {
            "title": "Billing Routing",
            "conditions": [
                {"field": "group", "operator": "is", "value": "billing"}
            ],
            "actions": [
                {"field": "assign", "value": "Billing Team"}
            ]
        }
    ]