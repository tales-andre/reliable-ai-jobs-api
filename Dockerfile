FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY jobsapi ./jobsapi
COPY fakeprovider ./fakeprovider
COPY chaos ./chaos
COPY tests ./tests
COPY pyproject.toml ./
RUN useradd --create-home app && mkdir -p /app/report && chown -R app /app
USER app
CMD ["python", "-m", "jobsapi"]
