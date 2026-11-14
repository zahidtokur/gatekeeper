FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /data && chown app:app /data


ARG SERVICE=gateway
COPY ${SERVICE}/ /app/

ENV DJANGO_SETTINGS_MODULE=config.settings ASGI_APPLICATION=config.asgi:application

USER app

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && exec daphne -b 0.0.0.0 -p 8000 ${ASGI_APPLICATION}"]
