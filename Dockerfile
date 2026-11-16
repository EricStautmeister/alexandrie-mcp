FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs npm \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mcp[cli]>=1.0" requests uvicorn

# Markdown compiler (same one the Alexandrie frontend uses for content_compiled)
COPY compile/package.json compile/
RUN cd compile && npm install --omit=dev --no-audit --no-fund
COPY compile/ compile/

COPY server.py .

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "while true; do python server.py; echo 'MCP server exited, restarting in 3s...'; sleep 3; done"]
