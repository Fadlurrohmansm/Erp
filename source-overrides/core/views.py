from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from io import BytesIO
import calendar

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, F
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils.text import slugify
from django.utils import timezone
from django.views.decorators.http import require_POST

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .models import Employee, Attendance, Payroll, JobRole, SlipLayout
from .forms import EmployeeForm, AttendanceForm, PayrollForm, PayrollExtraFormSet, DeleteConfirmationForm, JobRoleForm



@login_required(login_url="login")
def dashboard(request):
    from operations.views import dashboard as overview
    return overview(request)


@login_required(login_url="login")
def employee_list(request):
    search = request.GET.get("q", "").strip()
    employees = Employee.objects.select_related('role','branch').all()

    if search:
        employees = employees.filter(
            Q(employee_code__icontains=search)
            | Q(full_name__icontains=search)
            | Q(position__icontains=search)
            | Q(role__name__icontains=search)
            | Q(phone__icontains=search)
        )

    return render(request, "core/employees.html", {
        "employees": employees,
        "search": search,
        "total_employees": Employee.objects.filter(
            status="active"
        ).count(),
    })


@login_required(login_url="login")
def employee_create(request):
    if request.method == "POST":
        form = EmployeeForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect("employee_list")
    else:
        form = EmployeeForm()

    return render(request, "core/employee_form.html", {
        "form": form,
        "page_title": "Tambah karyawan",
        "back_url": "/employees/",
    })


@login_required(login_url="login")
def employee_edit(request, employee_id):
    employee = get_object_or_404(Employee, pk=employee_id)
    if employee.branch_id!=request.branch.pk:
        messages.info(request,'Karyawan ini terhubung dari '+employee.branch.name+'. Ubah data utama di cabang asal; absensi dapat diperbarui di cabang ini.')
        return redirect('employee_list')


    if request.method == "POST":
        form = EmployeeForm(request.POST, instance=employee)
        if form.is_valid():
            form.save()
            return redirect("employee_list")
    else:
        form = EmployeeForm(instance=employee)

    return render(request, "core/employee_form.html", {
        "form": form,
        "page_title": "Edit karyawan",
        "back_url": "/employees/",
        "employee": employee,
    })



@login_required(login_url="login")
def employee_delete(request, employee_id):
    employee = get_object_or_404(Employee, pk=employee_id)
    if employee.branch_id!=request.branch.pk or employee.branch_accesses.exists():
        messages.error(request,'Karyawan terhubung ke beberapa cabang. Gunakan status Nonaktif di cabang asal untuk menjaga identitas dan absensi bersama.')
        return redirect('employee_list')

    if employee.payroll_records.filter(payments__isnull=False).exists():
        messages.error(request,'Karyawan memiliki riwayat pembayaran gaji dan tidak dapat dihapus. Gunakan status Nonaktif untuk mempertahankan riwayat.')
        return redirect('employee_list')
    form = DeleteConfirmationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            Employee.objects.filter(pk=employee_id).update(base_salary=F('base_salary'))
            employee = Employee.objects.select_for_update().get(pk=employee_id)
            if employee.payroll_records.filter(payments__isnull=False).exists():
                messages.error(request,'Karyawan memiliki riwayat pembayaran gaji; gunakan status Nonaktif.')
                return redirect('employee_list')
            employee.payroll_records.all().delete()
            employee.attendance_records.all().delete()
            employee.delete()
        messages.success(request, 'Karyawan beserta absensi, payroll, dan bonus terkait berhasil dihapus.')
        return redirect('employee_list')
    return render(request, 'core/delete_confirm.html', {
        'form': form, 'page_title': 'Hapus karyawan', 'object_name': str(employee),
        'description': f'{employee.attendance_records.count()} absensi dan {employee.payroll_records.count()} payroll beserta bonusnya akan ikut dihapus. Untuk mempertahankan riwayat, gunakan Edit → status Nonaktif.',
        'back_url': '/employees/',
    })



@login_required(login_url="login")
def attendance_list(request):
    today = timezone.localdate()

    month_value = request.GET.get("month", "")
    try:
        year, month = map(int, month_value.split("-"))
        if not (1 <= month <= 12 and 2000 <= year <= 2100):
            raise ValueError
    except (ValueError, TypeError):
        year, month = today.year, today.month

    employees = Employee.objects.filter(
        status="active"
    ).order_by("full_name")

    employee_id = request.GET.get("employee", "")
    employee = (
        employees.filter(pk=employee_id).first()
        if employee_id.isdigit()
        else employees.first()
    )

    try:
        day = int(request.GET.get("day", today.day))
        selected_date = date(year, month, day)
    except (ValueError, TypeError):
        selected_date = date(year, month, 1)

    if request.method == "POST":
        try:
            employee = employees.get(pk=request.POST.get("employee"))
            selected_date = date.fromisoformat(
                request.POST.get("date", "")
            )

            if (
                selected_date.year != year
                or selected_date.month != month
            ):
                raise ValueError("Tanggal tidak sesuai bulan kalender.")

            raw_dates = request.POST.getlist('dates')
            if len(raw_dates) > 31:
                raise ValueError('Maksimal 31 tanggal sekaligus.')
            selected_dates = sorted({date.fromisoformat(value) for value in raw_dates}) if raw_dates else [selected_date]
            if any(value.year != year or value.month != month for value in selected_dates):
                raise ValueError('Semua tanggal harus sesuai bulan kalender.')

            status = request.POST.get("status", "")
            punctual_value = request.POST.get("punctual", "no")

            allowed_statuses = {
                "present", "leave", "sick", "absent", "late", "off"
            }
            if status not in allowed_statuses:
                raise ValueError("Status absensi tidak valid.")

            punctual = (
                status == "present"
                and punctual_value == "yes"
            )

            with transaction.atomic():
                # Keep DISTINCT inside the visibility subquery so PostgreSQL can lock the employee row.
                visible_employee_ids = employees.order_by().values("pk")
                employee = (
                    Employee._base_manager.filter(pk__in=visible_employee_ids)
                    .select_for_update()
                    .get(pk=employee.pk)
                )
                forms = []
                for target_date in selected_dates:
                    record = Attendance.objects.filter(employee=employee, date=target_date).first()
                    data = request.POST.copy()
                    data['employee'] = str(employee.pk)
                    data['date'] = target_date.isoformat()
                    data['punctual'] = str(punctual)
                    form = AttendanceForm(data, instance=record)
                    if not form.is_valid():
                        raise ValueError(f"{target_date:%d/%m/%Y}: " + '; '.join(str(e) for errors in form.errors.values() for e in errors))
                    forms.append(form)
                for form in forms:
                    form.save()

            messages.success(request, f'Absensi {len(selected_dates)} tanggal berhasil disimpan.')
            return redirect(
                f"{request.path}?month={year}-{month:02d}"
                f"&employee={employee.pk}&day={selected_date.day}"
            )

        except (Employee.DoesNotExist, ValueError, TypeError) as exc:
            messages.error(
                request,
                f"Data tidak valid: {exc}"
            )

    month_records = Attendance.objects.filter(
        date__year=year,
        date__month=month,
    ).select_related("work_branch", "branch", "portal_check_in")

    records_by_day = {}
    if employee:
        records_by_day = {
            record.date.day: record
            for record in month_records.filter(employee=employee)
        }

    month_calendar = calendar.Calendar(firstweekday=0)
    weeks = month_calendar.monthdayscalendar(year, month)

    calendar_weeks = []
    for week in weeks:
        calendar_week = []
        for day_number in week:
            calendar_week.append({
                "day": day_number,
                "record": (
                    records_by_day.get(day_number)
                    if day_number else None
                ),
                "selected": day_number == selected_date.day,
            })
        calendar_weeks.append(calendar_week)

    selected_record = records_by_day.get(selected_date.day) if (
        selected_date.year == year and selected_date.month == month
    ) else None

    previous_month = (
        date(year - 1, 12, 1)
        if month == 1
        else date(year, month - 1, 1)
    )
    next_month = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )

    month_names = [
        "Januari", "Februari", "Maret", "April",
        "Mei", "Juni", "Juli", "Agustus",
        "September", "Oktober", "November", "Desember",
    ]

    from .models import PayrollDeductionPolicy
    quota_policy=PayrollDeductionPolicy.objects.first()
    shared_quota=bool(quota_policy and quota_policy.quota_mode=='shared')
    quota_statuses=['off','leave','sick'] if shared_quota else ['off']
    used_quota=sum(r.status in quota_statuses for r in records_by_day.values())
    return render(request, "core/attendance.html", {
        "employees": employees,
        "employee": employee,
        "year": year,
        "month": month,
        "month_name": month_names[month - 1],
        "calendar_weeks": calendar_weeks,
        "records_by_day": records_by_day,
        "off_days": used_quota,
        "shared_quota":shared_quota,
        "remaining_leave": max(0, employee.monthly_leave_quota - used_quota) if employee else 0,
        "excess_leave": max(0, used_quota - employee.monthly_leave_quota) if employee else 0,
        "selected_date": selected_date,
        "selected_record": selected_record,
        "attendance_cutoff": request.branch.punctuality_cutoff_for(selected_date),
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "status_choices": [
            ("present", "Masuk"),
            ("leave", "Izin"),
            ("off", "Libur Jatah"),
            ("sick", "Sakit"),
            ("absent", "Tidak Hadir"),
            ("late", "Terlambat (data lama)"),
        ],
    })

@login_required(login_url="login")
def attendance_create(request):
    if request.method == "POST":
        form = AttendanceForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Absensi berhasil disimpan.")
            return redirect("attendance_list")
    else:
        form = AttendanceForm()

    return render(request, "core/attendance_form.html", {
        "form": form,
        "page_title": "Record Attendance",
    })


@login_required(login_url="login")
def attendance_edit(request, attendance_id):
    record = get_object_or_404(Attendance, pk=attendance_id)

    if request.method == "POST":
        form = AttendanceForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            messages.success(request, "Absensi berhasil diperbarui.")
            return redirect("attendance_list")
    else:
        form = AttendanceForm(instance=record)

    return render(request, "core/attendance_form.html", {
        "form": form,
        "page_title": "Edit absensi",
        "back_url": "/attendance/",
    })


@login_required(login_url="login")
def payroll_list(request):
    from django.db.models import Exists, OuterRef
    from .models import PayrollPayment

    active_payments = PayrollPayment.objects.filter(payroll_id=OuterRef("pk"), voided=False)
    payrolls = (
        Payroll.objects.select_related("employee")
        .prefetch_related("extras", "payments")
        .alias(has_active_payment=Exists(active_payments))
        .order_by("has_active_payment", *Payroll._meta.ordering, "pk")
    )

    query = request.GET.get("q", "").strip()
    month = request.GET.get("month", "").strip()
    year = request.GET.get("year", "").strip()

    if query:
        payrolls = payrolls.filter(
            Q(employee__full_name__icontains=query)
            | Q(employee__employee_code__icontains=query)
        )

    if month.isdigit() and 1 <= int(month) <= 12:
        payrolls = payrolls.filter(month=int(month))

    if year.isdigit():
        payrolls = payrolls.filter(year=int(year))

    from operations.reporting import period
    from .payments import payment_summary
    paid_start,paid_end=period(request.GET.get("paid_month"))
    paid_summary=payment_summary(paid_start,paid_end)
    return render(request, "core/payroll.html", {
        "paid_summary":paid_summary,
        "payrolls": payrolls,
        "query": query,
        "selected_month": month,
        "selected_year": year,
        "months": Payroll.MONTH_CHOICES,
        "current_year": timezone.localdate().year,
    })


@login_required(login_url="login")
def payroll_create(request):
    return payroll_form_view(request)


@login_required(login_url="login")
def payroll_edit(request, payroll_id):
    return payroll_form_view(request, payroll_id)


def payroll_form_view(request, payroll_id=None):
    payroll = get_object_or_404(Payroll, pk=payroll_id) if payroll_id else Payroll()
    if payroll_id and payroll.payments.filter(voided=False).exists():
        messages.error(request,'Payroll sudah dibayar. Batalkan pencatatan pembayaran terlebih dahulu jika perlu mengoreksi slip.')
        return redirect('payroll_preview',payroll_id=payroll.pk)
    today = timezone.localdate()
    form = PayrollForm(request.POST or None, instance=payroll, initial=None if payroll_id else {'period_type':'range', 'start_date':today, 'end_date':today, 'month': today.month, 'year': today.year, 'manual_deduction': 0})
    bonus_data = request.POST or None
    # Compatible with earlier clients posting payrolls without bonus fields;
    # never erase existing extras when their management form is missing.
    if request.method == 'POST' and 'extras-TOTAL_FORMS' not in request.POST and not (payroll.pk and payroll.extras.exists()):
        bonus_data = request.POST.copy()
        bonus_data.update({'extras-TOTAL_FORMS': '0', 'extras-INITIAL_FORMS': '0'})
    extras = PayrollExtraFormSet(bonus_data, instance=payroll, prefix='extras')
    if request.method == 'POST':
        valid_form, valid_extras = form.is_valid(), extras.is_valid()
        if valid_form and valid_extras:
            updated = form.save(commit=False)
            if not payroll_id:
                from .payroll_calculation import snapshot_payroll
                try:snapshot_payroll(updated)
                except ValidationError as exc:
                    form.add_error(None,exc)
                    return render(request,'core/payroll_form.html',{'form':form,'extras':extras,'page_title':'Buat payroll','back_url':'/payroll/'})
            bonus = sum((f.cleaned_data['amount'] for f in extras.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')), Decimal(0))
            earned = updated.payable_base_salary + updated.fuel_allowance + updated.punctual_bonus + bonus
            if updated.manual_deduction + updated.automatic_deduction > earned:
                form.add_error('manual_deduction', 'Total potongan manual dan absensi tidak boleh melebihi penghasilan termasuk bonus manual.')
            else:
                try:
                    with transaction.atomic():
                        from .payroll_calculation import check_overlap
                        visible_employee = Employee.objects.filter(pk=updated.employee_id)
                        # The UPDATE holds the row lock until the overlap check and save finish.
                        if not visible_employee.update(base_salary=F('base_salary')):
                            raise ValidationError('Karyawan tidak tersedia di cabang aktif.')
                        check_overlap(updated)
                        if updated.pk and updated.payments.filter(voided=False).exists():raise ValidationError('Payroll sudah dibayar; koreksi pencatatan pembayaran dahulu.')
                        updated.save()
                        extras.instance = updated
                        extras.save()
                except ValidationError as exc:
                    form.add_error(None,exc)
                except IntegrityError:
                    form.add_error(None, 'Payroll periode ini sudah ada atau data berubah. Muat ulang halaman.')
                else:
                    messages.success(request, 'Payroll dan rincian bonus manual berhasil disimpan.')
                    return redirect('payroll_list')
    from .payroll_calculation import attendance_review
    return render(request, 'core/payroll_form.html', {
        'form': form, 'extras': extras, 'payroll':payroll if payroll_id else None, 'attendance_review':attendance_review(payroll) if payroll_id else None, 'page_title': 'Edit payroll' if payroll_id else 'Buat payroll', 'back_url': '/payroll/',
        'description': 'Bonus manual hanya berlaku untuk payroll ini. Jangan mengisi bensin yang sama di tunjangan bulanan dan bonus manual. Edit mempertahankan snapshot gaji dan absensi awal.',
    })


@login_required(login_url="login")
def payroll_delete(request, payroll_id):
    payroll = get_object_or_404(Payroll, pk=payroll_id)
    if payroll.payments.exists():
        messages.error(request,'Payroll memiliki riwayat pembayaran dan tidak dapat dihapus. Untuk koreksi, batalkan pencatatan pembayaran lalu edit slip.')
        return redirect('payroll_list')
    form = DeleteConfirmationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            Payroll.objects.filter(pk=payroll_id).update(updated_at=F('updated_at'))
            if payroll.payments.exists():
                messages.error(request,'Payroll memiliki riwayat pembayaran dan tidak dapat dihapus.')
                return redirect('payroll_list')
            payroll.delete()
        messages.success(request, 'Payroll dan bonus terkait dihapus. Absensi dan data karyawan tetap tersimpan.')
        return redirect('payroll_list')
    return render(request, 'core/delete_confirm.html', {
        'form': form, 'page_title': 'Hapus payroll', 'object_name': str(payroll),
        'description': 'Slip dan bonus pada payroll ini akan dihapus. Biaya payroll pada laporan bulan tersebut ikut berkurang. Absensi tetap tersimpan; payroll periode ini bisa dibuat ulang.',
        'back_url': '/payroll/',
    })


@login_required(login_url="login")
def payroll_pdf(request, payroll_id):
    payroll = get_object_or_404(
        Payroll.objects.select_related("employee"),
        pk=payroll_id,
    )

    layout=SlipLayout.objects.filter(pk=1).first() or SlipLayout(pk=1)
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="SlipTitle",
        parent=styles["Title"],
        alignment=0,
        fontSize=21,
        leading=26,
        textColor=colors.HexColor("#172B3A"),
    ))

    def rupiah(value):
        from operations.templatetags.erp import rupiah as format_rupiah
        return format_rupiah(value)

    employee = payroll.employee
    month_name = dict(Payroll.MONTH_CHOICES)[payroll.month]

    from operations.models import BusinessSettings
    business = BusinessSettings.objects.filter(pk=1).first()
    business_name = business.name if business else "USAHA F&B"
    branch_rows=payroll.branch_work_rows
    story = [
        Paragraph(escape(business_name), styles["SlipTitle"]),
        Paragraph(escape(layout.title), styles["Heading2"]),
        Spacer(1, 5 * mm),
    ]

    story.append(Paragraph("PAYROLL / SLIP GAJI", styles["Normal"]))

    employee_data = [
        ["Nama", employee.full_name],
        ["Kode karyawan", employee.employee_code],
        ["Jabatan", payroll.role_name or employee.position],
        ["Periode", payroll.period_label],
    ]

    if layout.show_work_branches:
        branch_text=branch_rows[0]['name'] if len(branch_rows)==1 else '; '.join(f"{row['name']}: {row['days']} hari" for row in branch_rows)
        employee_data.append(['Cabang kerja',branch_text])
    if not layout.show_employee_code:employee_data=[r for r in employee_data if r[0]!='Kode karyawan']
    if not layout.show_position:employee_data=[r for r in employee_data if r[0]!='Jabatan']

    employee_table = Table(
        [[Paragraph(escape(str(v)),styles["Normal"]) for v in row] for row in employee_data],
        colWidths=[45 * mm, 105 * mm],
    )
    employee_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    salary_data = [["Keterangan", "Jumlah"]]
    if layout.show_base_salary:
        label="Gaji pokok periode"
        if payroll.pay_scheme=='daily':
            label=f"Gaji harian ({payroll.present_days+payroll.late_days} hari x {rupiah(payroll.daily_rate)})" if layout.show_rate_details else 'Gaji harian'
        salary_data.append([label,rupiah(payroll.payable_base_salary)])
    if layout.show_fuel:salary_data.append(['Uang bensin',rupiah(payroll.fuel_allowance)])
    if layout.show_punctual_bonus:
        label=f'Bonus tepat waktu ({payroll.punctual_days} hari)' if layout.show_rate_details else 'Bonus tepat waktu'
        salary_data.append([label,rupiah(payroll.punctual_bonus)])
    if layout.show_extras:
        salary_data.extend([[Paragraph(escape('Bonus / tunjangan: '+item.description),styles['Normal']),rupiah(item.amount)] for item in payroll.extras.all()])
    if layout.show_deduction:salary_data.append([Paragraph(escape(payroll.deduction_label),styles['Normal']),'- '+rupiah(payroll.manual_deduction)])
    if layout.show_deduction:
        for row in payroll.deduction_groups:
            label='Potongan '+row['label'].lower()
            if layout.show_rate_details:label+=f" ({row['days']} hari)"
            salary_data.append([label,'- '+rupiah(row['amount'])])
    salary_data.append(['GAJI BERSIH',rupiah(payroll.net_salary)])

    salary_table = Table(
        [[Paragraph(escape(v),styles["Normal"]) if isinstance(v,str) and i==0 and row_index not in (0,len(salary_data)-1) else v for i,v in enumerate(row)] for row_index,row in enumerate(salary_data)],
        repeatRows=1,
        colWidths=[100 * mm, 50 * mm],
    )
    salary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#172B3A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#E2E8ED")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DFF2BC")),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TOPPADDING", (0, -1), (-1, -1), 12),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))

    attendance_data = [
        ["Rekap absensi", "Jumlah hari"],
        ["Hadir", str(payroll.present_days)],
        ["Terlambat", str(payroll.late_days)],
        ["Tidak hadir", str(payroll.absent_days)],
        ["Izin", str(payroll.leave_days)],
        ["Sakit", str(payroll.sick_days)],
        ["Libur jatah / kuota", f"{payroll.quota_used_days} / {payroll.monthly_leave_quota}"],
        ["Kelebihan libur (potongan manual)", str(payroll.excess_off_days)],
        ["Memenuhi bonus tepat waktu", str(payroll.punctual_days)],
    ]

    if payroll.start_date:
        attendance_data = [row for row in attendance_data if row[0] != 'Kelebihan libur (potongan manual)']
        attendance_data = [['Libur tercatat dalam periode', str(payroll.off_days)] if row[0] == 'Libur jatah / kuota' else row for row in attendance_data]

    if payroll.deduction_policy.get('quota_mode')=='shared':
        attendance_data=[['Libur/izin/sakit terpakai / kuota',row[1]] if row[0]=='Libur jatah / kuota' else row for row in attendance_data]

    if payroll.deduction_policy.get('enabled'):
        attendance_data=[['Hari melebihi jatah',row[1]] if row[0]=='Kelebihan libur (potongan manual)' else row for row in attendance_data]

    attendance_table = Table(
        attendance_data,
        repeatRows=1,
        colWidths=[100 * mm, 50 * mm],
    )
    attendance_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EDF5EF")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.35, colors.HexColor("#E2E8ED")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))

    story.extend([employee_table,Spacer(1,5*mm),salary_table])
    if layout.show_attendance:
        story.extend([Spacer(1,5*mm),Paragraph('REKAP ABSENSI',styles['Heading3']),attendance_table])
    if layout.show_footer and layout.footer:
        story.extend([Spacer(1,6*mm),Paragraph(escape(layout.footer),styles['Normal'])])
    if layout.show_notes and payroll.notes:
        story.extend([Spacer(1,4*mm),Paragraph(f'Catatan: {escape(payroll.notes)}',styles['Normal'])])

    def page_chrome(canvas,document):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor('#B8E56C'))
        canvas.rect(20*mm,A4[1]-12*mm,170*mm,2*mm,fill=1,stroke=0)
        canvas.setStrokeColor(colors.HexColor('#E2E8ED'))
        canvas.line(20*mm,16*mm,A4[0]-20*mm,16*mm)
        canvas.setFont('Helvetica',8)
        canvas.setFillColor(colors.HexColor('#647687'))
        canvas.drawString(20*mm,11*mm,'PAYROLL / DOKUMEN KARYAWAN')
        canvas.drawRightString(A4[0]-20*mm,11*mm,f'Halaman {document.page}')
        canvas.restoreState()
    doc.build(story,onFirstPage=page_chrome,onLaterPages=page_chrome)
    pdf = buffer.getvalue()
    buffer.close()

    slip_date = payroll.end_date or date(
        payroll.year, payroll.month, calendar.monthrange(payroll.year, payroll.month)[1]
    )
    month_names = ('januari', 'februari', 'maret', 'april', 'mei', 'juni',
                   'juli', 'agustus', 'september', 'oktober', 'november', 'desember')
    employee_name = slugify(employee.full_name).replace('-', '_') or 'karyawan'
    filename = f"slipgaji_{employee_name}_{slip_date.day:02d}_{month_names[slip_date.month - 1]}.pdf"

    if request.GET.get('format') == 'png':
        from .slip_image import pdf_to_png
        response = HttpResponse(pdf_to_png(pdf), content_type='image/png')
        filename = filename.removesuffix('.pdf') + '.png'
        disposition = 'attachment'
    else:
        response = HttpResponse(pdf, content_type="application/pdf")
        disposition = 'inline' if request.GET.get('inline') == '1' else 'attachment'
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response['Cache-Control'] = 'private, no-store'

    return response




@login_required
def role_list(request):
    return render(request,'core/roles.html',{'roles':JobRole.objects.all()})

@login_required
def role_form(request,role_id=None):
    role=get_object_or_404(JobRole,pk=role_id) if role_id else None
    form=JobRoleForm(request.POST or None,instance=role)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'Jabatan dan aturan gaji disimpan. Payroll lama tetap memakai snapshot awal.');return redirect('role_list')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Edit jabatan' if role else 'Tambah jabatan','back_url':'/roles/','description':'Bulanan memakai gaji pokok; harian memakai tarif per hari Masuk/Masuk terlambat. Izin, sakit, libur dan absen tidak dibayar pada sistem harian.'})

@login_required
def role_delete(request,role_id):
    role=get_object_or_404(JobRole,pk=role_id)
    form=DeleteConfirmationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            role=JobRole.objects.select_for_update().get(pk=role_id)
            Employee.objects.filter(role=role).update(position=role.name,pay_scheme=role.pay_scheme,base_salary=role.base_salary,daily_rate=role.daily_rate,role=None)
            role.delete()
        messages.success(request,'Jabatan dihapus. Data karyawan dan aturan gaji terakhir tetap tersimpan.');return redirect('role_list')
    form.fields['confirm'].label='Saya menyetujui penghapusan jabatan; aturan terakhir disalin ke karyawan terkait.'
    return render(request,'core/delete_confirm.html',{'form':form,'page_title':'Hapus jabatan','object_name':role.name,'description':f'{role.employee_set.count()} karyawan tetap disimpan dengan aturan gaji terakhir; absensi dan payroll tidak dihapus.','back_url':'/roles/'})


@login_required
def payroll_refresh(request,payroll_id):
    from .payroll_calculation import attendance_review,refresh_attendance,current_deduction_policy
    payroll=get_object_or_404(Payroll,pk=payroll_id)
    if payroll.payments.filter(voided=False).exists():
        messages.error(request,'Payroll sudah dibayar. Batalkan pencatatan pembayaran sebelum menghitung ulang slip.')
        return redirect('payroll_preview',payroll_id=payroll.pk)
    form=DeleteConfirmationForm(request.POST or None)
    form.fields['confirm'].label='Saya menyetujui penghitungan ulang dari absensi terbaru. Nominal gaji/slip dan laporan dapat berubah.'
    from django import forms
    form.fields['apply_current_policy']=forms.BooleanField(required=False,label='Gunakan aturan potongan absensi terbaru untuk payroll ini')
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                Employee.objects.filter(pk=payroll.employee_id).update(base_salary=F('base_salary'))
                payroll=Payroll.objects.select_for_update().get(pk=payroll_id)
                if payroll.payments.filter(voided=False).exists():raise ValidationError('Payroll sudah dibayar; koreksi pencatatan pembayaran dahulu.')
                if form.cleaned_data.get('apply_current_policy'):
                    payroll.deduction_policy=current_deduction_policy()
                    if not payroll.monthly_salary_rate:
                        payroll.monthly_salary_rate=payroll.employee.role.base_salary if payroll.employee.role else payroll.employee.base_salary
                refresh_attendance(payroll)
        except ValidationError as exc:form.add_error(None,exc)
        else:
            messages.success(request,'Rekap absensi dan bonus berhasil dihitung ulang. Tarif awal serta bonus manual tetap tersimpan.')
            return redirect('payroll_edit',payroll_id=payroll_id)
    return render(request,'core/payroll_refresh.html',{'form':form,'payroll':payroll,'review':attendance_review(payroll)})


@login_required
def payroll_preview(request,payroll_id):
    payroll=get_object_or_404(Payroll.objects.select_related('employee').prefetch_related('extras'),pk=payroll_id)
    response=render(request,'core/payroll_preview.html',{'payroll':payroll,'layout':SlipLayout.objects.filter(pk=1).first() or SlipLayout(pk=1)})
    response['Cache-Control']='private, no-store'
    return response


@login_required
def slip_layout_settings(request):
    from .forms import SlipLayoutForm
    layout=SlipLayout.objects.filter(pk=1).first() or SlipLayout(pk=1)
    form=SlipLayoutForm(request.POST or None,instance=layout)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'Layout slip disimpan. Preview dan PDF mengikuti pilihan ini; perhitungan gaji tidak berubah.');return redirect('slip_layout_settings')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Atur layout slip gaji','back_url':'/payroll/','description':'Centang bagian yang ingin ditampilkan. Berlaku untuk seluruh preview/PDF, termasuk payroll lama saat dibuka kembali. Nama karyawan, periode dan total gaji bersih selalu tampil. Menyembunyikan rincian tidak mengurangi nominal gaji.'})


@login_required
def payroll_deduction_settings(request):
    from .models import PayrollDeductionPolicy
    from .forms import PayrollDeductionPolicyForm
    policy=PayrollDeductionPolicy.objects.first() or PayrollDeductionPolicy()
    form=PayrollDeductionPolicyForm(request.POST or None,instance=policy)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'Aturan potongan disimpan. Payroll lama tidak berubah otomatis.')
        return redirect('payroll_deduction_settings')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Pengaturan potongan absensi','back_url':'/payroll/','description':'Berlaku untuk gaji bulanan di cabang aktif, termasuk slip mingguan. Mode Gabungan: Libur Jatah + Izin + Sakit memakai jatah yang sama (contoh 2 hari/bulan), hanya kelebihannya dipotong. Jumlah jatah diatur di Edit karyawan. Mode Terpisah: jatah hanya untuk Libur Jatah, izin/sakit mengikuti pilihan potong per hari. Tidak hadir mengikuti pilihan tersendiri. Jatah dihitung kronologis dari awal bulan dan direset setiap bulan. Hari tanpa absensi tidak otomatis dipotong. Gaji harian tidak dipotong lagi. Payroll baru menyimpan aturan saat dibuat; payroll lama perlu Hitung ulang absensi dan pilih aturan terbaru bila ingin diterapkan.'})


@login_required
def account_settings(request):
    from django.contrib.auth import get_user_model
    from .forms import LoginAccountForm
    return render(request,'core/accounts.html',{'account_form':LoginAccountForm(),'accounts':get_user_model().objects.order_by('username') if request.user.is_superuser else []})

@login_required
@require_POST
def account_create(request):
    if not request.user.is_superuser:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden('Hanya administrator yang dapat membuat akun baru.')
    from .forms import LoginAccountForm
    from django.contrib.auth import get_user_model
    form=LoginAccountForm(request.POST)
    if form.is_valid():
        account=form.save(commit=False)
        account.is_staff=False;account.is_superuser=False;account.save()
        messages.success(request,'Akun login baru berhasil dibuat.')
        return redirect('account_settings')
    return render(request,'core/accounts.html',{'account_form':form,'accounts':get_user_model().objects.order_by('username')})

@login_required
def account_username_change(request, user_id):
    from django.contrib.auth import get_user_model
    from django.forms import modelform_factory
    from django.http import HttpResponseForbidden
    from django.urls import reverse

    if not request.user.is_superuser:
        return HttpResponseForbidden('Hanya administrator yang dapat mengganti username.')
    user_model = get_user_model()
    account = get_object_or_404(user_model, pk=user_id)
    username_form = modelform_factory(user_model, fields=('username',))
    form = username_form(request.POST if request.method == 'POST' else None, instance=account)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                updated = user_model.objects.filter(pk=user_id).update(
                    username=form.cleaned_data['username'],
                )
        except IntegrityError:
            form.add_error('username', 'Username sudah dipakai.')
        else:
            if updated:
                messages.success(request, 'Username berhasil diganti.')
                return redirect('account_settings')
            form.add_error(None, 'Akun sudah dihapus.')
    return render(request, 'core/generic_form.html', {
        'form': form,
        'page_title': 'Ganti username',
        'back_url': reverse('account_settings'),
    })


@login_required
def account_delete(request, user_id):
    from django.contrib.auth import get_user_model
    from django.http import HttpResponseForbidden
    from django.urls import reverse

    if not request.user.is_superuser:
        return HttpResponseForbidden('Hanya administrator yang dapat menghapus akun.')
    accounts = get_user_model().objects
    account = get_object_or_404(accounts, pk=user_id)
    if account.is_superuser or account.pk == request.user.pk:
        return HttpResponseForbidden('Akun administrator tidak dapat dihapus dari halaman ini.')

    if request.method == 'POST':
        with transaction.atomic():
            account = get_object_or_404(accounts.select_for_update(), pk=user_id)
            if account.is_superuser:
                return HttpResponseForbidden('Akun administrator tidak dapat dihapus dari halaman ini.')
            try:
                with transaction.atomic():
                    account.delete()
            except ProtectedError:
                account.is_active = False
                account.set_unusable_password()
                account.save(update_fields=('is_active', 'password'))
                messages.success(request, 'Akses akun dicabut. Akun disimpan untuk menjaga riwayat ERP.')
            else:
                messages.success(request, 'Akun berhasil dihapus.')
        return redirect('account_settings')

    return render(request, 'core/account_delete.html', {
        'account': account,
        'back_url': reverse('account_settings'),
    })


@login_required
def account_password_change(request):
    from django.contrib.auth.forms import PasswordChangeForm
    from django.contrib.auth import update_session_auth_hash
    form=PasswordChangeForm(request.user,request.POST or None)
    if request.method=='POST' and form.is_valid():
        user=form.save();update_session_auth_hash(request,user)
        messages.success(request,'Password akunmu berhasil diganti.')
        return redirect('account_settings')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Ganti password akun sendiri','description':'Masukkan password lama dan password baru dua kali. Akun yang diubah adalah '+request.user.get_username()+'.','back_url':'/account/'})


@login_required
def payroll_pay(request,payroll_id):
    from .forms import PayrollPaymentForm
    from .payments import record_payment
    payroll=get_object_or_404(Payroll.objects.select_related('employee').prefetch_related('extras'),pk=payroll_id)
    if payroll.payments.filter(voided=False).exists():
        messages.info(request,'Payroll ini sudah dibayar.');return redirect('payroll_list')
    form=PayrollPaymentForm(request.POST or None,initial={'expected_amount':payroll.net_salary})
    if request.method=='POST' and form.is_valid():
        try:record_payment(payroll.pk,form.cleaned_data['paid_date'],form.cleaned_data['notes'],request.user,expected_amount=form.cleaned_data['expected_amount'])
        except (ValidationError,IntegrityError) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else 'Pembayaran sudah tercatat; muat ulang halaman.')
        else:
            messages.success(request,'Pembayaran gaji dicatat. Total mengikuti bulan tanggal pembayaran.')
            from django.urls import reverse
            return redirect(reverse('payroll_list')+'?paid_month='+form.cleaned_data['paid_date'].strftime('%Y-%m'))
    from operations.templatetags.erp import rupiah
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Catat gaji sudah dibayar','description':payroll.employee.full_name+' · '+payroll.period_label+' · '+rupiah(payroll.net_salary)+'. Klik Simpan hanya setelah gaji benar-benar diberikan. Nominal adalah pelunasan seluruh gaji bersih; bukan pembayaran cicilan.','back_url':'/payroll/'})

@login_required
def payroll_payment_cancel(request,payment_id):
    from .models import PayrollPayment
    from .payments import cancel_payment
    from operations.forms import ReasonForm
    payment=get_object_or_404(PayrollPayment,pk=payment_id)
    form=ReasonForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        cancel_payment(payment.pk,form.cleaned_data['reason'])
        messages.success(request,'Pencatatan pembayaran dibatalkan; riwayat tetap tersimpan. Ini tidak melakukan pengembalian uang.')
        from django.urls import reverse
        return redirect(reverse('payroll_list')+'?paid_month='+payment.paid_date.strftime('%Y-%m'))
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Batalkan pencatatan pembayaran gaji','description':payment.employee_name+' · '+payment.period_label+'. Gunakan hanya untuk koreksi catatan yang salah. Total gaji dibayarkan akan berkurang; riwayat tetap ada dan tidak melakukan transfer/refund.','back_url':'/payroll/'})


@login_required
def employee_import_branch(request):
    from tenancy.models import Branch
    from .employee_import import import_employees
    from .employee_scope import employee_visibility
    sources=Branch.objects.exclude(pk=request.branch.pk)
    source_id=request.POST.get('source','') if request.method=='POST' else request.GET.get('source','')
    source=get_object_or_404(sources,pk=int(source_id)) if source_id.isdigit() and len(source_id)<19 else (None if request.method=='POST' else sources.first())
    people=Employee._base_manager.filter(employee_visibility(source.pk)).distinct().select_related('branch') if source else Employee._base_manager.none()
    if request.method=='POST':
        ids=request.POST.getlist('employees')
        if not source or not ids or len(ids)>500 or any(not i.isdigit() or len(i)>18 for i in ids):
            messages.error(request,'Pilih cabang sumber dan minimal satu karyawan yang valid.')
        elif request.POST.get('confirm')!='on':messages.error(request,'Centang konfirmasi untuk menghubungkan absensi.')
        else:
            try:
                added,skipped=import_employees(source.pk,request.branch.pk,ids)
            except ValidationError as exc:messages.error(request,'; '.join(exc.messages))
            else:
                messages.success(request,f'{added} karyawan dihubungkan; {skipped} sudah tersedia. Absensi memakai data yang sama, tanpa salinan.')
                return redirect('employee_list')
    return render(request,'core/employee_import.html',{'sources':sources,'source':source,'people':people})
