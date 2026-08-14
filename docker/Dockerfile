FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY matrix_room_media_retention/ ./matrix_room_media_retention/
COPY pyproject.toml .

RUN pip install --no-cache-dir --no-deps -e .

# db_path in config.yaml should point here so the SQLite file survives a
# container recreate -- mount this as a volume.
VOLUME ["/data"]

ENTRYPOINT ["python3", "-m", "matrix_room_media_retention.main"]
CMD ["--config", "/data/config.yaml"]
