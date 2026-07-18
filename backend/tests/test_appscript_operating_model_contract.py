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


def test_apps_script_upgrade_is_batch_scoped_and_preserves_custom_sheet_content():
    utils = (APPSCRIPT / "Utils.gs").read_text(encoding="utf-8-sig")

    assert "function replaceBatchRows_" in utils
    assert "existingByRecordId" in utils
    assert "rowValuesWithFormulas_" in utils
    assert "getFormulas()" in utils
    assert "clearContent" not in utils
    assert "deleteSheet" not in utils
    assert "deleteRows" not in utils


def test_apps_script_upgrade_has_editor_installer_and_deployment_version_probe():
    api = (APPSCRIPT / "Api.gs").read_text(encoding="utf-8-sig")
    config = (APPSCRIPT / "Config.gs").read_text(encoding="utf-8-sig")
    operating_model = (APPSCRIPT / "OperatingModel.gs").read_text(encoding="utf-8-sig")
    manual_guide = (ROOT / "Appscripts" / "manual-install" / "README.md").read_text(
        encoding="utf-8"
    )

    assert "API_VERSION: '2026-07-18-operating-model-v2'" in config
    assert "api_version: APP_CONFIG.API_VERSION" in api
    assert "function inspectOperatingModelUpgradeFromEditor" in operating_model
    assert "function installOperatingModelUpgradeFromEditor" in operating_model
    assert "ensureRequiredTabs_(spreadsheet, true)" in operating_model
    assert "Do not replace `Secrets.gs`" in manual_guide
    assert "Deploy > Manage deployments" in manual_guide
