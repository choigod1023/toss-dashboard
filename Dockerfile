# 수집·분석 워커. 대시보드(Next.js)는 Vercel 에 따로 있고,
# 이 컨테이너는 DB 를 채우는 역할만 한다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Seoul

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker/ ./worker/

# 루트로 돌리지 않는다 — 이 프로세스는 전 사용자의 자격증명을
# 복호화할 수 있는 마스터 키를 들고 있다.
RUN useradd -m -u 10001 worker && chown -R worker:worker /app
USER worker

# 관심종목은 배포 시 --symbols 로 덮어쓸 수 있다
CMD ["python3", "worker/main.py", "run"]
