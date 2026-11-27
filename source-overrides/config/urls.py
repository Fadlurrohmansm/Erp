
import logging

from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse, HttpResponse
from django.db import DatabaseError, connection
from django.views.decorators.http import require_safe
from tenancy import views as branch_views
from pathlib import Path
from django.conf import settings


logger = logging.getLogger(__name__)


@require_safe
def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except DatabaseError:
        logger.exception('ERP database health check failed')
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})

def web_manifest(request):
    manifest = Path(settings.BASE_DIR) / 'core' / 'static' / 'core' / 'manifest.webmanifest'
    response = HttpResponse(manifest.read_text(encoding='utf-8'), content_type='application/manifest+json; charset=utf-8')
    response['Cache-Control'] = 'no-cache'
    return response


def service_worker(request):
    worker = Path(settings.BASE_DIR) / 'core' / 'static' / 'core' / 'service-worker.js'
    response = HttpResponse(worker.read_text(encoding='utf-8'), content_type='application/javascript; charset=utf-8')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


urlpatterns = [
    path('manifest.webmanifest', web_manifest, name='web_manifest'),
    path('service-worker.js', service_worker, name='service_worker'),
    path("karyawan/", include("core.portal_urls")),
    path('health/', health_check, name='health_check'),
    path('branches/<int:branch_id>/edit/',branch_views.branch_edit,name='branch_edit'),
    path('branches/add/',branch_views.branch_create,name='branch_create'),
    path('branches/switch/',branch_views.branch_switch,name='branch_switch'),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
    path("", include("operations.urls")),
]
