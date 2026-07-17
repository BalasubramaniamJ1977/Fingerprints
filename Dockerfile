FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fingerprints/ fingerprints/
COPY api/ api/
COPY studio/ studio/
COPY recipes/ recipes/

RUN pip install --no-cache-dir ".[postgres]"

# production env:
#   FP_SALT       required - salt for hash/pseudonym policies
#   FP_API_TOKEN  optional - bearer token protecting /api routes
ENV PYTHONUNBUFFERED=1
EXPOSE 8600

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8600"]
