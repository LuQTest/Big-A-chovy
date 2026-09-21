FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

# Keep generated state in a named volume without masking the application code.
# The Python modules intentionally keep their existing paths for native/macOS
# compatibility, so the container links those paths into /app/runtime.
RUN mkdir -p /app/runtime /app/筛选结果 /app/决策记录 /app/tools/shadow_data \
    && ln -s /app/runtime/.kline_cache.json \
        /app/daily-stock-analysis/scripts/.kline_cache.json \
    && ln -s /app/runtime/flow_snapshot.json \
        /app/daily-stock-analysis/scripts/flow_snapshot.json \
    && ln -s /app/runtime/.announcement_risk_cache.json \
        /app/daily-stock-analysis/scripts/.announcement_risk_cache.json \
    && ln -s /app/runtime/watchlist_breakout_state.json \
        /app/daily-stock-analysis/scripts/watchlist_breakout_state.json \
    && ln -s /app/runtime/last_valid_result.json \
        /app/daily-stock-analysis/scripts/last_valid_result.json \
    && ln -s /app/runtime/intersection_state.json \
        /app/daily-stock-analysis/scripts/intersection_state.json \
    && ln -s /app/runtime/holdings.json \
        /app/daily-stock-analysis/scripts/holdings.json \
    && ln -s /app/runtime/gui_settings.json \
        /app/daily-stock-analysis/scripts/gui_settings.json \
    && ln -s /app/runtime/.em_nontrading_refresh \
        /app/daily-stock-analysis/scripts/.em_nontrading_refresh

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=4)"

# The workbench includes the original dashboard routes at / and adds
# /workbench plus the toolbox APIs on the same port.
CMD ["python", "daily-stock-analysis/scripts/web_workbench.py", "--host", "0.0.0.0", "--no-browser"]
