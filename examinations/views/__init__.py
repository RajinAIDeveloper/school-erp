"""The examination screens, one module per area. Every view is importable from here."""

from .combined import (  # noqa: F401
    combined_card,
    combined_detail,
    combined_list,
    combined_verify,
)
from .common import (  # noqa: F401
    _as_exam,
    _cell_text,
    card_enrollment,
    mask_name,
    verification_url,
)
from .exams import (  # noqa: F401
    ExamListView,
    _checklist_for,
    _clashes_for,
    _typed_parts,
    admit_cards,
    exam_detail,
    exam_routine,
    grade_rules,
    paper_parts,
    publish,
    request_unlock,
    scale_presets,
    unlock_review,
    unlocks,
)
from .marks import (  # noqa: F401
    ForecastFilter,
    MarkFilter,
    MarkForm,
    OverallCommentFilter,
    _comment_rows,
    comments,
    forecasts,
    mark_save,
    marks,
)
from .results import (  # noqa: F401
    ResultsFilter,
    _class_report,
    _report_download,
    progress,
    public_results,
    report_card,
    report_cards,
    results,
    verify,
)
from .series import (  # noqa: F401
    OFFICIAL_IMPORT_KEY,
    _series,
    board_registration,
    series_detail,
    series_export,
    series_list,
    series_results,
)
