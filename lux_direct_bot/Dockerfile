FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py dashboard.py lite.py connect.py exchange_diagnostics.py binance_account.py binance_preflight.py signal_levels.py live_readiness_policy.py durable_sqlite.py order_plan.py execution_runner.py binance_transport.py exchange_verification.py offline_sizing.py live_execution_candidate.py ./
COPY tests ./tests
RUN python -m pytest -q tests/test_live_execution_candidate.py tests/test_execution_runner.py
ENV PORT=10000
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --no-access-log"]
