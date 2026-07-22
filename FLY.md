# Fly.io 워커 배포

## 왜 Fly 인가

토스 Open API 는 **IP 화이트리스트** 방식이라 고정 outbound IP 가 필요하다.
확인한 시장가(2026-07):

| 서비스 | 고정 outbound IP | 비고 |
|---|---|---|
| **Fly.io** | **$0.005/시간 ≈ $3.60/월** | 가장 저렴 |
| Render | IP 별도 + **cron 자체가 유료**(월 최소 $1/서비스) | |
| AWS | **NAT Gateway $0.045/시간 ≈ $33/월** | Lambda 가 무료여도 IP 에서 터진다 |
| Oracle Always Free | 0원 | **7일 유휴 시 인스턴스 회수** — IP 소멸 위험 |

## 배포 절차

```bash
export PATH="$HOME/.fly/bin:$PATH"

# 1. 로그인 (브라우저)
flyctl auth login

# 2. 앱 생성 (배포는 아직)
flyctl launch --no-deploy --copy-config --name toss-dashboard-worker --region nrt

# 3. 시크릿 등록 — .env.render 를 그대로 넣으면 된다
#    ⚠️ 토스 자격증명은 넣지 않는다. DB(user_credential)가 단일 출처다.
cat .env.render | grep -vE '^(TOSS_|#|$)' | xargs flyctl secrets set

# 4. 배포
flyctl deploy

# 5. ★ egress IP 할당 — 이게 핵심이다
flyctl machine list                        # machine-id 확인
flyctl machine egress-ip allocate <machine-id>
flyctl machine egress-ip list              # 할당된 IP 확인
```

### ⚠️ dedicated IPv4 ≠ egress IP

Fly 문서 원문: *"Dedicated IPv4 할당이 아웃바운드 트래픽에는 영향을 주지 않는다"*

- **dedicated IPv4** = 인바운드(수신)용. 우리에겐 필요 없다.
- **egress IP** = 아웃바운드 고정. `fly machine egress-ip allocate` 로 따로 할당.

이걸 헷갈려서 dedicated IPv4 를 사면 돈만 나가고 토스는 계속 403 이다.

## 6. 토스에 IP 등록

`egress-ip list` 로 나온 IPv4 를 토스 WTS → 설정 → Open API → 허용 IP 에 등록.

등록 후 확인:
```bash
flyctl ssh console -C "python3 worker/main.py doctor"
```
`[토스 API]` 가 ✅ 면 완료. 이 명령은 그 머신의 실제 공인 IP 도 찍어준다.

## 운영

```bash
flyctl logs                    # 실시간 로그
flyctl status                  # 머신 상태
flyctl ssh console             # 셸 접속
flyctl scale count 1           # ⚠️ 반드시 1대. 여러 대면 토스 토큰이 서로 무효화된다
```

**머신은 1대만 띄운다.** 토스는 client 당 유효 토큰이 1개라, 두 머신이 각자
토큰을 발급하면 서로를 401 로 만든다.

## 비용 예상

- egress IP: ~$3.60/월
- shared-cpu-1x 512MB 상주: Fly 요금표 확인 필요 (미확인)

무료 티어 한도 안에 드는지는 `flyctl` 대시보드에서 실사용량을 봐야 정확하다.
