from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import Resolver404, resolve

from .portal_models import EmployeePortalAccount


class EmployeePortalMiddleware:
    """Enforce the portal boundary for any login path using a linked account."""

    allowed = {
        'employee_portal:home',
        'employee_portal:login',
        'employee_portal:check_in',
        'employee_portal:logout',
        'employee_portal:manifest',
        'employee_portal:service_worker',
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.portal_account = None
        if request.path_info == '/health/':
            return self.get_response(request)
        if request.user.is_authenticated:
            request.portal_account = (
                EmployeePortalAccount.objects.select_related('employee', 'employee__branch')
                .filter(user_id=request.user.pk)
                .first()
            )
        if request.portal_account:
            try:
                allowed = resolve(request.path_info).view_name in self.allowed
            except Resolver404:
                allowed = False
            if not allowed:
                if request.method in ('GET', 'HEAD'):
                    return redirect('employee_portal:home')
                return HttpResponseForbidden('Akun karyawan hanya dapat mengakses portal absensi.')
        return self.get_response(request)
