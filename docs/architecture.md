# 온담 서비스 아키텍처

코드·운영 절차 검토일: 2026-11-18. 자료·측정 시점은 해당 보고서를 따른다.
이용자 안내는 [이용 가이드](user-guide.md), 자료별 사용 방식은 [RAG·API](rag.md),
배포·장애 대응은 [운영 가이드](operations.md)에 있다.

## 설계 원칙

| 원칙 | 서비스에 적용한 방식 |
| --- | --- |
| 이용자 중심의 공통 화면 | 텍스트를 기본으로 하고 선택형 음성·큰 글씨·고정 대화 조작부 제공 |
| 근거와 생성의 구분 | 사실 근거·대화 사례·실행 API를 구분하고 실제 인용한 출처만 표시 |
| 명시적인 처리 한계 | 진단·처방·자격 심사를 하지 않고, 외부 의존성 실패를 정상 답변으로 숨기지 않음 |
| 서비스별 변경 영향 제한 | 실행 프로필별 전용 노드·관리 ID·모델·검색·음성과 교차 접근 제한 |
| 개인정보 최소화 | 탭 메모리의 제한된 이력 사용, 서버 상담 이력 미보관, 외부 처리와 익명화 한계 안내 |
| 운영 상태와 검증 기록의 분리 | 현재 설정은 운영 상태로 관리하고 과거 성능·품질 결과는 측정 조건과 함께 별도 보관 |

## 1. 서비스 구조

```text
어르신·보호자 브라우저
  │ HTTPS, 텍스트 모드·선택형 음성 대화
  ▼
AKS LoadBalancer → Caddy → FastAPI (기본 접속 경로)
                           ├─ Azure AI Search: 지식·사례 하이브리드 검색
                           ├─ Foundry Responses: 검색어 확장·답변 생성
                           ├─ Foundry Embeddings: 질문 벡터
                           ├─ Speech: 음성 인식·합성
                           └─ 질병관리청 API: 건강정보 본문

운영자 로컬 작업
  AI Hub 파일 / 공식 문서 / 복지·기관 API
    → 스키마별 정제·분류·평가 분할
    → 문서 임베딩·발행
    → 지식·사례 Search 인덱스
```

파일 다운로드·외부 자료 수집·문서 임베딩은 운영자 명령으로 실행하는 작업이다.
앱 시작·사용자 질문·앱 재배포 시 전체 corpus를 다시 수집하거나 임베딩하지 않는다.
자동 수집 스케줄러·CronJob은 구현되어 있지 않다.

### 서비스 실행 구성

기본 이용 주소는 `/`다. 같은 코드와 화면을 사용하는 다른 실행 구성은 별도 경로로 분리한다.
아래 표는 운영·개발자를 위한 연결 정보이며 이용자에게 모델 선택을 요구하는 화면은 제공하지 않는다.

| 구성 | 공개 경로 | 내부 프로필 | 기능 범위 |
| --- | --- | --- | --- |
| 기본 서비스 | `/` | `azure` | GPT + Azure Search·공식 건강정보·음성 |
| Qwen + Azure 검색 | `/qwen-rag/` | `qwen-azure-rag` | Qwen + Azure Search·공식 건강정보·음성 |
| Qwen + 자체 검색 | `/local-rag/` | `local-rag` | OpenSearch·CPU BGE의 품질·성능 검증, 공식 건강정보·음성 |
| Qwen 단독 | `/local-only/` | `local-only` | 검색·공식 건강정보 없이 학습 지식으로 답변, 음성 |

자체 검색 구성은 새로운 질문에서 재정렬 시간이 길 수 있다.
병행 실행 경로의 존재를 동일한 품질·응답 시간 보장이나 임상 검증으로 해석하지 않는다.
구성별 한계와 측정값은 [평가 보고서](comparison-results-2026-11-16.md)에 있다.

```text
같은 Caddy HTTPS 호스트
 ├─ /             → azurecpu → 전용 GPT·임베딩·Azure AI Search·Speech
 ├─ /local-rag/   → localmem → 전용 Foundry gateway → 전용 Qwen 생성·임베딩
 │                                   ├─ OpenSearch BM25/HNSW → RRF → BGE 재정렬
 │                                   └─ 질병관리청 실행 API
 ├─ /local-only/  → onlycpu → 전용 gateway·Qwen 생성·Speech
 │                                   검색·사례·임베딩·질병관리청 조회 없음
 └─ /qwen-rag/ → qwazcpu → 전용 gateway·Qwen 생성·검색어 확장
                      + Azure 임베딩·AI Search·semantic 재정렬·Speech·질병관리청 API
```

`azure`, `local-rag`, `local-only`, `qwen-azure-rag`는 코드 복사본이 아닌 설정 프로필이다.
4번의 공개 `BASE_PATH`는 `/qwen-rag`이며 내부 프로필·Deployment·Service 이름은 변경하지 않는다.
기존 `/qwen-azure-rag` 공개 경로와 호환 리디렉션은 제공하지 않는다.
각 앱의 HTML·이미지·JavaScript·API는 해당 URL 접두사 안에서 동작한다.
앱은 생성 모델·검색 연결 오류 시 다른 생성·검색 공급자로 우회하지 않는다.
모델 단독도 일반 학습 지식으로 답하도록 하며, 검색 근거가 없다는 이유로 답변을 강제로 막지 않는다.
출처를 생성하지 않고 모델 지식 기반임을 표시하며 공통 응급·의료·개인정보 방침을 유지한다.

`local-*`는 내부 프로필의 식별자이며 실제 오프라인·휴대폰 실행을 뜻하지 않는다.
생성·임베딩은 Foundry 호스팅, 재정렬과 검색은 AKS 실행이다.
`qwen-azure-rag`는 검색·재정렬도 Azure AI Search를 사용하므로 실제 오프라인 RAG가 아니다.
서비스별 관리 ID는 자기 계정의 추론·Speech와 자기 Search의 문서 읽기만 허용한다.
NetworkPolicy는 같은 프로필 내부 통신과 공통 Caddy의 앱 접근, DNS·공개 HTTP(S)만 허용한다.
다른 프로필의 내부 gateway는 접근할 수 없고, Azure 자원은 별도 RBAC로 교차 접근을 차단한다.
기본 구성과 Qwen + Azure 검색 구성은 각각의 전용 Search에 같은 지식·사례 corpus를 사용한다.
두 구성의 512차원 인덱스와 자체 검색의 1024차원 인덱스는 벡터를 혼용하지 않는다.
클라우드 모델 정밀도와 휴대폰 양자화 모델, 서버와 휴대폰의 실행 자원은 동일하지 않다.

평가용 추적은 서버 기능 플래그와 별도 토큰으로 승인된 `/api/chat` 요청에서만 응답에 추가한다.
일반 응답 형식은 유지하며 원문 질문·검색 문맥을 서버 파일이나 일반 로그에 상시 기록하지 않는다.
추적은 요청 단위로 생성되어 평가 클라이언트가 소유자 전용 파일로 저장한다.
응급 고정 분기는 검색·생성보다 먼저 실행되며 의료 응급과 자해 위기 안내를 구분한다.
증상 표현의 부정·가정·지속 기간과 새로 발생하거나 악화된 상태를 구분한다.
변화 없는 선천적·장기적 상태와 연결된 가정문은 현재 응급상황으로 단정하지 않으며,
분류에서 제외됐다는 사실이 의료적으로 안전하다는 뜻은 아니다.
최근 갑작스러운 신경학적 이상이 호전된 경우나 명확한 현재 위험 신호는 즉시 도움 요청을 유지한다.
말·효과적인 기침이 불가능한 기도폐쇄, 심한 호흡 불능, 명확한 급성 시력 소실 표현을 처리한다.
현재 환자가 없는 수업·학습 문맥을 구분하며 단순 시야 흐림을 일괄적으로 고정 응급 판정하지 않는다.
처치 이름만 있는 개요를 근거로 신체 위치·동작·수치·절차를 생성하지 않는다.

## 2. 리소스

| 역할 | 리소스·설정 | 소유 범위 |
| --- | --- | --- |
| 서비스 리소스 그룹 | `rg-silver-care`, Korea Central | AKS·ACR·검색·관리 ID |
| AKS 기반 시설 | `aks-silvercare`, `system` D2ds_v5 2개 | 공통 Caddy·DNS·클러스터 기반 시설 |
| 컨테이너 레지스트리 | `silvercare2a5aalbg7ugfc` | ACR Basic |
| 앱 관리 ID | `silvercare-v1` ~ `silvercare-v4` | 서비스별 Workload Identity·별도 ServiceAccount |
| 1번 | `azurecpu` D2ds_v5 1개, `ondam-v1-e4b60458` | GPT-6.1-Terra·512차원 임베딩·Speech |
| 1번 검색 | `silvercare-v1-e4b60458` | Basic, replica 1 / partition 1, semantic Standard |
| 2번 | `localmem` D8ds_v5 1개, `ondam-v2-e4b60458` | Qwen 생성 A100 1개 + 임베딩 A100 1개·Speech |
| 3번 | `onlycpu` D2ds_v5 1개, `ondam-v3-e4b60458` | Qwen 생성 A100 1개·Speech, 검색·임베딩 없음 |
| 4번 | `qwazcpu` D2ds_v5 1개, `ai-foundry-m098047` | Qwen 생성 A100 1개·512차원 임베딩·Speech |
| 4번 검색 | `silvercare-rag-2a5aalbg7ugfc` | Basic·semantic Standard |
| HTTPS | Caddy·공개 IP·영속 인증서 디스크 | LoadBalancer의 80/443 |
| 비교 생성 | 각 계정의 `ondam-qwen-4b`, Qwen3.5-4B version 3 | 독립 GlobalManagedCompute, 각 A100 80GB 1개 |
| 비교 임베딩 | 2번 계정의 `ondam-qwen-embedding`, Qwen3-Embedding-0.6B version 2 | GlobalManagedCompute, A100 80GB 1개 |
| 비교 검색·재정렬 | OpenSearch 3.3.2, BGE-reranker-v2-m3 FP32 ONNX | 별도 `silvercare-local` namespace, ClusterIP |
| 2번 검색·재정렬 노드 | `localmem`, Standard_D8ds_v5 1개, 8 vCPU / 32GiB | 다른 서비스는 배치하지 않음 |

공유 Foundry 계정의 전역 정책·다른 프로젝트 학습을 앱 배포에서 수정하지 않는다.
`GlobalStandard`는 계정 위치와 추론 처리 위치가 같다고 보장하지 않는다.
Qwen 생성·임베딩 모델은 Serverless가 아닌 **Managed-compute 탭**에서 확인한다.
네 A100 배포는 시간 단위 과금이며 휴대폰급 자원 제한을 적용한 측정 환경이 아니다.
전용 노드는 label·taint·nodeSelector로 분리한다. HTTPS 진입점·클러스터 기반 시설·ACR,
질병관리청 외부 API·서비스키·상위 구독/공급자 한도는 공통이다.
별도 모델 배포·할당량을 Azure 공급자 내부의 전용 물리 서버 보장으로 해석하지 않는다.
재정렬의 8192-token 문맥과 비절단 처리를 유지하기 위해 32GiB 노드를 사용한다.
노드 축소 시 전체 문맥·최대 배치의 메모리를 별도 검증하며, 유휴 RSS만으로 용량을 정하지 않는다.

### 템플릿과 활성 설정

- [main.bicep](../infra/main.bicep): AKS·ACR·관리 ID와 **Free 검색**을 만드는 기본 템플릿이다.
- [search-rag.bicep](../infra/search-rag.bicep): 운영 corpus 용량에 사용하는 **Basic 검색** 템플릿이다.
- [service_isolation.py](../silvercare/service_isolation.py): 전용 연결·canary·이전·경계 정책·이미지 업데이트를 관리한다.
- [deploy.py](../silvercare/deploy.py): 초기 단일 서비스 배포 도구다. 로컬 kubeconfig가 없어도
  기존 프로젝트 클러스터를 먼저 조회해 자격 증명을 준비하고, 격리 marker를 확인하기 전에는 인프라를 변경하지 않는다.
  이미 격리한 환경의 공유 배포는 차단한다.
- `data/processed/expanded/release.json`: 사용할 검색 endpoint·인덱스·검색 기능 설정이다.
- `.runtime/service-isolation-state.json`: 전용 연결·기존 설정·이미지·canary 검증의 현재 운영 상태다.
- `.runtime/deployment.json`: 격리 이전 단일 서비스 배포 기록이며 현재 연결의 기준으로 사용하지 않는다.

기본 템플릿의 Free 검색을 운영 검색으로 오해하지 않는다.
운영 인덱스는 `silvercare-knowledge-v9`, `silvercare-cases-v8`이며 앱은 릴리스 설정으로 선택한다.
변경 없는 사례는 모든 문서 필드의 일치를 검증해 쓰기 없이 재사용한다.
`PREFER_OFFICIAL_REFERENCES=true`는 공식 안내 후보 2개를 우선 확보하고 실행 API 근거를 먼저 배치한다.
`NONCOMMERCIAL_DATA_USE=true`는 운영자가 확인한 WHO·한국 기본소생술 지침의 비상업적 이용을 기록한다.
이 확인 없이 해당 문서를 발행하거나 검색 결과를 생성 모델에 전달할 수 없다.
`CONTEXTUAL_RETRIEVAL=true`는 v8 메타데이터 기반 검색·부모 문맥 결합·명시적인 일상 대화의
검색 생략을 켠다. 기본값은 `false`이며 이전 인덱스에서 켜지 않는다.
별도 Free 검색의 파일럿 인덱스는 운영 답변에 사용하지 않는다.
이미지 태그와 인덱스 이름은 서로 독립적이다.

## 3. 모듈별 책임

| 파일 | 책임 |
| --- | --- |
| [app.py](../silvercare/app.py) | FastAPI·수명주기·라우트·출처 검증·요청 제한·오류 응답 |
| [azure.py](../silvercare/azure.py) | 현재 시스템 지침·검색어 확장·질문 임베딩·검색·API 근거 결합·생성·인용 검증·음성 |
| [local_services.py](../silvercare/local_services.py) | 공통 Qwen 호출 코드, 전용 배포를 사용하는 Azure Search RAG·OSS RAG·모델 단독 프로필 |
| [foundry_gateway.py](../silvercare/foundry_gateway.py) | Workload Identity로 관리형 Qwen 추론, 응답 정규화·정확한 임베딩 토큰 한도 검증 |
| [offline_search.py](../silvercare/offline_search.py) | OpenSearch 필터·BM25/HNSW·RRF·재정렬·부모 문맥 조회 |
| [cpu_reranker.py](../silvercare/cpu_reranker.py) | 고정 BGE CPU 실행·길이순 비절단 batch·중복 score cache·모델 artifact 검증 |
| [offline_ingest.py](../silvercare/offline_ingest.py) | 준비된 지식·사례의 신규 OpenSearch 인덱스 발행·holdout·벡터·수량 검증 |
| [deploy_comparison.py](../silvercare/deploy_comparison.py) | 비교 namespace·배포·Foundry 모델 명세·기존 경로를 보존하는 Caddy 설정 |
| [health.py](../silvercare/health.py) | 21개 허용 주제·주제 연결·질병관리청 XML·TLS·캐시·근거 문단 선별 |
| [models.py](../silvercare/models.py) | 대화·출처·공식 건강정보의 요청·응답 타입 |
| [speech_text.py](../silvercare/speech_text.py) | 화면 답변을 읽기용 텍스트로 변환·출처 목록/서식 기호 제외 |
| [config.py](../silvercare/config.py) | 앱/수집 설정·SecretStr·환경변수 호환·운영 가드 |
| [safety.py](../silvercare/safety.py) | 응급 표현의 고정 응답·AI/건강정보 안내 |
| [privacy.py](../silvercare/privacy.py) | NFKC·공백 정리·개인정보 패턴 최소화 |
| [archives.py](../silvercare/archives.py) | 제한된 TAR/ZIP 읽기·part 조립·동일 중복 확인 |
| [data.py](../silvercare/data.py) | 원천 스키마·문서 타입·정제·검색 스키마·개별 파일 CLI |
| [corpus.py](../silvercare/corpus.py) | 등록된 AI Hub 파일의 다운로드·receipt·준비 CLI |
| [external_sources.py](../silvercare/external_sources.py) | 공식 문서·XML·복지/기관 API 수집·출처·부분 실패 보고 |
| [clinical_sources.py](../silvercare/clinical_sources.py) | WHO·VA·FDA·openFDA의 제한된 수집·판본/권한 확인·표/라벨 구조 보관 |
| [targeted_sources.py](../silvercare/targeted_sources.py) | 성인 기도폐쇄·영양제·저혈당·머리 부상의 고정 절 선택·재사용 조건 확인·범위 보존 |
| [retrieval.py](../silvercare/retrieval.py) | 원천 분류·보수적 질문 범위·약명 별칭·부모 문맥 결합·근거에 등장한 용어 대응 |
| [expanded.py](../silvercare/expanded.py) | 지식·사례·holdout 구성·임베딩 캐시·버전 인덱스 발행·릴리스 검증 |
| [evaluate.py](../silvercare/evaluate.py) | 검색·실제 답변·음성 평가 |
| [static/](../silvercare/static/) | HTML·CSS·JavaScript·화면 이미지 |

## 4. 온라인 HTTP 인터페이스

| 경로 | 동작 |
| --- | --- |
| `GET /health/live` | 앱 프로세스 응답. 모든 외부 의존성의 종합 상태는 아님 |
| `GET /health/ready` | 비교 앱은 gateway·검색 인덱스·재정렬 의존성 점검. 실제 Foundry 추론 성공은 별도 검증 필요 |
| `GET /api/config` | 기능 활성 여부. 모델명·인증키는 반환하지 않음 |
| `POST /api/chat` | 검색·질병관리청 근거를 이용한 답변 |
| `GET /api/health-topics` | 설정된 21개 건강 주제. API 키가 없으면 빈 목록 |
| `GET /api/health-info/{content_id}` | 허용된 건강 주제의 원문 텍스트 발췌 |
| `POST /api/speech/transcribe` | 음성 인식, 최대 4MB |
| `POST /api/speech/synthesize` | 음성 모드/수동 듣기의 읽기용 텍스트 변환·음성 합성 |

일반 요청 본문은 196,608바이트, 음성 업로드는 4MB와 multipart 여유분으로 제한한다.
익명 공개 서비스이며 사용자 진료 기록 관리나 자동 신고 시스템이 아니다.

### 화면과 대화 상태

앱 셸은 화면 높이에 맞고, `conversation-scroll`과 `conversation-dock`은 서로 다른 grid 행이다.
환영·주제·메시지·출처는 `conversation-scroll`에서 스크롤하며 모드 선택·입력·음성 상태·조작은 하단에 남는다.
입력창·음성 상태·오류·재생 영역은 `dock-content`에 두고, 그 아래 모드 선택과 공통 이용 안내는
별도의 고정 행으로 둔다. 모드 전환이나 음성 처리 상태로 내용 높이가 바뀌어도 선택 버튼의 위치·크기는 유지한다.
낮은 화면에서 내용이 넘치면 `dock-content`만 스크롤하며 모드 선택은 남는다.
고정 요소를 메시지 위에 덮는 방식이 아니므로 메시지 영역과 조작부가 겹치지 않는다.
`visualViewport`의 높이·offset과 safe-area를 적용하며 가상 키보드가 열리면 조작부 높이와 위치를 조정한다.
작은 화면이나 낮은 창에서는 보조 설명과 입력창 최대 높이를 줄이고 필수 모드·입력·멈춤 버튼을 유지한다.
사용 가능 높이가 300px 미만이면 하단의 보조 이용 안내를 숨겼다가 높이가 복원될 때 다시 표시한다.
AI 주의·개인정보 상세 안내는 별도 이용 안내에 있고 답변 자체의 의료·응급 안내는 줄이지 않는다.

데스크톱의 왼쪽 ‘화면 설정’ 또는 작은 화면의 상단 설정 버튼이 `screen-settings` dialog를 연다.
화면 크기에 따라 하나의 설정 진입점만 표시하며 오른쪽 패널은 돌봄 도움말에 집중한다.
글자 크기와 음성 이용 방법을 확인할 수 있으며 창이 열리면 음성 대화를 일시정지한다.
닫을 때는 이전 메뉴·호출 버튼의 포커스를 복원하고 마이크를 자동 재시작하지 않는다.
화면 설정과 별도의 이용 안내·119 안내도 대화 이력을 지우지 않는다.
좁은 화면에서는 데스크톱 보조 패널 대신 상단 설정·119 안내를 사용한다.

새 답변은 해당 메시지의 시작을 읽기 위치로 삼는다. 긴 답변의 출처·주의·공식 자료가 본문 아래에
붙어 있어도 페이지 맨 아래로 이동하지 않는다. 짧은 답변은 가능한 스크롤 범위 안에서 표시한다.
화면·키보드·입력창·메시지 크기가 변하면 레이아웃 반영 후 같은 답변 시작을 유지한다.
높이 변화 자체를 사용자의 과거 대화 탐색으로 취급하지 않는다.
터치·휠·키보드로 직접 읽는 위치를 옮기면 자동 위치 맞춤과 입력 포커스 이동을 멈춘다.
공식 자료를 펼칠 때도 읽기 위치를 유지하며 본문 시작이나 문서 끝으로 강제로 이동하지 않는다.
첫 방문·새 대화는 환영 위치를 유지하고 주제 메뉴는 자동으로 최신 대화로 되돌리지 않는다.
다른 대화를 읽는 동안 온 답변은 `new-answer` 버튼으로 안내하며 누르면 새 답변 시작으로 이동한다.
새 질문을 보내면 대기 메시지를 표시하고, ‘돌봄 대화’ 메뉴는 가장 최근 메시지의 시작으로 이동한다.
‘함께 알아보기’는 주제 상단을 대화 스크롤 영역에 맞추고 공식 건강정보 목록을 펼치며 제목·선택 메뉴를 바꾼다.
메뉴 클릭·`#topics` 직접 접속·hash 변경 모두 같은 위치 이동을 사용하며 대화 이력을 지우거나 질문을 자동 전송하지 않는다.
공식 건강정보 선택기는 좁은 화면에서 라벨을 위에 두고 주제 선택과 원문 보기 버튼을 같은 행에 배치한다.
선택 열은 가용 폭 안에서 줄어들며, 긴 주제명이나 큰 글씨 때문에 버튼이 다음 행으로 밀리지 않는다.

텍스트·음성 모드는 현재 브라우저 탭의 `state.history`를 공유한다.
최근 12개 이전 메시지만 현재 질문과 함께 전달하며, 화면에 보이는 모든 과거 메시지가 모델 입력에 남는 것은 아니다.
서버 세션 저장소·사용자별 장기 기억·대화 요약 메모리·다른 탭이나 기기와의 대화 동기화는 없다.
이 기능과 질병관리청의 1시간 문서 캐시는 서로 다른 목적의 메모리다.

음성 상태는 `voice.phase`로 관리하며 실제 마이크 입력·음성 처리·재생에 맞춰 아이콘·문구·버튼을 표시한다.
표현·시간 제한·취소 규칙은 [음성 모드 운영 기준](operations.md#텍스트음성-모드)을 따른다.

화면 답변은 인용·서식을 보존한다. 음성 합성은 `SpeechRequest`와
[spoken_text](../silvercare/speech_text.py)의 공통 변환을 거쳐 읽을 본문만 SSML로 전달한다.
음성용으로 별도의 LLM 요약을 만들지 않으며 TTS용 변환으로 화면 답변·대화 이력을 덮어쓰지 않는다.

## 5. 인증·개인정보·장애 경계

- 브라우저는 앱과 통신하며 Azure·질병관리청 키를 받지 않는다.
- 앱은 Azure 모델·검색·음성에 Workload Identity로 접근한다.
- 질병관리청 인증키는 `silvercare-health` Secret의 `secretKeyRef`로 주입한다.
- 수집용 AI Hub·복지·기관 키와 수동 로그인 정보는 배치 작업의 로컬 설정이다.
- 질문·최근 대화는 개인정보 패턴 처리 후 외부 검색/모델에 전달한다. 완전한 익명화를 보장하지 않는다.
- 질병관리청에는 콘텐츠 ID와 인증키만 보낸다. API 본문 발췌는 답변 모델에 전달한다.
- 질병관리청 호스트 전용 TLS 클라이언트는 인증서·호스트 검증과 TLS 1.2 이상을 유지한다.
- 모델·검색·필요한 공식 API 근거가 실패하면 명시적 오류를 반환한다. 실패를 무근거 정상 답변으로 대체하지 않는다.
- 응급 표현은 검색·모델·공식 API 호출 전에 고정 안내로 처리한다. 규칙 기반 감지는 모든 응급을 포괄하지 않는다.
- 텍스트 모드는 수동 전송이며, 음성 모드는 말끝·수동 완료 후 STT 결과를 같은 대화 API에 자동 전송한다. 인식 결과와 답변을 화면에 표시한다.
- 음성 모드는 한 사람씩 말한다. 마이크는 사용자 발화 시에만 열고 STT·답변 처리·TTS 중에는 닫는다. 답변 재생 완료 후 다음 입력을 듣는다.
- 모드 전환·설정/안내창·일시정지·새 대화·화면 이탈은 대기 중 권한·인식·재생·재청취를 무효화한다. 두 모드의 대화 이력과 자료 검색은 공유한다.

앱과 엣지는 non-root·읽기 전용 루트 파일시스템으로 실행한다.
NetworkPolicy와 Caddy의 프록시 설정, 요청 제한·동시 처리 제한을 사용한다.
단일 노드·앱·엣지·검색 replica 구성에는 고가용성·분산 rate limit·의료 정확성 보장이 없다.
