from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APPSCRIPT = ROOT / "Appscripts" / "src"


def test_apps_script_supports_every_heavy_operating_model_object_type():
    config = (APPSCRIPT / "Config.gs").read_text(encoding="utf-8")
    for sheet_name in (
        "Brands",
        "Groups",
        "Automations",
        "Categories",
        "Sections",
        "Articles",
        "Batch Metadata",
        "Progress Log",
    ):
        assert f"name: '{sheet_name}'" in config
    for object_type in ("brands", "groups", "automations", "categories", "sections", "articles"):
        assert f"{object_type}:" in config


def test_apps_script_exposes_idempotent_metadata_and_progress_actions():
    api = (APPSCRIPT / "Api.gs").read_text(encoding="utf-8")
    writer = (APPSCRIPT / "SheetWriter.gs").read_text(encoding="utf-8")
    operating_model = (APPSCRIPT / "OperatingModel.gs").read_text(encoding="utf-8")

    assert "action === 'write_batch_metadata'" in api
    assert "action === 'append_progress_events'" in api
    assert "replaceBatchRows_" in writer
    assert "function writeBatchMetadata" in operating_model
    assert "function appendProgressEvents" in operating_model
    assert "upsertRowsByKeys_" in operating_model


def test_apps_script_enforces_approval_and_reports_pipeline_timing():
    api = (APPSCRIPT / "Api.gs").read_text(encoding="utf-8")
    approval = (APPSCRIPT / "Approval.gs").read_text(encoding="utf-8")
    execution = (APPSCRIPT / "ExecutionLog.gs").read_text(encoding="utf-8")

    assert "action === 'get_batch_operational_state'" in api
    assert "timings_ms" in api
    assert "requestedDecision === 'approved'" in approval
    assert "importDecision = 'blocked'" in approval
    assert "latestByRecord" in execution
