# 배포 가이드

## 구성

| 무엇 | 어디 | 왜 |
|---|---|---|
| 대시보드 (Next.js) | **Vercel** | DB 읽기 전용, 서버리스로 충분 |
| 뉴스·RSS·감성분석·보존정리 | **GitHub Actions** | 토스 API 미사용 → IP 무관, 무료 |
| 시세·계좌·전략 (토스 API) | **Render cron** | 토스가 IP 화이트리스트 방식 → 고정 IP 필요 |
| DB | **Neon Postgres** | TimescaleDB + pgvector |

> 토스 Open API 는 허용된 IP 에서만 호출된다. GitHub Actions 러너는 IP 가
> 매번 바뀌어 등록이 불가능하므로(403 ip-not-allowed), 토스 의존 작업만
> 고정 IP 를 가진 Render 로 분리했다.

## Render 배포 절차

### 1. Blueprint 생성
https://dashboard.render.com/blueprints → **New Blueprint Instance**
→ `choigod1023/toss-dashboard` 선택 → `render.yaml` 자동 인식

### 2. 환경변수 입력 (`sync: false` 항목)
```
DATABASE_URL           Neon 접속 문자열
TOSS_CLIENT_ID         토스 Open API
TOSS_CLIENT_SECRET     토스 Open API
GEMINI_API_KEY         aistudio.google.com/apikey
DART_API_KEY           opendart.fss.or.kr
CREDENTIAL_MASTER_KEY  ★ Vercel·GitHub 과 반드시 같은 값
```

`CREDENTIAL_MASTER_KEY` 가 다르면 사용자 자격증명을 복호화할 수 없다.
`ALLOW_ORDERS` 는 **절대 넣지 말 것** — 주문 경로가 열린다.

### 3. Outbound IP 를 토스에 등록  ← 빠뜨리면 전부 403

서비스 → **Connect** → **Outbound** 탭의 IP 를 **전부** 복사해서
토스증권 WTS → 설정 → Open API → **허용 IP** 에 등록.
허용 개수 제한이 없으므로 나오는 대로 다 넣는다.

### 4. 확인
`toss-daily` 를 수동 실행(Manual Run)해서 403 이 안 나면 완료.

## 주의사항

- **region 을 바꾸지 말 것.** Render outbound IP 는 지역별로 다르다.
  지역이 바뀌면 토스 화이트리스트를 전부 다시 등록해야 한다.
- **비용**: cron 서비스당 월 최소 $1 (초 단위 정산). 서비스 2개 → 최소 $2/월.
- **멀티유저**: 사용자마다 자기 토스 계정에 **이 서버 IP를 등록**해야 한다.
  온보딩 화면이 403 을 감지해 IP 를 안내하지만, 이탈 요인이다.

## 로컬 실행

```bash
python3 worker/main.py daily      # 토스 배치 1회
python3 worker/main.py news       # 뉴스·감성 1회
python3 worker/main.py run        # 상주 스케줄러 (전체)
```

## 미해결

- **시세 지연 여부 미확정** — 장중에 `python3 worker/probe_toss.py` 로 확인할 것.
  실시간이냐 15분 지연이냐가 자동매매 설계의 전제다.
- US 수수료가 `0.1`, KR 은 `0.00015` — 단위가 달라 보인다.
  실거래 내역과 대조 전에는 백테스팅에 쓰지 말 것.
- 자본시장법(투자자문업) 및 토스 약관상 제3자 서비스 제공 가능 여부 미확인.
