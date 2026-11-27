from django.http import HttpResponse, JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_safe


@require_safe
def manifest(request):
    scope = reverse('employee_portal:home')
    return JsonResponse({
        'id': scope,
        'name': 'ERP UMKM',
        'short_name': 'ERP UMKM',
        'lang': 'id',
        'start_url': scope,
        'scope': scope,
        'display': 'standalone',
        'background_color': '#f3f6fc',
        'theme_color': '#f3f6fc',
        'icons': [
            {'src': static(f'core/icons/erp-umkm-{size}.png'), 'sizes': f'{size}x{size}', 'type': 'image/png'}
            for size in (192, 512)
        ],
    }, content_type='application/manifest+json')


@require_safe
def service_worker(request):
    # Private attendance pages and check-ins always use the network.
    return HttpResponse(
        "self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));\n"
        "self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));\n",
        content_type='application/javascript; charset=utf-8',
        headers={'Cache-Control': 'no-cache'},
    )
