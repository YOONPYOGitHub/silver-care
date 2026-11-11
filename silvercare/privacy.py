import re
import unicodedata

PATTERNS = [
    (re.compile(r"(?<!\d)\d{6}\s*[-–]?\s*[1-8]\d{6}(?!\d)"), "[주민번호 삭제]"),
    (
        re.compile(r"(?<!\d)(?:\+82[- .]?)?0?1[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)"),
        "[전화번호 삭제]",
    ),
    (re.compile(r"(?<!\d)0[2-6]\d?[- .]\d{3,4}[- .]\d{4}(?!\d)"), "[전화번호 삭제]"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[이메일 삭제]"),
    (re.compile(r"(?:성명|이름|환자명)\s*[:：]\s*[가-힣]{2,5}"), "이름: [삭제]"),
    (
        re.compile(
            r"(?:저는|제 이름은)\s*"
            r"[김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주]"
            r"[가-힣]{1,2}(?=\s*(?:입니다|이고|이에요|예요))"
        ),
        "저는 [이름 삭제]",
    ),
    (re.compile(r"(?:주소|거주지)\s*[:：][^\n]{3,100}"), "주소: [삭제]"),
    (re.compile(r"(?:계좌(?:번호)?)\s*[:：]?\s*[\d -]{8,30}"), "계좌: [삭제]"),
]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()).strip()


def redact(text: str) -> str:
    result = normalize(text)
    for pattern, replacement in PATTERNS:
        result = pattern.sub(replacement, result)
    return result
