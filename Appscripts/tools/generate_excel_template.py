#!/usr/bin/env python
from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

OUTPUT_FILE = Path(__file__).resolve().parents[1] / 'templates' / 'Zendesk_Import_Assistant_Template.xlsx'

OBJECT_RECORD_HEADERS = [
    'batch_id',
    'record_id',
    'title',
    'object_type',
    'preview_summary',
    'conditions_json',
    'actions_json',
    'validation_status',
    'warning_message',
    'blocked_reason',
    'import_decision',
    'approved_by',
    'approved_at',
    'deployment_status',
    'zendesk_object_id',
    'chunk_id',
    'record_key',
    'department',
    'topic',
    'source_provider',
    'source_model',
]

OBJECT_TAB_NAMES = [
    'Ticket Fields',
    'Ticket Forms',
    'Brands',
    'Groups',
    'Macros',
    'Triggers',
    'Automations',
    'Views',
    'Tag Dictionary',
    'Recommendations',
    'Categories',
    'Sections',
    'Articles',
]

TABS = [
    ('Requests', ['batch_id', 'prompt', 'requester', 'status', 'target_environment', 'created_at']),
    ('Planning Output', ['batch_id', 'object_type', 'intent', 'confidence']),
    *((name, list(OBJECT_RECORD_HEADERS)) for name in OBJECT_TAB_NAMES),
    (
        'Batch Metadata',
        [
            'batch_id',
            'status',
            'coverage_status',
            'coverage_manifest_json',
            'department_coverage_json',
            'quality_gates_json',
            'supervisor_json',
            'llm_routes_json',
            'usage_report_json',
            'orchestration_json',
            'progress_narration_json',
            'updated_at',
        ],
    ),
    (
        'Progress Log',
        [
            'batch_id',
            'event_id',
            'status',
            'wave',
            'department',
            'object_type',
            'message',
            'source',
            'provider',
            'model',
            'at',
        ],
    ),
    ('Validation Log', ['batch_id', 'record_id', 'object_type', 'title', 'validation_status', 'warnings', 'blocked_reason', 'checked_at']),
    ('Approval Log', ['batch_id', 'record_id', 'import_decision', 'approved_at', 'approved_by']),
    ('Execution Log', ['batch_id', 'record_id', 'object_type', 'title', 'deployment_status', 'zendesk_object_id', 'execution_message', 'executed_at']),
    ('Integration Secrets', ['key_name', 'key_value', 'generated_at', 'generated_by', 'note', 'is_active']),
    ('Schema Registry', ['schema_name', 'schema_version', 'hash', 'updated_at', 'schema_json']),
]


def _col_name(index: int) -> str:
    name = ''
    while index > 0:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _escape_xml(value: str) -> str:
    return (
        value.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
        .replace('"', '&quot;')
        .replace("'", '&apos;')
    )


def _sheet_xml(headers: list[str]) -> str:
    rows = []
    cells = []
    for i, header in enumerate(headers, start=1):
      ref = f"{_col_name(i)}1"
      cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{_escape_xml(header)}</t></is></c>')
    rows.append(f'<row r="1">{"".join(cells)}</row>')
    dim = f"A1:{_col_name(max(1, len(headers)))}1"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dim}"/>'
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<sheetData>{"".join(rows)}</sheetData>'
        '</worksheet>'
    )


def _workbook_xml() -> str:
    sheets = []
    for idx, (name, _headers) in enumerate(TABS, start=1):
        safe_name = _escape_xml(name)
        sheets.append(f'<sheet name="{safe_name}" sheetId="{idx}" r:id="rId{idx}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets>{"".join(sheets)}</sheets>'
        '</workbook>'
    )


def _content_types_xml() -> str:
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    ]
    for idx in range(1, len(TABS) + 1):
        overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + ''.join(overrides) +
        '</Types>'
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '</Relationships>'
    )


def _workbook_rels_xml() -> str:
    rels = []
    for idx in range(1, len(TABS) + 1):
        rels.append(
            f'<Relationship Id="rId{idx}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{idx}.xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(rels) +
        '</Relationships>'
    )


def create_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with ZipFile(path, 'w', ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', _content_types_xml())
        zf.writestr('_rels/.rels', _root_rels_xml())
        zf.writestr('xl/workbook.xml', _workbook_xml())
        zf.writestr('xl/_rels/workbook.xml.rels', _workbook_rels_xml())

        for idx, (_name, headers) in enumerate(TABS, start=1):
            zf.writestr(f'xl/worksheets/sheet{idx}.xml', _sheet_xml(headers))


def main() -> int:
    create_template(OUTPUT_FILE)
    print(f'Wrote Excel template: {OUTPUT_FILE}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
