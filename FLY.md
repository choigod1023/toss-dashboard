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

## 실제 배포에서 걸린 함정 (겪은 순서대로)

1. **도쿄(nrt) egress IP 재고 없음** — `No more egress IPs available`.
   싱가포르(sin)로 옮겨 해결. IP 는 리전에 묶이므로 리전을 바꾸면 IP 도 바뀐다.
2. **트라이얼 조직은 egress IP 할당 불가** — 카드 등록 필요.
3. **egress IP 는 머신 단위** — `fly deploy` 로 머신이 새로 생기면 따라오지 않는다.
   배포 후 반드시 `fly machine egress-ip list` 확인.
4. **할당만으로는 반영 안 됨** — 머신을 재시작해야 실제 outbound 가 바뀐다.
5. **Fly 내부망은 IPv6** — `0.0.0.0` 바인딩이면 프로세스는 살아있는데
   프록시가 못 붙어 Connection refused. dual-stack(`::`)으로 열어야 한다.
6. **인바운드 IP 도 따로 필요** — `fly ips allocate-v4 --shared`.
7. **SESSION_PEPPER 누락** — Fly 가 빈 페퍼로 세션을 해시하고 Vercel 은
   진짜 페퍼로 검증해서 영원히 불일치. 대시보드가 계속 온보딩으로 튕겼다.

## 현재 IP (토스 허용 IP 에 둘 다 등록)

```
209.71.95.247   api    — 온보딩 검증 프록시
209.71.95.232   worker — 스케줄 수집
```
