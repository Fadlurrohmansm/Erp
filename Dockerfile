FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY Capy_Works_Shortcut.zip /tmp/source.zip
RUN python -m zipfile -e /tmp/source.zip /app && rm /tmp/source.zip
COPY shortcut-icons/ /app/core/static/core/icons/
RUN python -m pip install --no-cache-dir -r requirements.txt
RUN ERP_SECRET_KEY=django-collectstatic-build-only python manage.py collectstatic --noinput
CMD ["sh", "railway-start.sh"]
