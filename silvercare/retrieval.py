"""Provenance-based retrieval planning and bounded source-context assembly."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

Domain = Literal["medical", "welfare", "conversation", "general"]
SourceType = Literal[
    "patient_education",
    "clinical_guideline",
    "drug_mechanism",
    "drug_label",
    "medical_dataset",
    "welfare_guidance",
    "institution_directory",
    "contact_reference",
    "dialogue_case",
    "qa_case",
    "reference",
]
Language = Literal["ko", "en", "unknown"]
Jurisdiction = Literal["KR", "US", "international", "unspecified"]


@dataclass(frozen=True)
class SourceProfile:
    domain: Domain
    source_type: SourceType
    source_language: Language
    jurisdiction: Jurisdiction


PROFILES: dict[str, SourceProfile] = {
    "medlineplus": SourceProfile("medical", "patient_education", "en", "US"),
    "who-icope-2025": SourceProfile("medical", "clinical_guideline", "en", "international"),
    "va-pressure-2026": SourceProfile("medical", "clinical_guideline", "en", "US"),
    "fda-older-adult-medicines": SourceProfile("medical", "patient_education", "en", "US"),
    "fda-cyp-interactions": SourceProfile("medical", "drug_mechanism", "en", "US"),
    "openfda-drug-labels": SourceProfile("medical", "drug_label", "en", "US"),
    "mohw-care-2026": SourceProfile("welfare", "welfare_guidance", "ko", "KR"),
    "mohw-welfare-senior-2026": SourceProfile("welfare", "welfare_guidance", "ko", "KR"),
    "welfare-central": SourceProfile("welfare", "welfare_guidance", "ko", "KR"),
    "welfare-local": SourceProfile("welfare", "welfare_guidance", "ko", "KR"),
    "nhis-search": SourceProfile("welfare", "institution_directory", "ko", "KR"),
    "nhis-detail": SourceProfile("welfare", "institution_directory", "ko", "KR"),
    "mohw-129-guidance": SourceProfile("welfare", "contact_reference", "ko", "KR"),
    "ondam-reference-v1": SourceProfile("general", "contact_reference", "ko", "KR"),
}
DRUG_ALIASES = {
    "apixaban": ("apixaban", "아픽사반", "eliquis", "엘리퀴스"),
    "rivaroxaban": ("rivaroxaban", "리바록사반", "xarelto", "자렐토"),
    "dabigatran": ("dabigatran", "다비가트란", "pradaxa", "프라닥사"),
    "sitagliptin": ("sitagliptin", "시타글립틴", "januvia", "자누비아"),
    "gabapentin": ("gabapentin", "가바펜틴", "neurontin", "뉴론틴"),
    "pregabalin": ("pregabalin", "프레가발린", "lyrica", "리리카"),
    "memantine": ("memantine", "메만틴", "namenda", "에빅사"),
    "rosuvastatin": ("rosuvastatin", "로수바스타틴", "crestor", "크레스토"),
    "digoxin": ("digoxin", "디곡신", "lanoxin", "라녹신"),
    "clarithromycin": ("clarithromycin", "클라리트로마이신"),
}
MEDICAL = re.compile(
    r"건강|의료|의학|질환|질병|증상|진료|진단|치료|처방|검사|검진|약물|의약품|복약|복용"
    r"|혈압|당뇨|혈당|치매|통증|욕창|상처|드레싱|근력|근감소|노쇠|사레|삼킴|연하"
    r"|콩팥|신장|출혈|영양|식사|식단|낙상|넘어|수면|호흡|기억력|우울|보행|재활"
    r"|일어나기|다리에 힘|걷기|운동|비판막|(?<![가-힣])약(?:[이가을은의에과도]|\s|$)"
    r"|medical|health|disease|symptom|medicine|medication|drug|prescri|diabet|blood|renal"
    r"|kidney|swallow|dysphagia|wound|dressing|pressure injur|mobility|multimorbidity|frailty"
    r"|sarcopenia|exercise|care plan|rehabilitation",
    re.I,
)
WELFARE = re.compile(
    r"복지|장기요양|기초연금|지원금|보험|급여|보조금|신청|행정복지|주민센터|돌봄.?서비스"
    r"|노인맞춤돌봄|방문요양|요양기관|요양원|배움터|디지털배움|문화누림|아카데미"
    r"|welfare|benefit|pension|insurance|application|long.term care|care service",
    re.I,
)
DRUG_QUESTION = re.compile(
    r"약물|의약품|복약|복용|처방|항생제|혈압약|감기약|당뇨약|용량|상호작용"
    r"|(?<![가-힣])약(?:[이가을은의에과도]|\s|$)|drug|medicin|medication|dosage|dose|CYP|P-gp",
    re.I,
)
FACT_REQUEST = re.compile(
    r"[?？]|알려|궁금|어떻|어떤|무엇|왜|방법|추천|확인|신청|가능|상담|설명|가르쳐|될까|괜찮"
)
CHAT_ONLY = re.compile(
    r"그냥.{0,8}(?:이야기|대화|들어)|(?:이야기|대화).{0,5}(?:나누고|하고|해보고) 싶|들어만 줘"
)
GREETING = re.compile(r"\s*(?:안녕(?:하세요)?|고마워(?:요)?|감사(?:해요|합니다)|반가워요)[.!~\s]*")
MEDICAL_TERMS = {
    "nonvalvular atrial fibrillation": "비판막성 심방세동",
    "renal impairment": "신기능 저하(신장애)",
    "creatinine clearance": "크레아티닌 청소율",
    "serum creatinine": "혈중 크레아티닌",
    "substrate": "기질",
    "inhibitor": "억제제",
    "inducer": "유도제",
    "insufficient evidence": "근거 불충분",
    "pressure injury": "압박손상(욕창)",
    "frailty": "노쇠",
    "sarcopenia": "근감소증",
    "polypharmacy": "다제약물 복용",
    "dysphagia": "삼킴장애(연하곤란)",
}


def source_profile(dataset: str, kind: str) -> SourceProfile:
    if kind == "dataset_knowledge":
        return SourceProfile("medical", "medical_dataset", "ko", "unspecified")
    if kind in {"dialogue", "qa_case"}:
        return SourceProfile(
            "conversation" if kind == "dialogue" else "medical",
            "dialogue_case" if kind == "dialogue" else "qa_case",
            "ko",
            "unspecified",
        )
    return PROFILES.get(dataset, SourceProfile("general", "reference", "unknown", "unspecified"))


def drug_entities(text: str) -> tuple[str, ...]:
    found = []
    for canonical, aliases in DRUG_ALIASES.items():
        if any(
            re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.I)
            for alias in aliases
        ):
            found.append(canonical)
    return tuple(found)


def parent_record(record: str) -> str:
    return re.sub(r":chunk-\d+$", "", record)


def parent_identity(dataset: str, file_key: str, record: str) -> str:
    return hashlib.sha256(f"{dataset}:{file_key}:{parent_record(record)}".encode()).hexdigest()


def source_topic(profile: SourceProfile, title: str) -> str:
    if profile.source_type in {"drug_label", "drug_mechanism"}:
        return "의약품·신기능·상호작용"
    if profile.source_type == "clinical_guideline":
        if "욕창" in title:
            return "상처·욕창"
        if "영양" in title:
            return "영양·식사"
        if "이동" in title or "근력" in title:
            return "운동·신체활동"
        return "통합 돌봄·복합질환"
    if profile.domain == "welfare":
        return "복지·돌봄 제도"
    if profile.domain == "medical":
        return "의학·건강 정보"
    return "공식 안내" if profile.domain == "general" else "대화 참고"


@dataclass(frozen=True)
class RetrievalPlan:
    domains: tuple[Domain, ...] = ()
    drugs: tuple[str, ...] = ()
    medication: bool = False
    mechanism: bool = False
    needs_drug_identity: bool = False
    search_references: bool = True
    qa_examples: bool = False


def conversation_only(query: str) -> bool:
    return bool(
        GREETING.fullmatch(query) or (CHAT_ONLY.search(query) and not FACT_REQUEST.search(query))
    )


def retrieval_plan(query: str, current: str | None = None) -> RetrievalPlan:
    current = query if current is None else current
    if conversation_only(current):
        return RetrievalPlan(search_references=False)
    drugs = drug_entities(current)
    medical = bool(MEDICAL.search(query) or drugs)
    welfare = bool(WELFARE.search(query))
    domains: tuple[Domain, ...] = (
        ("medical", "welfare")
        if medical and welfare
        else ("medical",)
        if medical
        else ("welfare",)
        if welfare
        else ()
    )
    medication = bool(drugs or DRUG_QUESTION.search(query))
    mechanism = bool(re.search(r"CYP|P-gp|효소|기전|기질|억제|유도|수송체|표의? 빈칸", query, re.I))
    uncertain = (
        medication
        and not drugs
        and not mechanism
        and bool(re.search(r"같이|함께|병용|먹어도|안전|together|safe", current, re.I))
    )
    return RetrievalPlan(
        domains=domains,
        drugs=drugs,
        medication=medication,
        mechanism=mechanism,
        needs_drug_identity=uncertain,
        qa_examples=bool(re.search(r"객관식|선택지|시험|문항|퀴즈|정답|quiz|exam", current, re.I)),
    )


def odata_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def domain_filter(plan: RetrievalPlan) -> str:
    conditions = []
    if len(plan.domains) == 1:
        conditions.append(f"(domain eq {odata_string(plan.domains[0])} or domain eq 'general')")
    if plan.medication:
        if plan.drugs:
            entities = " or ".join(f"term eq {odata_string(drug)}" for drug in plan.drugs)
            conditions.append(
                f"(source_type ne 'drug_label' or drug_entities/any(term: {entities}))"
            )
        else:
            conditions.append("source_type ne 'drug_label'")
    return " and ".join(conditions)


def preferred_filter(plan: RetrievalPlan) -> str:
    kinds = {
        "patient_education",
        "clinical_guideline",
        "welfare_guidance",
        "institution_directory",
        "contact_reference",
    }
    if plan.domains == ("welfare",):
        kinds = {"welfare_guidance", "institution_directory", "contact_reference"}
    elif plan.medication:
        kinds = {"patient_education", "contact_reference"}
        if plan.drugs:
            kinds.add("drug_label")
        if plan.mechanism or plan.drugs:
            kinds.add("drug_mechanism")
    elif plan.domains == ("medical",):
        kinds = {"patient_education", "clinical_guideline", "contact_reference"}
    if "welfare" in plan.domains:
        kinds.update({"welfare_guidance", "institution_directory", "contact_reference"})
    return "(" + " or ".join(f"source_type eq {odata_string(kind)}" for kind in sorted(kinds)) + ")"


def choose_parents(
    broad: list[dict[str, Any]],
    preferred: list[dict[str, Any]],
    limit: int = 6,
    *,
    domains: tuple[Domain, ...] = (),
) -> list[dict[str, Any]]:
    for row in [*preferred, *broad]:
        if not isinstance(row.get("parent_id"), str) or not row["parent_id"]:
            raise ValueError("Missing contextual parent identity")
        if not isinstance(row.get("chunk_index"), int) or row["chunk_index"] < 0:
            raise ValueError("Missing contextual chunk index")
    seed = []
    seen = set()
    if len(domains) > 1:
        for domain in domains:
            candidate = next(
                (row for row in [*preferred, *broad] if row.get("domain") == domain),
                None,
            )
            if candidate is not None and candidate["parent_id"] not in seen:
                seed.append(candidate)
                seen.add(candidate["parent_id"])
    for row in preferred:
        if len(seed) == 2:
            break
        if row["parent_id"] not in seen:
            seed.append(row)
            seen.add(row["parent_id"])
    result = list(seed)
    for row in [*broad, *preferred]:
        if row["parent_id"] not in seen:
            result.append(row)
            seen.add(row["parent_id"])
            if len(result) == limit:
                break
    return result


def stitch_chunks(rows: list[dict[str, Any]]) -> str:
    texts: list[str] = [row["content"] for row in rows]
    lines = texts[0].split("\n", 3)
    prefix = ""
    if len(lines) == 4 and lines[0] == rows[0]["title"] and lines[1].startswith("Source date:"):
        prefix = "\n".join(lines[:3]) + "\n"
        if not all(text.startswith(prefix) for text in texts):
            prefix = ""
    if prefix:
        texts = [text.removeprefix(prefix) for text in texts]
    result = texts[0]
    for text in texts[1:]:
        overlap = next(
            (
                size
                for size in range(min(512, len(result), len(text)), 39, -1)
                if result.endswith(text[:size])
            ),
            0,
        )
        result += text[overlap:] if overlap else "\n\n" + text
    return prefix + result


def merge_parent(
    anchor: dict[str, Any], siblings: list[dict[str, Any]], max_characters: int = 3600
) -> dict[str, Any]:
    matching = {
        row["chunk_index"]: row for row in siblings if row["parent_id"] == anchor["parent_id"]
    }
    if len(matching) != len(siblings) or not any(
        row["id"] == anchor["id"] for row in matching.values()
    ):
        raise ValueError("Parent lookup did not return the exact anchor and unique chunk indexes")
    identity = (
        "dataset_id",
        "source_url",
        "source_type",
        "jurisdiction",
        "source_language",
        "audience",
        "updated_at",
        "source_year",
        "license",
    )
    if any(any(row.get(key) != anchor.get(key) for key in identity) for row in siblings):
        raise ValueError("Cannot merge different source identities or applicability")
    index = anchor["chunk_index"]
    selected = [matching[index]]
    if len(anchor["content"]) > max_characters:
        raise ValueError("Anchor chunk exceeds the evidence budget")
    for neighbor in (index - 1, index + 1):
        if neighbor in matching:
            candidate = sorted([*selected, matching[neighbor]], key=lambda row: row["chunk_index"])
            if len(stitch_chunks(candidate)) <= max_characters:
                selected = candidate
    return {
        **anchor,
        "content": stitch_chunks(selected),
        "source_record_id": parent_record(anchor.get("source_record_id", "")),
        "context_record_ids": [row.get("source_record_id", "") for row in selected],
        "context_chunk_ids": [row["id"] for row in selected],
        "risk_level": (
            "clinical"
            if any(row.get("risk_level") == "clinical" for row in selected)
            else anchor.get("risk_level", "general")
        ),
    }


def terminology(rows: list[dict[str, Any]]) -> dict[str, str]:
    text = "\n".join(row["content"] for row in rows)
    return {
        term: korean
        for term, korean in MEDICAL_TERMS.items()
        if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text, re.I)
    }
