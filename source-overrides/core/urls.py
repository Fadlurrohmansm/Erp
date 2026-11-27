from . import attendance_notifications

from django.urls import path
from tenancy.views import branch_list
from . import views
from .attendance_rules import attendance_cutoff
from .portal_views import delete_check_in

urlpatterns = [
    path('branches/', branch_list, name='branch_list'),
    path('employees/import-branch/',views.employee_import_branch,name='employee_import_branch'),
    path('payroll/<int:payroll_id>/pay/',views.payroll_pay,name='payroll_pay'),
    path('payroll/payments/<int:payment_id>/cancel/',views.payroll_payment_cancel,name='payroll_payment_cancel'),
    path('account/',views.account_settings,name='account_settings'),
    path('account/create/',views.account_create,name='account_create'),
    path('account/<int:user_id>/username/', views.account_username_change, name='account_username_change'),
    path('account/<int:user_id>/hapus/', views.account_delete, name='account_delete'),
    path('account/password/',views.account_password_change,name='account_password_change'),
    path('payroll/deductions/',views.payroll_deduction_settings,name='payroll_deduction_settings'),
    path('payroll/layout/',views.slip_layout_settings,name='slip_layout_settings'),
    path("payroll/<int:payroll_id>/preview/",views.payroll_preview,name="payroll_preview"),
    path("payroll/<int:payroll_id>/refresh/",views.payroll_refresh,name="payroll_refresh"),
    path('roles/',views.role_list,name='role_list'),
    path('roles/add/',views.role_form,name='role_create'),
    path('roles/<int:role_id>/edit/',views.role_form,name='role_edit'),
    path('roles/<int:role_id>/delete/',views.role_delete,name='role_delete'),
    path("payroll/<int:payroll_id>/delete/", views.payroll_delete, name="payroll_delete"),
    path("", views.dashboard, name="dashboard"),
    path("employees/", views.employee_list, name="employee_list"),
    path("employees/add/", views.employee_create, name="employee_create"),
    path(
        "employees/<int:employee_id>/edit/",
        views.employee_edit,
        name="employee_edit",
    ),
    path(
        "employees/<int:employee_id>/delete/",
        views.employee_delete,
        name="employee_delete",
    ),
        
path("attendance/", views.attendance_list, name="attendance_list"),
path("attendance/batas-hadir/", attendance_cutoff, name="attendance_cutoff"),
path("attendance/notifikasi/", attendance_notifications.attendance_notifications, name="attendance_notifications"),
path("attendance/notifikasi/perangkat/", attendance_notifications.attendance_push_subscription, name="attendance_push_subscription"),
path("attendance/notifikasi/<int:check_in_id>/lokasi/", attendance_notifications.attendance_notification_location, name="attendance_notification_location"),
path("attendance/check-in/<int:check_in_id>/delete/", delete_check_in, name="attendance_delete_check_in"),
path("attendance/add/", views.attendance_create, name="attendance_create"),
path(
    "attendance/<int:attendance_id>/edit/",
    views.attendance_edit,
    name="attendance_edit",
),
path("payroll/", views.payroll_list, name="payroll_list"),
path("payroll/add/", views.payroll_create, name="payroll_create"),
path(
    "payroll/<int:payroll_id>/edit/",
    views.payroll_edit,
    name="payroll_edit",
),
path(
    "payroll/<int:payroll_id>/pdf/",
    views.payroll_pdf,
    name="payroll_pdf",
),
]
