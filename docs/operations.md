# 실행·수집·배포·검증

자료·릴리스 설정 확인 기준일: 2026-11-13.
프로젝트 루트에서 실행한다. 구조는 [아키텍처](architecture.md), 데이터 의미는 [RAG·API](rag.md)를 따른다.

## 1. 설정과 비밀값

[config.py](../silvercare/config.py)의 `Settings`는 앱·Azure·질병관리청 설정,
`DataSourceSettings`는 AI Hub·복지·장기요양 수집 설정을 읽는다.
[.env.example](../.env.example)은 변수명과 비민감 로컬 기본값만 담은 공유용 틀이다.
연결 주소·배포명·리소스 ID·계정·키는 비어 있으며 실제 값은 Git에서 제외한 `.env`에 둔다.
예시 파일을 복사하는 것만으로 모델·검색·음성이 연결되지는 않는다.
실제 `.env`나 사용자가 제공한 원본 문서를 예시 파일로 덮어쓰지 않는다.

| 사용처 | 환경변수 | 배포·노출 |
| --- | --- | --- |
| AI Hub 파일 수집 | `AIHUB_API_KEY` | 로컬 수집 작업 |
| 중앙부처·지자체 복지 수집 | `WELFARE_CENTRAL_API_KEY`, `WELFARE_LOCAL_API_KEY` | 로컬 수집 작업 |
| 장기요양 검색·상세 수집 | `NHIS_LTC_SEARCH_API_KEY`, `NHIS_LTC_DETAIL_API_KEY` | 로컬 수집 작업 |
| 질병관리청 실행 API | `KDCA_HEALTH_API_KEY` | 서버 Kubernetes Secret |
| 질병관리청 키 호환 이름 | `NHIS_HEALTH_INFO_API_KEY` | 전용 이름이 없을 때 사용 |
| 수동 포털 로그인 | `AIHUB_ID`, `AIHUB_PASSWORD`, `DATA_GO_KR_ID`, `DATA_GO_KR_PASSWORD` | 앱·자동 로그인에 사용하지 않음 |
| Azure 모델·검색·음성 | Azure CLI 로그인 또는 Workload Identity | Azure API 키를 브라우저에 전달하지 않음 |

복지 계열은 전용 키가 없으면 `WELFARE_API_KEY`, 장기요양 계열은 `NHIS_LTC_API_KEY`도 읽는다.
키별 변수는 분리하며 범용 `API_KEY`는 사용하지 않는다.
전용 변수를 빈 값으로 설정하면 호환 이름으로 자동 전환한다고 가정하지 않는다.
`NHIS_HEALTH_INFO_API_KEY`만 사용할 경우 예시 파일의 빈 `KDCA_HEALTH_API_KEY=` 줄을 제거한다.
키 문자열은 설정 파일에서 보존하고 요청 때 한 번 디코딩해 요청 인코딩에 맡긴다.

네 수집 endpoint는 `WELFARE_CENTRAL_API_ENDPOINT`, `WELFARE_LOCAL_API_ENDPOINT`,
`NHIS_LTC_SEARCH_API_ENDPOINT`, `NHIS_LTC_DETAIL_API_ENDPOINT`다.
질병관리청 endpoint는 [health.py](../silvercare/health.py)의 고정 HTTPS 주소다.

### 질병관리청 TLS·인증

가이드의 HTTP 예시 대신 HTTPS를 사용한다.
서버가 요구하는 `ssl.OP_LEGACY_SERVER_CONNECT`는 해당 API 전용 클라이언트에만 적용하고
인증서·호스트 검증과 TLS 1.2 이상을 유지한다.
검증을 끄거나 HTTP로 키를 보내지 않는다.
GET 요청의 `TOKEN`은 `httpx` 로그에서 가리고, 리다이렉트를 따라 전달하지 않는다.
POST는 실제 404로 응답해 이 API에서 사용하지 않는다.

## 2. 로컬 실행

```bash
uv sync --group dev
az login
APP_ENV=development PUBLIC_ORIGIN=http://127.0.0.1:8000 TRUSTED_PROXY=false \
uv run uvicorn silvercare.app:create_app --factory --host 127.0.0.1 --port 8000
```

이 명령은 `.env`의 기능 설정을 사용한다. 로컬 실행은 `APP_ENV=development`,
`PUBLIC_ORIGIN=http://127.0.0.1:8000`, `TRUSTED_PROXY=false`로 설정한다.
모델·검색·임베딩·음성 연결값은 배포 설정과 검증된 릴리스에서 확인하며 비밀값을 터미널에 출력하지 않는다.
`Settings`와 `DataSourceSettings`의 키·endpoint를 빠짐없이 채우되 Azure 로그인 토큰을 환경 파일에 저장하지 않는다.

운영 릴리스와 같은 검색 설정을 명령 안에서 명시적으로 불러오려면:

```bash
uv run python - <<'PY'
import json
import os
from pathlib import Path
import uvicorn

os.environ.update(json.loads(Path("data/processed/expanded/release.json").read_text()))
os.environ["APP_ENV"] = "development"
os.environ["PUBLIC_ORIGIN"] = "http://127.0.0.1:8000"
os.environ["TRUSTED_PROXY"] = "false"
uvicorn.run("silvercare.app:create_app", factory=True, host="127.0.0.1", port=8000)
PY
```

`release.json`은 endpoint·인덱스·검색 기능만 포함하며 비밀값이 아니다.
`Settings` 자체가 자동으로 이 파일을 로드하지는 않는다.
CLI는 명령별 환경변수, 배포 도구는 릴리스 파일을 사용한다.
검색 서비스 역할이 없으면 로그인 성공만으로 검색이 동작하지 않는다.
`PREFER_OFFICIAL_REFERENCES`의 기본값은 `false`이며 검증한 `v8` 릴리스에서는 `true`다.
공식 안내 후보를 먼저 확보하고 질병관리청 API 근거를 먼저 배치하는 동작을 제어한다.
검색·답변 평가와 릴리스·배포에서 같은 값을 사용해야 한다.
`NONCOMMERCIAL_DATA_USE`도 기본값은 `false`다. 운영자가 확인한 비상업적 이용의 `v8`은 `true`이며,
WHO 수집·발행·검색·릴리스에서 이 조건을 확인한다.
`CONTEXTUAL_RETRIEVAL`는 기본값 `false`, v8 릴리스에서는 `true`다.
이 기능은 자료 유형·분야 필터와 부모 키·청크 순번이 있는 v8 인덱스에서만 켠다.
이전 인덱스에 켜거나 부모 원문 조회가 실패하면 검색 오류를 알리고 조용히 기존 검색으로 되돌리지 않는다.

## 3. 수집과 오프라인 준비

아래 작업은 다운로드·파일 갱신만 수행하며 앱 배포·운영 인덱스 적재를 자동으로 수행하지 않는다.

```bash
# 등록된 AI Hub 파일 확인·다운로드. 수동 승인 완료 및 적법한 이용 범위가 전제다.
uv run python -m silvercare.corpus list
uv run python -m silvercare.corpus fetch --max-mb 20

# 출처가 확인된 원천만 명시적으로 선택한다.
uv run python -m silvercare.external_sources \
  --providers mohw-care-2026 medlineplus welfare-central nhis-search mohw-129-guidance

# 확인한 노령층 안내서만 추가·갱신한다. 다른 provider 자료는 보존한다.
uv run python -m silvercare.external_sources --providers mohw-welfare-senior-2026

# 환자교육 summary 155개 주제와 재사용 정책을 검증하는 FDA 본문 2개를 수집한다.
uv run python -m silvercare.external_sources \
  --providers medlineplus fda-older-adult-medicines

# 운영자가 WHO 자료의 비상업적 이용을 확인한 경우에만 실행한다.
uv run python -m silvercare.clinical_sources --noncommercial-use

# 직접 작성한 기관 안내와 등록 corpus를 준비한다.
uv run python -m silvercare.data prepare \
  --manifest data/reference.json --output data/processed/reference.jsonl
uv run python -m silvercare.expanded prepare \
  --external data/processed/external-knowledge.jsonl \
  --clinical data/processed/clinical-knowledge.jsonl
```

`expanded prepare`에서 `--external`을 빼면 외부 지식이 준비 결과에 들어가지 않는다.
`--clinical`을 빼면 전문 근거 591개 청크도 들어가지 않으므로 운영 corpus 준비에는 둘 다 지정한다.
집계 준비는 최종 지식·사례·평가 파일에 분야·자료 유형·원문 언어·국가·부모 키·청크 순번·
제한된 약명 연결을 채운다. 원천별 수집 산출물을 최종 검색 메타데이터 집계본으로 오해하지 않는다.
원본·manifest·수집 응답을 먼저 점검하고 통계·출처·개인정보·평가 분리를 검토한다.
수집기의 기본 provider 목록에는 지자체·기관 상세가 포함되고 129 이용안내는 별도 선택이다.
노령층 안내서 `mohw-welfare-senior-2026`도 명시적으로 선택한다.
수집기는 보건복지부 본문의 책 활용 허용·이미지 제한, 게시일, 고정 PDF hash와 구조를 확인한다.
FDA 수집은 opt-in이며 정책 확인 후 고정 URL 2개의 제목·본문·날짜를 검증한다.
별도 저작권 표시가 있거나 본문·날짜 구조가 바뀌면 실패하므로 권한 확인을 끄고 재시도하지 않는다.
MedlinePlus의 요청·실제 제목 차이는 보고서에 남기며 원문 summary와 한글 검색 제목을 구분한다.
지원 내용·대상의 복잡한 표/도식은 원문 확인 안내로 대체하며 그 범위를 수집 보고서에 남긴다.
원본이 바뀌면 hash를 무조건 새 값으로 바꾸지 말고 이용조건·목차·절/문의 열을 다시 검토한다.
전체 기본 수집이 일부 실패할 수 있으므로 종료 코드와 `external-collection-report.json`을 확인한다.

전문 근거 수집은 WHO·VA·FDA·openFDA를 고정된 범위에서 함께 수집한다.
한 원천이라도 실패하면 기존 `clinical-knowledge.jsonl`을 덮어쓰지 않고
`clinical-collection-report.json`에 실패를 남긴다. 이 수집과 운영 인덱스 발행은 별개다.
WHO·VA는 고정 PDF hash·쪽수·열/본문 영역을 확인하므로 새 판에 hash만 바꿔 적용하지 않는다.
FDA 표의 행·열·병합·머리글을 보존하고, 라벨은 경구용 9성분의 선택본만 보관한다.
openFDA는 현재 키 없이 고정 조회하며 속도 제한을 우회하지 않는다. 429나 조회 실패는 수집 실패다.
원문 라벨의 47개 절·표 HTML/셀 구조와 신청 정보는 `clinical-structured.json`에 보관한다.
수치 관계를 검증하지 못한 표가 붙은 절은 온라인 숫자 지시로 평탄화하지 않는다.
개인별 용량을 계산하는 기능이나 한국 허가 기준을 검증한 의약품 DB는 아니다.
WHO 가공 부분은 `who-icope-adapted.jsonl`로도 분리하며 출처·가공 고지·CC BY-NC-SA 조건을 유지한다.

기관 상세는 검색 결과의 기관 ID가 필요하므로 `--providers nhis-search nhis-detail`처럼 선택한다.
지자체·기관 상세의 미수집 상태는 [API 범위 표](rag.md#4-수집-시-사용하는-api)에 있다.
`129-faq`와 `kdca-older-adults`는 HTML 대량 수집 경로이며 권한 게이트로 실패한다.
질병관리청 실행 API 연결은 이 명령과 무관하게 `HealthService`가 담당한다.

AI Hub 목록 endpoint는 HTTP 오류와 함께 목록 본문을 반환하는 경우가 있다.
CLI는 HTTP 오류를 실패로 보고하므로 필요하면 공식 웹 파일 목록에서 ID를 확인한다.
다운로드는 검증한 HTTPS 스트림 호스트로만 이동하며 스트림 호스트에 API 키 헤더를 전달하지 않는다.

## 4. 문서 임베딩·인덱스 발행

**활성 인덱스를 대상으로 아래 작업을 바로 실행하지 않는다.**
`expanded.py`의 `KNOWLEDGE_INDEX`·`CASE_INDEX`는 발행 대상 이름이다.
코드 상수와 활성 릴리스는 모두 `v8`이므로 새 corpus를 발행하려면 먼저
두 발행 대상을 사용하지 않는 버전 이름으로 지정해야 한다.
목표 이름을 바꾸는 CLI 옵션이나 자동 blue/green 이름 생성은 구현되어 있지 않다.

예를 들어 다음 발행 대상이 `silvercare-knowledge-v9`, `silvercare-cases-v9`라면:

1. 코드의 두 발행 상수를 해당 미사용 이름으로 설정한다.
2. `AZURE_SEARCH_INDEX`·`AZURE_SEARCH_CASE_INDEX`는 발행 시점의 활성 인덱스를 가리키게 한다. 발행기는 이 설정과 같은 endpoint의 `release.json`에 기록된 지식·사례 인덱스를 모두 보호한다.
3. Azure endpoint·임베딩 배포·차원·검색 기능을 명시하고 발행한다.
4. 평가 때는 환경변수의 인덱스를 발행한 새 대상으로 전환한다.
5. 평가·count·hash·용량 검증 뒤 릴리스를 생성한다.

```bash
# 위 1~2단계의 발행 대상 검토가 끝난 경우에만 실행한다.
export AZURE_SEARCH_ENDPOINT=https://silvercare-rag-2a5aalbg7ugfc.search.windows.net
export AZURE_SEARCH_INDEX=silvercare-knowledge-v8
export AZURE_SEARCH_CASE_INDEX=silvercare-cases-v8
export AZURE_EMBEDDING_DEPLOYMENT=silvercare-embedding
export EMBEDDING_DIMENSIONS=512
export RETRIEVAL_MODE=hybrid
export REWRITE_QUERIES=true
export SEMANTIC_RANKING=true
export PREFER_OFFICIAL_REFERENCES=true
export NONCOMMERCIAL_DATA_USE=true
export CONTEXTUAL_RETRIEVAL=true
uv run python -m silvercare.expanded publish

# 평가·릴리스는 검토한 새 버전 대상으로 실행한다.
export AZURE_SEARCH_INDEX=silvercare-knowledge-v9
export AZURE_SEARCH_CASE_INDEX=silvercare-cases-v9
uv run python -m silvercare.evaluate \
  --queries data/retrieval-evaluation.json \
  --output data/processed/expanded/retrieval-evaluation-final.json
uv run python -m silvercare.evaluate --audience \
  --queries data/audience-evaluation.json \
  --output data/processed/expanded/audience-evaluation.json
uv run python -m silvercare.expanded release
```

`publish`는 두 corpus의 타입·중복 ID·발행 대상을 원격 변경 전에 검증한다.
현재 설정과 기록된 릴리스의 활성 인덱스는 발행 대상으로 사용할 수 없다.
`publish`는 모델·차원·입력 hash가 같은 벡터를 재사용한다.
v8은 본문·제목을 바꾸지 않는 메타데이터/검색 개선이므로 문서 벡터를 전부 재사용했다.
현재 검색 서비스는 v2~v8 지식·사례 14개 인덱스로 15개 할당량 중 14개를 사용한다.
다음 쌍을 만들기 전에는 활성·롤백 대상을 제외한 이전 버전의 명시적인 정리 계획이나 용량 검토가 필요하다.
배포 도구가 오래된 인덱스를 자동 삭제하지는 않는다.
관리하는 발행 대상에서는 변경 문서를 업데이트하고 불필요한 ID를 삭제하므로 대상의 혼동을 피한다.
`data ingest`/`corpus ingest`는 별도의 범용·비벡터 적재 경로이며 운영 하이브리드 인덱스 재발행에 사용하지 않는다.

`release`는 발행 보고서의 검색 endpoint·준비된 corpus hash·발행 문서 수·검색 평가·답변 평가·검색 용량을 검증한다.
endpoint가 없는 발행 보고서는 서비스 일치를 검증할 수 없어 새 릴리스의 근거로 사용하지 않는다.
답변 평가는 프롬프트·시나리오·인덱스·공식 API 활성 상태도 확인한다.
검색의 `required` 대상은 정확한 원문 URL로 판정하며 하나라도 상위 6개에 없으면 실패한다.
답변의 `required_concepts`는 모든 개념 그룹에서 적어도 한 표현이 있어야 한다.
`expected_mode`·`required_source_type_groups`로 일상 대화와 의료/복지 근거 사용도 검사한다.
`required_patterns`는 실제로 요청한 예시의 출력 형태를 검사하며 유효하지 않은 정규식은 거부한다.
`require_medical_caution`은 답변 본문과 별도로 앱에 표시하는 건강 주의 필드를 검사한다.
일반 설명의 본문에 매번 같은 의사·약사 문구를 반복해야만 안전 검사가 통과하는 구조로 만들지 않는다.
제공자 오류는 보고서에 실패로 남기고 다음 질문을 평가한다. 모든 `400`을 허용하지 않는다.
개인별 복약 여부를 묻는 빈혈 사례만 `accept_provider_safety_refusal`을 명시한다.
이 경우 확인된 모델 `content_filter` 거절은 안전 경계 검사로만 인정하고
`answer_generated=false`·`provider_safety_refusals`로 의학 답변과 별도 집계한다.
앱도 이를 `422 / safety_refusal`로 표시하며 의료진 확인을 안내한다. 안전 필터 설정은 변경하지 않는다.
`deploy`가 이를 매번 재평가하는 것은 아니다.
프롬프트·답변 처리 변경은 데이터 재발행 필요성과 별개로 실제 답변 평가와 릴리스 정합성을 확인한다.
롤백은 이전 릴리스 파일·검색 인덱스·설정·이미지를 함께 맞춰 복원한다.
이미지만 되돌리고 WHO 인덱스나 이용조건 설정을 남겨 두지 않는다. 이전 v6 인덱스는 삭제하지 않았다.

## 5. AKS 배포

리소스를 다시 만들지 않고 앱을 갱신할 때는 새로운 고유 이미지 태그를 지정한다.

```bash
uv run python -m silvercare.deploy --skip-infra --tag release-YYYYMMDD-N
.runtime/bin/kubectl --kubeconfig .runtime/kubeconfig -n silvercare get pods,svc
curl -fsS https://ondam-e4b60458.koreacentral.cloudapp.azure.com/health/live
```

`deploy`는 `release.json`을 읽고 ACR 빌드·IAM 확인·Kubernetes 적용·rollout·공개 HTTPS 검증을 수행한다.
배포 상태 파일은 rollout과 공개 HTTPS 검증이 성공한 뒤에만 원자적으로 갱신한다.
실패한 배포를 마지막 정상 배포로 기록하지 않으며, Secret을 포함할 수 있는 stdin 적용 실패의 원문 stderr는 출력하지 않는다.
질병관리청 키가 설정되어 있으면 `silvercare-health` Secret을 적용하고 앱 환경변수에 참조로 연결한다.
문서·원본·처리 결과·`.env`는 [.dockerignore](../.dockerignore)에 의해 빌드에서 제외한다.
앱 실행 Python은 `/app/.venv/bin/python`이다. 컨테이너에서 검증 명령을 실행할 때도 같은 환경을 사용한다.

신규 환경에는 [main.bicep](../infra/main.bicep)의 기반,
[search-rag.bicep](../infra/search-rag.bicep)의 Basic 검색,
모델·임베딩·음성 배포, IAM과 데이터 릴리스가 필요하다.
기반 템플릿만으로 전체 RAG와 모델이 구성되지는 않는다.
AKS·ACR·검색·모델·음성에는 요금이 발생한다.
Basic 검색은 replica 1 / partition 1도 유료이며 비용은 Azure 현행 가격·계약으로 확인한다.

## 6. 검증

전체 검증에는 Python 개발 의존성과 Node.js가 필요하다.
Node.js는 브라우저용 JavaScript의 문법 검사와 모의 DOM·미디어 회귀 테스트에 사용하며 앱 서버 실행에는 필요하지 않다.
Node.js가 없으면 프론트엔드 pytest가 건너뛰어질 수 있으므로 건너뛴 테스트까지 확인한다.

```bash
uv run pytest -q
uv run ruff check silvercare tests
uv run mypy
node --check silvercare/static/app.js
```

실제 검색·모델 평가에는 4절의 Azure 검색 설정이 필요하다.
음성 실측에는 허용된 WAV·정답 전사 쌍을 사용한다.

```bash
uv run python -m silvercare.evaluate \
  --speech-manifest data/raw/aihub/107/speech-evaluation.json \
  --speech-base-url https://ondam-e4b60458.koreacentral.cloudapp.azure.com \
  --output data/processed/expanded/speech-evaluation.json
```

건강정보 검증은 본문의 생성 모델 전달, 답변의 `official_api` 인용, AI Hub와의 결합,
응급 시 외부 호출 우회, 빈 본문·실패의 명시, 원문 열람 유지를 확인한다.
개인정보 안내·키 비노출·320/390/768/1440 화면 폭·큰 글씨도 검증 대상이다.
테스트에서 실제 응급전화 링크를 실행하지 않는다.

### 텍스트·음성 모드

- 초기 상태는 ‘글로 대화’다. 입력·Enter/보내기·주제 카드·대화 이력·출처 동작을 유지한다.
- 모드 선택과 입력/음성 버튼은 `conversation-dock`에 있다. 대화는 `conversation-scroll`만 스크롤하며 조작부가 메시지를 덮지 않는다.
- 입력·음성 패널은 위쪽 `dock-content`, 모드 선택은 그 아래 별도 행에 있다.
  모드·음성 상태·오류 표시·입력창 높이가 바뀌어도 선택 버튼의 좌표·크기가 유지되어야 한다.
- ‘말로 대화’ 전환은 이용자의 마이크 사용 선택이다. 별도 `stt-toggle`/`tts-toggle` 조합은 없다.
- 마이크 입력의 음량 분석으로 발화·약 3초의 말끝 무음을 감지한다. 이는 스트리밍 STT나 동시 발화 모델이 아니다. 발화 완료 후 파일 단위 STT를 호출한다.
- `voice.phase`를 `voice-panel`의 `data-voice-phase`와 연결한다. 실제 듣는 상태에서만 마이크의 부드러운 파장을 표시하고 입력 RMS의 5단계 막대를 갱신한다. 무음·마이크 종료·모드 전환에서는 막대를 0으로 되돌린다. 음량 반응은 소리 입력 표시이지 인식 성공·정확도 표시가 아니다.
- 본문 확인·답변 준비는 시계와 점 표시, 실제 답변 재생은 스피커, 일시정지는 일시정지 아이콘으로 구분한다. 듣는 중에는 ‘말 마침’·‘잠시 멈춤’, 답변 준비/재생 중에는 ‘답변 멈추고 말하기’·‘잠시 멈춤’, 멈춘 상태에는 ‘대화 시작’을 표시한다.
- `prefers-reduced-motion: reduce`에서는 파장·점 애니메이션과 막대 전환 효과를 끈다. 상태 문구·아이콘·정적인 테두리·입력 단계는 유지하며 색상이나 움직임만으로 상태를 전달하지 않는다. 음량 갱신마다 스크린리더 안내가 반복되지 않도록 입력 표시는 live region으로 만들지 않는다.
- ‘말 마침’으로 감지를 기다리지 않고 전송할 수 있다. 입력 전 무음 30초는 일시정지, 한 번 녹음의 상한은 120초·4MB다. 인식된 질문의 2,000자 제한을 넘어가는 경우 조용히 잘라 전송하지 않는다.
- 인식 → 같은 `/api/chat` → 답변 음성 → 재생 완료 → 다음 입력의 순서다. 답변 중에는 녹음하지 않아 자기 목소리의 재입력을 피한다.
- ‘잠시 멈춤’·텍스트 전환·새 대화·숨겨진 페이지는 음성 세션을 중단한다. 취소된 인식·재생 응답으로 마이크가 다시 시작되면 안 된다.
- 음성·텍스트는 `state.history`를 공유한다. 텍스트로 전환해도 이미 수락한 대화·인용은 사라지지 않는다.
- 인식된 말과 답변은 대화 목록에 표시한다. 하단 음성 패널은 아이콘·상태·음량·필요한 조작만 표시하며 긴 답변을 중복 표시하지 않는다. 상세 음성 설명과 시간 제한은 화면 설정과 이용 안내에서 확인한다.
- 화면 설정은 데스크톱 왼쪽 메뉴 또는 작은 화면의 상단 버튼에서 `screen-settings` dialog를 연다. 같은 설정 버튼은 중복 표시하지 않는다. 음성 대화를 중단하며 닫은 뒤 명시적인 ‘대화 시작’이 필요하다. Escape/닫기는 호출 버튼의 포커스를 복원하고 대화·글자 크기를 유지한다.
- ‘함께 알아보기’는 주제 상단으로 이동해 공식 건강정보 목록을 펼친다. 메뉴 클릭과 `#topics` 직접 접속을 점검하고 원문 보기·질문 카드·대화로 돌아오기에서 이력과 검색 동작을 유지해야 한다.
- 새 답변은 본문 시작을 표시한다. 출처·건강정보 주의·공식 자료 카드는 삭제하거나 가리지 않고 본문 뒤에서 읽는다.
- 답변의 일반 AI 안내·의료 주의·공식 자료 소개 문장을 반복하지 않고 `display_notice` 하나로 표시한다.
  원본 `notice`·`medical_caution` 필드는 API에 남긴다. 자료별 권리 고지는 기존 ‘이용 안내’로 옮겨
  전체 텍스트·라이선스 링크를 유지하며 출처 버튼 자체는 답변 아래에 그대로 둔다.
  키보드 열기/닫기·화면 회전·입력창 확장·비동기 자료 높이 변화에도 답변 시작을 유지한다.
- 사용자가 답변 중간이나 과거 대화를 직접 읽으면 자동 위치 맞춤과 포커스 이동을 멈춘다.
  ‘새 답변 보기’는 새 답변 시작으로 이동한다. 환영·주제 영역이나 펼친 공식 자료에서 강제로 맨 아래로 이동하지 않는다.
  화면 높이는 `visualViewport`로 반영하고 safe-area를 적용한다.
- 모바일 공식 건강정보 선택기는 라벨 아래 선택창·원문 보기 버튼을 같은 행에 둔다.
  긴 주제명·큰 글씨에서도 선택창이 가용 폭으로 줄어들고 원문 보기 버튼은 화면 안에 남는다.
- 음성 모드와 ‘이 답변 듣기’는 서버의 같은 읽기용 변환을 사용한다. 화면·대화 이력의 답변은 유지하고, TTS에는 마크다운 서식 기호·인용 번호·별도 출처 목록을 제외한 본문을 전달한다. 링크는 표시 이름을 읽고, 본문의 의학적 주의·119·숫자·단위·확인 질문은 유지한다. 읽을 본문이 없는 요청은 422로 거부한다.
- 실제 모바일 기기의 마이크 권한·스피커·브라우저 수명주기는 기기별 검증이 필요하다. 마이크나 Web Audio를 지원하지 않는 환경에는 텍스트 모드를 안내한다.
- 자동 검증은 브라우저의 모의 마이크에 허용된 실제 음성 표본을 넣어 STT·답변·TTS와 다음 마이크 시작까지 확인한다. 이는 실제 아이폰·갤럭시의 하드웨어 마이크나 소음 환경 검증을 대신하지 않는다.

상태는 아래 순서로 연결하며, 텍스트 모드에는 음성 패널을 표시하지 않는다.

| `voice.phase` | 표시 | 마이크·동작 |
| --- | --- | --- |
| `starting`, `permission` | 시계·연결 안내·점 표시 | 브라우저 마이크 권한/연결 대기 |
| `listening` | 마이크·파장·입력 음량 막대 | 듣는 중. 무음이면 막대 0, ‘말 마침’으로 조기 전송 |
| `transcribing` | 시계·‘말씀을 확인하고 있어요’ | 마이크 종료 후 파일을 STT에 전송 |
| `thinking` | 시계·‘답변을 생각하고 있어요’ | 같은 검색·대화 API 사용, 마이크 꺼짐 |
| `preparing` | 시계·‘답변을 준비하고 있어요’ | TTS 준비, ‘답변 멈추고 말하기’로 중단 가능 |
| `speaking` | 스피커·‘온담이 이야기하고 있어요’ | Web Audio 재생, 마이크 꺼짐 |
| `paused` | 일시정지·‘잠시 멈췄어요’ | 자동 재청취 없음. ‘대화 시작’으로 재개 |

브라우저 수동 점검에서는 다음을 확인한다.

1. 처음에는 글로 대화하며, 모드 선택 전 마이크 권한을 요청하거나 소리를 재생하지 않는다.
2. 말로 전환해 실제 입력에 막대가 반응하고 무음에서 낮아지는지 확인한다.
3. 답변 중 스피커로 바뀌고, 재생 완료 후 마이크로 돌아오는지 확인한다.
4. ‘잠시 멈춤’·텍스트 전환·탭 이탈 후 파장·입력 막대·마이크 사용이 멈추는지 확인한다.
5. 마이크 거부·STT 실패는 안내하고, 글로 전환해 대화를 계속할 수 있어야 한다.
6. 화면의 굵은 글씨·출처는 유지하되 음성은 서식 기호·인용 번호·출처 목록을 읽지 않아야 한다.
7. 320/390/768/1440 화면 폭·큰 글씨·동작 줄이기에서 상태와 버튼을 구분할 수 있어야 한다.
8. 20~30개 이상의 긴 메시지와 원문을 펼쳐도 모드·입력·말 마침·멈춤이 화면에 남고 메시지와 겹치지 않아야 한다.
9. 과거 메시지를 읽는 중 응답을 지연시켜 새 답변 안내·스크롤·포커스 유지와 최신 이동을 확인한다.
10. 왼쪽 메뉴·상단의 설정창 열기/닫기·글자 크기·음성 일시정지·포커스 복원을 점검한다.
11. 가상 키보드의 높이·offset 변화, 250~400px 낮은 사용 영역, 큰 글씨·여러 줄 입력과 홈 영역 여백을 확인한다. 300px 미만에서는 보조 이용 안내를 숨기며 키보드를 닫으면 복원한다. 자동 viewport 모의 검증이 실제 아이폰·갤럭시 키보드 검증을 대신하지는 않는다.
12. 같은 화면 높이에서 글로/말로를 반복 전환하고 권한 대기·듣기·답변 준비·말하기·일시정지 사이의
    모드 버튼 좌표·크기가 동일한지 확인한다. 키보드가 보이는 동안에는 가시 화면 하단을 기준으로 한다.
13. 모바일·PC에서 화면보다 긴 답변에 여러 출처·의료 주의·공식 자료 카드를 붙여 본문 시작이 먼저
    보이는지 확인한다. 키보드·화면 크기 변화 뒤에도 출처만 보이는 위치로 내려가면 안 된다.
14. 답변 중간이나 과거 대화를 읽으면 위치·포커스를 유지하고, ‘새 답변 보기’는 새 답변 시작을 표시한다.
    공식 자료 펼치기·새 대화·주제 메뉴·글로/말로 전환도 각각 읽기 위치와 고정 버튼을 점검한다.
15. 320/360/384/402/430px 폭·큰 글씨·가장 긴 건강 주제명에서 선택창과 원문 보기 버튼의
    세로 좌표가 같고 가로 넘침이 없는지 확인한다. 선택창 글자 크기는 모바일에서 16px 이상으로 유지한다.

의료 답변의 기본 화면에서 `.medical-caution`과 `.message-notice`가 동시에 나오지 않는지,
긴 WHO 고지·이용조건 링크가 대화 영역에 없고 출처 링크는 유지되는지 확인한다.
‘이용 안내’를 열면 인용한 자료의 전체 고지와 라이선스 링크가 있고 같은 고지가 중복되지 않아야 한다.
공식 원문 카드는 유지하며 닫혀 있을 때 불필요한 원문 조회를 시작하지 않는다.
이 표시 변경만으로 RAG 재수집·재임베딩·인덱스 발행이나 모델 프롬프트 재평가는 필요하지 않다.

### 검증 결과와 한계

검색·답변은 구어체·질문 범위·부모 문맥·한국어 용어를 보강한 `v8` 처리 보고서,
음성은 2026-11-11 측정 보고서의 수치다.
프론트엔드 상태·취소·접근성 검증은 코드 변경에 맞춰 실행하며 영구적 품질 보장이 아니다.

| 평가 | 결과 | 보고서 |
| --- | --- | --- |
| 고정 40질문 검색 | 키워드 Hit@6 24/40, hybrid 39/40, hybrid MRR@6 0.890. 필수 원문 24질문 모두 적중 | `retrieval-evaluation-final.json` |
| 동일 검색어·벡터의 v7/v8 비교 | 40질문 적중 38/40 → 39/40, MRR 0.846 → 0.890. 자연스러운 한국어 8문항은 8/8 유지 | `paired-retrieval-comparison.json` |
| 한·영 대응 질문 | 자연스러운 한국어 8개와 같은 뜻의 영어 8개 모두 기대 근거 1위. 의료 번역·의학 정답률이 아님 | `bilingual-retrieval-evaluation.json` |
| 본인/보호자·의료·복지·129·질병관리청 | 31개 답변의 개념·인용·유형·표현 검사 통과, 개인별 철분제 복약 질문 1개는 제공자 안전 거절. 합계 32개 경계 검사이며 32개 의학 답변 성공이 아님 | `audience-evaluation.json` |
| 설명 깊이 | 간단한 연락처·복합 질문·자세한 단계/예시 요청 3개 검사 통과 | `answer-detail-evaluation.json` |
| 텍스트·음성 상태 전이 | 모드 전환·자동 재청취·취소·입력 막대·상태 표시·읽기 경로 | `tests/test_frontend.py` |
| 대화 조작부·설정 | 스크롤 추적·지연 응답·화면 높이·dialog 일시정지/포커스·DOM 구조 | `tests/test_frontend.py`, `tests/test_layout.py` |
| 음성 인식 | 62세 1화자·6발화·34.755초, CER 4/77=5.19%, WER 6/28=21.43% | `speech-evaluation.json` |

평가 JSON 보고서는 `data/processed/expanded/`에 있다. 프론트엔드 테스트 결과는 pytest 출력으로 확인한다.
검색 적중률은 의학적 정답률이 아니며 생성식 검색어 확장은 실행마다 변동할 수 있다.
답변 평가는 합성 시나리오의 표현·인용·길이 회귀 검사이며 임상 검증이나 실제 어르신 사용성 검증이 아니다.
의료인 검토는 미실시이며 보고서의 `clinical_review_status`도 `not_performed`다.
검토자는 [평가 질문](../data/audience-evaluation.json)과 보고서의 답변·원문을 대조해
사실 정확성·누락·한국 적용 가능성·응급 위험 신호·복약 변경 방지·이해 가능성을 각각 판단해야 한다.
노쇠·근감소·복합질환은 WHO의 확인한 본문으로 보강했지만 해당 분야의 전용 진단 지침 전체는 아니다.
의약품은 9성분의 선택 라벨이며 모든 약물의 용량·금기 판단을 완비한 자료가 아니다.
검토되지 않은 도구·수치 관계·개인별 용량 판단은 자동 검사 통과와 무관하게 검토 대기다.
같은 검색어·벡터로 비교하지 않은 생성식 검색어 확장 결과의 차이를 데이터 개선 효과로 단정하지 않는다.
설명 수준은 질문의 복잡도와 요청에 맞춰 평가한다. 기본 글자 수 상한은 없으며,
`max_characters`를 지정한 시나리오에만 길이 상한을 적용한다.
같은 자료에 대한 단순 연락처·복합 질문·자세한 단계/예시 요청은 다음과 같이 점검한다.

```bash
uv run python -m silvercare.evaluate --audience \
  --queries data/answer-detail-evaluation.json \
  --output data/processed/expanded/answer-detail-evaluation.json
```

프롬프트 변경은 기존 답변 평가의 `prompt_sha256`을 무효화하므로, 배포 릴리스와 정합성을 확인할 때
현재 corpus·검색 설정으로 답변 평가를 다시 실행한다. 설명 정책 변경만으로 문서를 재임베딩하지 않는다.
음성은 1화자의 편의 표본이며 70~90대·다른 지역·소음 환경을 대표하지 않는다.
음성 정규화는 Unicode NFKC·소문자화·구두점 제외이며, CER은 공백을 제외하고 WER은 공백 단위를 사용한다.

AI Hub 86 연령 코드의 근거는 `data/raw/aihub/86/data-guide.pdf` 10쪽,
SHA256 `86341d7119995d26bdb2ad35992292471891123be9267d59dc24700aa7501617`이다.
원본·음성·서명된 URL·키를 공개 결과물이나 로그에 포함하지 않는다.

## 7. 운영 파일

| 경로 | 용도 |
| --- | --- |
| `data/raw/aihub/registry.json` | 다운로드 파일의 크기·hash·확인 시각 |
| `data/processed/external-knowledge.jsonl` | 수집 원천별 외부 지식 스냅샷 |
| `data/processed/external-collection-report.json` | 마지막 수집 결과. 모든 서비스의 누적 상태는 아님 |
| `data/raw/clinical/` | WHO/VA PDF·정책, FDA HTML, 후보 라벨·Drugs@FDA 조회 원본 |
| `data/processed/clinical-knowledge.jsonl` | 전문 근거 최종 청크 591개, 원문·독자·위험·이용조건 보존 |
| `data/processed/clinical-structured.json` | FDA 표와 9성분 원문 절·표 HTML·셀/병합 구조·신청 정보 |
| `data/processed/who-icope-adapted.jsonl` | 별도 CC BY-NC-SA 적용 범위인 WHO 가공 부분 |
| `data/processed/clinical-collection-report.json` | 비상업적 이용 확인, 소스별 결과·누락 영역·산출물 hash |
| `data/processed/expanded/inventory.json` | corpus별 문서 수·태그 |
| `data/processed/expanded/embeddings.sqlite3` | 문서 임베딩 캐시 |
| `data/processed/expanded/publish-report.json` | 발행 대상·문서 수·hash |
| `data/processed/expanded/release.json` | 검증한 검색 설정 |
| `data/processed/expanded/paired-retrieval-comparison.json` | 동일 검색어·벡터의 이전/신규 인덱스 비교, 의학 정답률 아님 |
| `data/processed/expanded/bilingual-retrieval-evaluation.json` | 한·영 대응 자연 질문의 실제 원문 검색 검사 |
| `data/processed/expanded/retrieval-public-verification.json` | 운영 v8의 API 연결·자료 유형·용어·일상 대화·문맥 결합 검증 |
| `data/processed/kdca-content-catalog.csv` / `.json` | 인증키 없는 공개 콘텐츠 목록 |
| `data/processed/kdca-api-verification.json` | API 표본의 인증·본문 유무·hash |
| `.runtime/deployment.json` | 배포 URL·이미지·클러스터 |
| `.runtime/kubeconfig` | 관리 접근 정보. 비공개·Git 제외 |

원본·처리 파일·런타임 상태는 [.gitignore](../.gitignore)로 제외한다.
첨부된 API 가이드는 제공자의 원본으로 취급하고 편집하지 않는다.
앱 동작이나 데이터를 바꾸지 않는 문서 갱신에는 재수집·재임베딩·재배포가 필요하지 않다.

## 8. Git 업로드 전 확인

프로젝트 폴더의 `.gitignore`는 다음을 제외한다.

- `.env`와 환경별 설정, 개인키·kubeconfig·Azure 로컬 인증 상태
- 원본·정제본·임베딩 DB·런타임 배포 상태·음성·압축 다운로드
- 제공처에서 받은 `docs/`의 HWP·Word 가이드 원본
- Python 가상환경·캐시·테스트/빌드 결과·로그·임시 파일

`.env.example`, `uv.lock`, 소스·테스트·인프라 템플릿·Markdown 문서,
`data/catalog.json`·manifest·직접 작성한 안내·평가 시나리오는 업로드 대상이다.
원본 API 가이드는 Git 복제본에 포함되지 않으므로 제공처에서 내려받아 로컬에서 참고한다.

커밋·푸시 전에 실제 추적·업로드 대상을 확인한다. `main` 반영 전에는 6절의 전체 검증도 실행한다.

```bash
git status --short --ignored
git ls-files --others --exclude-standard
git diff --cached --stat
git diff --check
git diff --cached --check
```

`.gitignore`는 이미 추적 중인 비밀값이나 커밋 이력을 삭제하지 않는다.
강제 추가나 전역 ignore 예외를 적용한 뒤에는 제외 여부를 다시 확인한다.
비밀값을 실제 커밋한 경우에는 파일 제거뿐 아니라 키 교체와 이력 정리가 필요하다.
테스트·패키지 빌드가 통과해도 모든 취약점·데이터 재사용 조건을 보증하는 것은 아니다.
