# 아키텍처와 코드 구조

코드·릴리스 설정 확인 기준일: 2026-11-11.
자료별 사용 방식은 [RAG·API](rag.md), 실행 방법은 [운영 절차](operations.md)에 있다.

## 1. 서비스 구조

```text
어르신·보호자 브라우저
  │ HTTPS, 텍스트 모드·선택형 음성 대화
  ▼
AKS LoadBalancer → Caddy → FastAPI
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

## 2. 리소스

| 역할 | 리소스·설정 | 소유 범위 |
| --- | --- | --- |
| 서비스 리소스 그룹 | `rg-silver-care`, Korea Central | AKS·ACR·검색·관리 ID |
| AKS | `aks-silvercare`, `Standard_D2ds_v5` 노드 1개 | 앱·Caddy·공개 Service |
| 컨테이너 레지스트리 | `silvercare2a5aalbg7ugfc` | ACR Basic |
| 앱 관리 ID | `silvercare-app` | AKS Workload Identity |
| 운영 검색 | `silvercare-rag-2a5aalbg7ugfc` | Basic, replica 1 / partition 1, semantic Standard |
| 생성 모델 | `RG-AI/ai-foundry-m098047`, `gpt-6.1-terra` | 공유 Foundry 계정, GlobalStandard |
| 임베딩 | `silvercare-embedding`, `text-embedding-3-small`, 512차원 | 같은 Foundry 계정 |
| 음성 | Foundry 계정의 Speech endpoint, `ko-KR-SunHiNeural` | 선택형 STT/TTS |
| HTTPS | Caddy·공개 IP·영속 인증서 디스크 | LoadBalancer의 80/443 |

공유 Foundry 계정의 전역 정책·다른 프로젝트 학습을 앱 배포에서 수정하지 않는다.
`GlobalStandard`는 계정 위치와 추론 처리 위치가 같다고 보장하지 않는다.

### 템플릿과 활성 설정

- [main.bicep](../infra/main.bicep): AKS·ACR·관리 ID와 **Free 검색**을 만드는 기본 템플릿이다.
- [search-rag.bicep](../infra/search-rag.bicep): 운영 corpus 용량에 사용하는 **Basic 검색** 템플릿이다.
- [deploy.py](../silvercare/deploy.py): Kubernetes 객체 생성·역할 연결·빌드·rollout·HTTPS 검증을 수행한다.
- `data/processed/expanded/release.json`: 사용할 검색 endpoint·인덱스·검색 기능 설정이다.
- `.runtime/deployment.json`: 배포 URL·이미지·클러스터·검색 endpoint의 로컬 상태다.

기본 템플릿의 Free 검색을 운영 검색으로 오해하지 않는다.
운영 인덱스는 `silvercare-knowledge-v4`, `silvercare-cases-v4`이며 앱은 릴리스 설정으로 선택한다.
별도 Free 검색의 파일럿 인덱스는 운영 답변에 사용하지 않는다.
이미지 태그와 인덱스 이름은 서로 독립적이다.

## 3. 모듈별 책임

| 파일 | 책임 |
| --- | --- |
| [app.py](../silvercare/app.py) | FastAPI·수명주기·라우트·출처 검증·요청 제한·오류 응답 |
| [azure.py](../silvercare/azure.py) | 현재 시스템 지침·검색어 확장·질문 임베딩·검색·API 근거 결합·생성·인용 검증·음성 |
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
| [expanded.py](../silvercare/expanded.py) | 지식·사례·holdout 구성·임베딩 캐시·버전 인덱스 발행·릴리스 검증 |
| [evaluate.py](../silvercare/evaluate.py) | 검색·실제 답변·음성 평가 |
| [static/](../silvercare/static/) | HTML·CSS·JavaScript·화면 이미지 |

## 4. 온라인 HTTP 인터페이스

| 경로 | 동작 |
| --- | --- |
| `GET /health/live` | 앱 프로세스 응답. 모든 외부 의존성의 종합 상태는 아님 |
| `GET /api/config` | 기능 활성 여부. 모델명·인증키는 반환하지 않음 |
| `POST /api/chat` | 검색·질병관리청 근거를 이용한 답변 |
| `GET /api/health-topics` | 설정된 21개 건강 주제. API 키가 없으면 빈 목록 |
| `GET /api/health-info/{content_id}` | 허용된 건강 주제의 원문 텍스트 발췌 |
| `POST /api/speech/transcribe` | 음성 인식, 최대 4MB |
| `POST /api/speech/synthesize` | 선택한 답변의 음성 합성 |

일반 요청 본문은 196,608바이트, 음성 업로드는 4MB와 multipart 여유분으로 제한한다.
익명 공개 서비스이며 사용자 진료 기록 관리나 자동 신고 시스템이 아니다.

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
- 모드 전환·일시정지·새 대화·화면 이탈은 대기 중 권한·인식·재생·재청취를 무효화한다. 두 모드의 대화 이력과 자료 검색은 공유한다.

앱과 엣지는 non-root·읽기 전용 루트 파일시스템으로 실행한다.
NetworkPolicy와 Caddy의 프록시 설정, 요청 제한·동시 처리 제한을 사용한다.
단일 노드·앱·엣지·검색 replica 구성에는 고가용성·분산 rate limit·의료 정확성 보장이 없다.
