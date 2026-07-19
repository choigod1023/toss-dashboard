# 비활성화된 워크플로

`market.yml` / `daily.yml` 은 토스 Open API 를 호출한다.
토스는 **IP 화이트리스트** 방식인데 GitHub Actions 러너는 IP 가 매번
바뀌므로 등록이 불가능하다 → 실행하면 `403 ip-not-allowed` 만 난다.

이 작업들은 고정 outbound IP 를 가진 **Render cron**(`render.yaml`)에서 돈다.

여기 남아 있는 워크플로는 GitHub Actions 에서 실행되지 않는다
(`.github/workflows/` 바로 아래가 아니므로).
