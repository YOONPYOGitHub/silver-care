import pytest

from silvercare.safety import (
    MEDICAL_CAUTION,
    MEDICAL_DISPLAY_NOTICE,
    NOTICE,
    answer_display_notice,
)


def test_medical_display_combines_ai_and_clinical_cautions_without_changing_raw_fields():
    result = answer_display_notice(
        NOTICE,
        MEDICAL_CAUTION,
        dataset_cited=False,
        has_sources=True,
    )
    assert result == MEDICAL_DISPLAY_NOTICE
    for meaning in ("AI", "오류", "진단", "복약 변경", "검사", "의료진"):
        assert meaning in result
    assert len(result) < len(NOTICE) + len(MEDICAL_CAUTION)


@pytest.mark.parametrize("medical", [None, ""])
def test_general_notices_preserve_special_source_information(medical):
    notice = "자동 정리본이며 전문가 검토 전입니다. " + NOTICE
    assert (
        answer_display_notice(
            notice,
            medical,
            dataset_cited=True,
            has_sources=True,
        )
        == notice
    )


def test_compact_medical_notice_keeps_dataset_and_missing_evidence_disclosures():
    result = answer_display_notice(
        NOTICE,
        MEDICAL_CAUTION,
        dataset_cited=True,
        has_sources=True,
    )
    assert "전문가 검토 전" in result
    assert "AI Hub" in result and "원문이 아닌 데이터셋 안내" in result
    missing = answer_display_notice(
        NOTICE,
        MEDICAL_CAUTION,
        dataset_cited=False,
        has_sources=False,
    )
    assert "인용된 참고자료는 없습니다" in missing


def test_unknown_custom_medical_guidance_is_not_silently_discarded():
    caution = "사용자에게 꼭 보여야 하는 별도 의료 주의입니다."
    result = answer_display_notice(NOTICE, caution, dataset_cited=False, has_sources=True)
    assert NOTICE in result and caution in result
