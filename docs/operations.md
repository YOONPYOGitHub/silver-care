# 실행·수집·배포·검증

코드·운영 절차 검토일: 2026-11-18. 자료·릴리스·측정 시점은 해당 보고서를 따른다.
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

## 네 가지 비교 서비스

기존 root URL의 앱 이미지는 `azure-baseline-20261116-3b2c666`으로 고정한다.
비교 앱은 `silvercare-local` namespace의 독립 Deployment·Service와
`/local-rag/`, `/local-only/`, `/qwen-rag/` 경로를 사용한다.
4번의 내부 프로필·Deployment·Service 이름은 `qwen-azure-rag`로 유지한다.
앱 ConfigMap의 `BASE_PATH=/qwen-rag`가 HTML·정적 파일·API 접두사를 결정한다.
기존 `/qwen-azure-rag` 공개 경로와 호환 리디렉션은 제공하지 않는다.
기존 배포 명령으로 비교 이미지를 root 앱에 배포하지 않는다.

### Foundry 모델과 비용

```bash
uv run python -m silvercare.service_isolation status
```

Foundry의 **Models → Deployments → Managed-compute**에서 확인한다.
Serverless 목록에 안 보이는 것은 배포 부재를 의미하지 않는다.
관리 API는 Cognitive Services 계정의 `managedComputeDeployments`,
`2026-03-15-preview`를 사용한다. 모델 URI는 버전 고정,
관리 API가 요구하는 deployment template URI는 `labels/latest` 형식이다.
2·3·4번의 Qwen 생성은 계정별 A100 80GB 1개씩, 2번의 Qwen 임베딩은 추가 A100 1개를 사용한다.
1번 GPT·OpenAI 임베딩은 별도 계정의 GlobalStandard 배포다.
사용하지 않는 비교 환경은 앱뿐 아니라 관리형 모델 컴퓨트도 비용 관점에서 점검한다.
4번의 기존 계정·Qwen 생성·Azure 임베딩·Search는 유지한다.
사용하지 않는 이전 공용 Qwen 임베딩 배포와 공용 gateway는 제거했다.
원래 Foundry의 다른 프로젝트용 GPT 배포는 삭제하거나 할당량을 줄이지 않았다.

생성은 native `/managed-deployments/{name}/v1/chat/completions`를 사용한다.
통합 `/openai/v1/chat/completions`는 비사고 모드용 `chat_template_kwargs`를 거절한다.
임베딩도 native `v1/embeddings`를 사용하며 gateway가 1024차원·순서·유한값·토큰 합계를 검증한다.
인증은 Workload Identity이며 Azure API 키를 새로 만들지 않는다.
gateway의 `/health`는 프로세스 상태이지 관리형 모델의 실제 준비 완료 증거가 아니다.
`Succeeded`와 실제 생성·임베딩 호출을 모두 확인해야 한다.

### AKS와 이미지

서비스별 자원과 관리 ID는 [리소스 표](architecture.md#2-리소스)를 따른다.
`azurecpu`, `onlycpu`, `qwazcpu`는 각각 D2ds_v5 1개이며 앱·gateway를 해당 서비스에만 배치한다.
2번 전용 노드 `localmem`은 `Standard_D8ds_v5` 1개(8 vCPU / 32GiB),
label `workload=local-ai`, taint `workload=local-ai:NoSchedule`다.
기존 system 노드 2개는 유지한다. OpenSearch 데이터는 8Gi PVC다.
기존 TEI cache 8Gi PVC는 롤백용으로 보존하며 최적화 runtime은 고정 이미지의 모델을 읽는다.
reranker는 requests 3Gi / limits 24Gi, OpenSearch는 heap 1Gi / limits 4Gi다.
같은 BGE FP32 가중치를 ONNX Runtime 1.23.2, 6개 intra-op thread로 실행한다.
유휴 thread spinning은 끄고 길이순 microbatch(최대 8개·padding 포함 8192 tokens)를 사용한다.
HTTP당 64개 후보를 모두 처리하며 16개 HTTP 요청만 대기시킨다.
중복 query/text pair는 SHA256 key와 score만 저장하는 process-local LRU(2048개)로 재사용한다.
공식 근거와 일반 근거 검색이 겹쳐도 후보나 출처를 버리지 않고 모델 평가만 중복 제거한다.
readiness `/info`는 추론 대기열 뒤에 다시 모델 계산을 추가하지 않는다.
전체 문맥 warm-up의 OOM을 피하기 위한 서버 설정이며 휴대폰 메모리 요구량 측정치가 아니다.
검색·재정렬은 Recreate 방식으로 업데이트해 단일 PVC·큰 warm-up 메모리의 중복 사용을 피한다.

```bash
az acr build --registry silvercare2a5aalbg7ugfc \
  --image silvercare:COMPARISON_TAG --file Dockerfile --target runtime --no-logs .
uv run python -m silvercare.service_isolation deploy-image \
  --profile qwen-azure-rag --component app \
  --image silvercare2a5aalbg7ugfc.azurecr.io/silvercare@sha256:VERIFIED_DIGEST
```

이미지는 임베딩 tokenizer도 원본 revision과 SHA256으로 검증해 포함한다.
로컬 gateway 실행은 같은 tokenizer를 준비하고 `FOUNDRY_EMBEDDING_TOKENIZER_PATH`를 지정한다.
`FOUNDRY_ENDPOINT`, `FOUNDRY_LLM_DEPLOYMENT`와 token scope를 설정한다.
`FOUNDRY_EMBEDDING_DEPLOYMENT`는 2번 gateway에만 설정한다.
임베딩이 없는 3·4번 gateway는 tokenizer를 읽지 않고 `/embed` 요청을 명시적으로 404 처리한다.
생성·임베딩 모델 가중치는 AKS에 설치하지 않는다. 재정렬 모델만 CPU 전용 이미지에 포함한다.
각 서비스는 전용 Speech 연결을 사용한다. 질병관리청 서비스키는 외부 API의 공통 예외다.
모델 단독 앱에는 KDCA·검색·임베딩 설정을 연결하지 않는다.

CPU 재정렬 이미지는 기본 앱 이미지와 다른 Docker target이다. 기본 target은 계속 앱이다.

```bash
az acr build --registry silvercare2a5aalbg7ugfc \
  --image silvercare:CPU_RERANKER_TAG --file Dockerfile --target cpu-reranker --no-logs .
uv run python -m silvercare.service_isolation deploy-image \
  --profile local-rag --component local-reranker \
  --image silvercare2a5aalbg7ugfc.azurecr.io/silvercare@sha256:VERIFIED_DIGEST
uv run --extra reranker pytest -q
```

기본 정밀도는 FP32이며 graph·external weights의 SHA256을 빌드 때 검증한다.
INT8 후보도 검증했지만 고정된 46개 후보군 평가에서 Hit@6은 같고
MRR이 0.637681→0.626812로 하락해 사전 기준(최대 하락 0.01)을 통과하지 못했다.
속도만 보고 기본 모델을 양자화하지 않는다. FP32 최적화는 같은 평가에서 Hit@6·MRR을 유지했다.
이 검사는 각 질문의 동결된 RRF 상위 12개 후보 안에서의 비교이며 전체 임상 품질 인증이 아니다.
TEI 롤백 manifest는 `.runtime/reranker-tei-rollback.json`에 별도로 보존한다.

### BGE GPU 검토 상태

현재 2번은 위 FP32 ONNX **CPU** runtime을 사용한다. Azure GPU로 전환하거나 새 endpoint를 배포하지 않았다.
같은 기반 모델의 Azure 카탈로그 자산
`azureml://registries/HuggingFace/models/baai-bge-reranker-v2-m3/versions/6`과
`tei-rerank-gpu-extra-small` GPU 엔진 version 1의 등록은 확인했다.
카탈로그 기본 SKU는 `Standard_NC4as_T4_v3`, 실행 경로는 Azure ML 관리형 endpoint의 `/rerank`다.
[모델 카드](https://ai.azure.com/catalog/models/baai-bge-reranker-v2-m3)를 참고한다.

2026-11-18 읽기 전용 확인에서 `Microsoft.MachineLearningServices`는 Registered였으나,
Korea Central의 **Azure ML 전용 T4 계열 vCPU 한도는 0**이고 프로젝트 workspace는 없었다.
Foundry Managed Compute 쿼터·일반 VM 쿼터·Azure ML 쿼터를 혼동하지 않는다.
SKU가 지원 목록에 있어도 구독 할당·실제 GPU 공급을 보장하지 않는다.
카탈로그 기본 revision `main`·CUDA image의 `latest` 태그는 고정된 현재 ONNX artifact와 다르다.
revision·tokenizer·정밀도·8192-token 비절단·점수 형식과 검색/의료 품질을 검증하기 전에
동등한 배포나 완료된 속도 개선으로 표시하지 않는다. 공유 시험 용량도 지속 운영 보장으로 간주하지 않는다.
AKS를 중지해도 별도 Foundry 모델 컴퓨트가 자동으로 중지되는 것은 아니므로 비용을 각각 확인한다.

### 전용 연결 유지와 이미지 업데이트

```bash
uv run python -m silvercare.service_isolation deploy-image \
  --profile qwen-azure-rag --component app \
  --image silvercare2a5aalbg7ugfc.azurecr.io/silvercare@sha256:VERIFIED_DIGEST
```

이 명령은 선택한 서비스의 이미지 하나만 바꾸며 node·관리 ID·연결 설정의 drift를 거부한다.
앱·gateway는 지정된 전용 ConfigMap 하나만 참조해야 한다. 참조 누락·다른 ConfigMap·Secret overlay와
보호된 연결값을 덮어쓰는 직접 `env`/`valueFrom`은 이미지 변경 전에 거부한다.
프로필은 `azure`, `local-rag`, `local-only`, `qwen-azure-rag`다.
`--component gateway`는 해당 프로필의 gateway만, `local-reranker`·`local-search`는 2번만 선택할 수 있다.
1번 기준 이미지도 변경 승인을 받은 경우에만 업데이트한다.
현재 기준 이미지와 프론트엔드는 자원 분리 과정에서 바꾸지 않았다.

클러스터의 `silvercare/silvercare-runtime-isolation` marker는 기존 단일/공유 배포 명령을 차단한다.
`deploy.py`, `deploy_comparison deploy`, `deploy-qwen-azure`, `deploy-reranker`, `models`로
공유 연결을 재생성하지 않는다. 전용 상태 파일은 0600 권한으로 보관·백업하며 Git에 넣지 않는다.
다른 운영 환경에서 이 파일 없이 snapshot을 새로 만들어 현재 연결을 덮어쓰지 않는다.
공유 변경 명령은 kubeconfig 없이 실행할 수 없다. 초기 `deploy.py`도 기존 프로젝트 클러스터의
자격 증명을 먼저 확보해 marker를 확인한다. 클러스터가 중지됐거나 인증·조회가 실패하면
보호 검증을 생략하지 않고 인프라 변경 전에 종료한다.
이전 공용 관리 ID의 두 AKS federated credential은 폐기했다.
NetworkPolicy는 다른 프로필의 내부 API·gateway·검색·재정렬 연결을 차단하고,
Azure RBAC는 다른 계정의 추론·검색 접근을 거부한다.
동일 호스트의 HTTPS 진입점·클러스터 기반 시설·ACR·외부 질병관리청 API와 상위 한도는 공통 예외다.

### 서비스와 평가 부하 분리

전체 비교의 의료 24건 ×4버전, 적용 가능한 retrieval 46건, audience 32건·detail 3건을 완료·보관했다.
중지했던 2번 backend 평가는 자원 격리 후 2번 전용 연결과 별도 D8ds_v5 평가 CPU에서 마무리했다.
평가 Pod와 임시 `evalcpu` pool은 삭제했다.
의료·1·3·4번 backend는 격리 전, 2번 backend는 격리 후 측정이다.
두 실행 조건과 공개 URL 지연을 섞어 하나의 성능 통계로 만들지 않는다.
실패·기준 미통과도 원본에 보존하며 실행 완료와 품질 통과를 구분한다.
수치와 한계는 [비교 보고서](comparison-results-2026-11-16.md)에 있다.
향후 평가 복제본은 버전별 전용 모델·검색·관리 ID만 사용해야 한다.
4번을 사용 중일 때 4번의 모델·검색에 전체 평가 부하를 다시 보내지 않는다.
공개 재정렬과 다른 process·node에서 평가하며, 별도 CPU 노드는 사용 후 삭제한다.

평가용 질문은 합성 데이터만 사용한다. 결과는 요청마다 checkpoint를 남기고 원시 결과를 보존한다.
공개 URL의 실제 카드 클릭 검증과 격리 환경의 전체 성능·품질 평가는 구분해 기록한다.
단순 `/health/live` 성공을 실제 답변 생성의 성공으로 간주하지 않는다.

### 문서 발행과 공개 순서

클러스터 내부의 임시 indexer에서 준비된 JSONL 두 개만 전달한 후 실행한다.
아래 파일 경로는 indexer에 복사한 위치를 뜻하며 원천 archive나 holdout은 전달하지 않는다.

```bash
python -m silvercare.offline_ingest \
  --endpoint http://local-search:9200 \
  --embedding-endpoint http://local-rag-foundry:8080 \
  --knowledge /tmp/data/processed/expanded/knowledge.jsonl \
  --cases /tmp/data/processed/expanded/cases.jsonl \
  --knowledge-index local-knowledge-v1 --case-index local-cases-v1 \
  --dimensions 1024 --batch-size 32 \
  --embedding-model Qwen/Qwen3-Embedding-0.6B \
  --embedding-revision azure-huggingface/version-2 \
  --output /tmp/ingest-report.json
```

발행기는 기존 인덱스를 덮어쓰지 않는다. 실패 시 부분 인덱스가 남으므로 새 이름으로 재시도하고,
검증된 인덱스 이름을 비교 ConfigMap과 맞춘다. 보고서에는 source hash·수량·모델 정보를 남기며
성공 수량은 지식 6,562 / 사례 6,762여야 한다. 기존 Azure release 파일은 변경하지 않는다.
기관 전화번호는 검증한 노령층 가이드의 발행기관 표시 유선번호만 보존한다.
본문·분할·벡터·bulk 응답·refresh·최종 count 검증이 실패하면 성공 보고서를 쓰지 않는다.

비교 앱의 실제 추론, hybrid 검색·출처, 모델 단독의 빈 출처·건강정보,
Speech와 URL 접두사 분리를 확인한 뒤에만 다음 명령으로 경로를 공개한다.

```bash
uv run python -m silvercare.deploy_comparison routes
```

Caddy 설정을 먼저 검증하고 원본을 `.runtime/comparison-baseline-edge.json`에 백업한다.
기존 root reverse proxy는 보존하며 비교 API/static만 각 서비스로 전달한다.
클라이언트 IP와 비교용 660초 upstream timeout을 별도로 전달한다.
기존 `/local-rag/`, `/local-only/` 경로가 있으면 그 블록과 root proxy를 보존하면서
`/qwen-rag/`만 추가하며 이전 주소의 리디렉션은 만들지 않는다.
기존 `/qwen-azure-rag/` 프록시가 있으면 알려진 블록만 교체하며 다른 형태이면 자동 변경하지 않는다.
`.runtime/qwen-azure-baseline-edge.json`에 추가 전 설정을 보관한다.
기존 배포의 주소를 바꿀 때는 실제 앱 ConfigMap의 `BASE_PATH`만 수정하고 해당 앱을 재시작한 뒤
Caddy 경로를 전환한다. 모델·검색 설정과 다른 앱 이미지는 변경하지 않는다.
요청한 경로가 모두 존재하면 재실행으로 덮어쓰지 않는다.
이후 앱 업데이트에는 전용 `service_isolation deploy-image`만 사용한다.
앱과 gateway는 긴 비교 요청을 배포 중 잘라 버리지 않도록 종료 유예 660초를 둔다.
인덱스 보고서와 비교 배포 상태는 별도 운영 파일로 보관하고 임시 indexer는 삭제한다.
초기 단일 질문 smoke에서 모델 단독은 약 6.6초, CPU 재정렬을 포함한 RAG는
복지 문의 약 86초, 낙상 문의 약 188초였다.
이는 특정 질문·서버 설정의 단발 측정이며 일반 성능·의학 정확도·휴대폰 속도를 나타내지 않는다.
버전별 설명 배너나 모델명은 화면 상단에 표시하지 않으며 요청 취소와 600초 비교용 제한은 유지한다.

프론트엔드만 수정하면서 평가 중인 backend를 그대로 유지하려면
`infra/Dockerfile.ui`의 `BASE_IMAGE`에 해당 버전의 검증된 이미지 digest를 지정한다.
이 target은 HTML·CSS·JavaScript만 교체한다. backend가 다른 앱의 base image를 혼용하지 않는다.

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
uv run python -m silvercare.targeted_sources --noncommercial-use

# 직접 작성한 기관 안내와 등록 corpus를 준비한다.
uv run python -m silvercare.data prepare \
  --manifest data/reference.json --output data/processed/reference.jsonl
uv run python -m silvercare.expanded prepare \
  --external data/processed/external-knowledge.jsonl \
  --clinical data/processed/clinical-knowledge.jsonl \
  --targeted data/processed/targeted-medical-knowledge.jsonl
```

`expanded prepare`에서 `--external`을 빼면 외부 지식이 준비 결과에 들어가지 않는다.
`--clinical`을 빼면 전문 근거 591개 청크도 들어가지 않으므로 운영 corpus 준비에는 둘 다 지정한다.
v9에서는 `--targeted`도 지정해야 성인 기도폐쇄·영양제·저혈당·머리 부상 근거 31개 청크가 포함된다.
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
지식 발행 대상은 `silvercare-knowledge-v9`, 사례는 변경 없는 `silvercare-cases-v8`이다.
새 지식 corpus는 사용하지 않는 버전 이름에 발행한다.
활성 사례는 모든 문서 필드가 준비본과 일치할 때만 읽기 전용으로 재사용하며,
불일치하면 지식 색인을 변경하기 전에 발행을 중단한다.
목표 이름을 바꾸는 CLI 옵션이나 자동 blue/green 이름 생성은 구현되어 있지 않다.

아래는 v8에서 지식 v9·사례 v8로 전환하는 절차다.
이미 활성화된 지식 v9를 다시 덮어쓰는 절차가 아니며, 다음 발행에는 새 이름과 용량 검토가 필요하다.

1. 지식 발행 상수를 미사용 이름으로 지정한다. 사례를 변경했다면 별도 미사용 사례 이름도 필요하다.
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
export AZURE_SEARCH_CASE_INDEX=silvercare-cases-v8
uv run python -m silvercare.evaluate \
  --queries data/retrieval-evaluation.json \
  --output data/processed/expanded/retrieval-evaluation-final.json
uv run python -m silvercare.evaluate --audience \
  --queries data/audience-evaluation.json \
  --output data/processed/expanded/audience-evaluation.json
uv run python -m silvercare.expanded release
```

`publish`는 두 corpus의 타입·중복 ID·발행 대상을 원격 변경 전에 검증한다.
현재 설정과 기록된 릴리스의 활성 지식 인덱스는 발행 대상으로 사용할 수 없다.
활성 사례의 재사용은 쓰기·삭제·벡터 갱신 없이 전체 문서 필드의 일치를 확인한다.
`publish`는 모델·차원·입력 hash가 같은 벡터를 재사용한다.
v8은 본문·제목을 바꾸지 않는 메타데이터/검색 개선이므로 문서 벡터를 전부 재사용했다.
v9는 기존 14개를 삭제하지 않고 지식 색인 하나만 추가하여 15개 할당량을 모두 사용한다.
다음 쌍을 만들기 전에는 활성·롤백 대상을 제외한 이전 버전의 명시적인 정리 계획이나 용량 검토가 필요하다.
배포 도구가 오래된 인덱스를 자동 삭제하지는 않는다.
로컬 직접 실행은 기본 `.env`를 읽고 배포는 검증한 `release.json`을 사용한다.
코드가 최신이어도 기본 환경 설정이 최신이라는 뜻은 아니므로 로컬 공개 검색 설정도 활성 릴리스와 맞춘다.
관리하는 발행 대상에서는 변경 문서를 업데이트하고 불필요한 ID를 삭제하므로 대상의 혼동을 피한다.
`data ingest`/`corpus ingest`는 별도의 범용·비벡터 적재 경로이며 운영 하이브리드 인덱스 재발행에 사용하지 않는다.

`release`는 발행 보고서의 검색 endpoint·준비된 corpus hash·발행 문서 수·검색 평가·답변 평가·검색 용량을 검증한다.
답변 평가에서 모델 입력 근거의 존재와 최종 인용은 별도로 검사한다.
복약 API가 실제 모델 문맥에 포함되는지 확인하면서, 해당 답변을 뒷받침하는
공식 고혈압/복약 자료 중 적절한 자료를 인용하는 것은 허용한다.
동일한 뜻의 경과 관찰·정기 검토 표현을 인정하며, 독립 24문항 의학 평가의 모범답안·판정 규칙은 바꾸지 않는다.
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

### 현재 격리된 네 서비스 업데이트

고유 태그로 앱 이미지를 빌드한 뒤 registry에서 확인한 불변 digest로 선택한 프로필만 갱신한다.
`runtime` target은 앱·gateway용이며 큰 CPU 재정렬 모델을 빌드에 포함하지 않는다.

```bash
az acr build --registry silvercare2a5aalbg7ugfc \
  --image silvercare:release-YYYYMMDD-N --file Dockerfile --target runtime --no-logs .
az acr repository show --name silvercare2a5aalbg7ugfc \
  --image silvercare:release-YYYYMMDD-N --query digest -o tsv
uv run python -m silvercare.service_isolation deploy-image \
  --profile qwen-azure-rag --component app \
  --image silvercare2a5aalbg7ugfc.azurecr.io/silvercare@sha256:VERIFIED_DIGEST
.runtime/bin/kubectl --kubeconfig .runtime/kubeconfig -n silvercare get pods,svc
curl -fsS https://ondam-e4b60458.koreacentral.cloudapp.azure.com/qwen-rag/health/ready
```

`deploy-image`는 rollout 성공 뒤에만 전용 상태의 이미지 포인터를 갱신한다.
readiness 성공만으로 모델 추론·STT/TTS 성공을 단정하지 않으며 실제 질문·출처·음성도 확인한다.
검색 인덱스·endpoint·기능 설정의 전환은 이미지 갱신과 별도다. 새 자료는 해당 프로필의 전용 검색에
발행·평가하고 연결 설정과 운영 상태를 검증한 뒤 별도 전환한다. 공유 배포로 우회하지 않는다.
1번 기준 이미지의 변경과 운영 Azure 배포는 명시적으로 승인한 작업에서만 수행한다.
문서·원본·처리 결과·`.env`는 [.dockerignore](../.dockerignore)에 의해 빌드에서 제외한다.
앱 실행 Python은 `/app/.venv/bin/python`이다. 컨테이너에서 검증 명령을 실행할 때도 같은 환경을 사용한다.

### 신규 단일 서비스 bootstrap

신규 환경에는 [main.bicep](../infra/main.bicep)의 기반,
[search-rag.bicep](../infra/search-rag.bicep)의 Basic 검색,
모델·임베딩·음성 배포, IAM과 데이터 릴리스가 필요하다.
기반 템플릿만으로 전체 RAG와 모델이 구성되지는 않는다.
`python -m silvercare.deploy --tag ...`는 이 초기 단일 서비스 도구다. 이미 격리한 현재 환경의
업데이트에 사용하지 않으며 `--skip-infra`도 격리 가드를 우회하지 않는다.
초기 도구는 `release.json`을 읽고 빌드·IAM·Kubernetes 적용·rollout·공개 HTTPS를 검증한다.
초기 배포 상태 파일은 rollout과 공개 HTTPS 검증이 모두 성공한 뒤에만 원자적으로 갱신한다.
실패한 배포를 마지막 정상 배포로 기록하지 않으며, Secret을 포함할 수 있는 stdin 적용 실패의 원문 stderr는 출력하지 않는다.
질병관리청 키가 설정되어 있으면 `silvercare-health` Secret을 적용하고 앱 환경변수에 참조로 연결한다.
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
uv run --project evaluation pytest -q evaluation/tests
uv run --directory evaluation ruff check ondam_eval tests
uv run --directory evaluation mypy
```

실제 검색·모델 평가에는 4절의 Azure 검색 설정이 필요하다.
음성 실측에는 허용된 WAV·정답 전사 쌍을 사용한다.

새로운 [독립 의료 평가](../evaluation/README.md)는 별도 Python 프로젝트다.
온담 API에서 답변을 수집한 뒤 `EVAL_JUDGE_DEPLOYMENT`의 평가 모델로 의미·근거를 채점한다.
운영 `AZURE_OPENAI_DEPLOYMENT`는 바꾸지 않는다. 질문·채점 기준의 전문가 검토 전 결과는
초안으로 표시하며, 기존 키워드·검색 회귀 평가를 임상 검증으로 대체하거나 혼동하지 않는다.

평가 요청에서 실제 근거 입력을 확인하려면 서버에 `EVALUATION_TRACE_ENABLED=true`와
32자 이상의 별도 `EVALUATION_TRACE_TOKEN`을 설정하고, 수집 명령에 `--trace`를 지정한다.
토큰은 Git에서 제외한 개인 설정과 전용 Kubernetes Secret에만 보관한다.
일반 요청에는 추적 정보가 추가되지 않는다. 합성 평가 문항에만 사용하며,
운영 사용자 대화의 상시 기록이나 서버 측 추적 파일 저장은 수행하지 않는다.
서버 기능은 기본적으로 꺼져 있고, 올바른 토큰을 보낸 명시적 평가 요청만 허용한다.
권한 없는 추적 요청은 403으로 거절한다. 출처 본문의 일반 화면용 260자 발췌와 달리
권한 있는 추적에는 실제 모델 입력 근거와 고유 청크 ID가 포함되므로 공유 시에도 주의한다.

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

다음 표는 Azure 기준 서비스와 개별 기능에 대해 보존한 검증 기록이다.
네 프로필이 모두 같은 수치를 통과했다는 뜻이 아니며, 현재 비교 결과와 측정 조건은
[네 서비스 평가 보고서](comparison-results-2026-11-16.md)를 따른다.

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
| `.runtime/service-isolation-state.json` | 현재 전용 연결·기준 이미지·이전 설정·검증 결과. 0600 권한으로 백업 |
| `.runtime/deployment.json` | 격리 이전 단일 서비스 배포 기록. 현재 전용 연결의 기준 아님 |
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
