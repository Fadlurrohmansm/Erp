from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone
from .models import Payroll,PayrollPayment

@transaction.atomic
def record_payment(payroll_id,paid_date,notes,user,expected_amount=None):
    Payroll.objects.filter(pk=payroll_id).update(updated_at=F('updated_at'))
    payroll=Payroll.objects.select_for_update().select_related('employee').prefetch_related('extras').get(pk=payroll_id)
    if payroll.payments.filter(voided=False).exists():raise ValidationError('Payroll ini sudah dicatat dibayar. Tidak ada pembayaran ganda yang ditambahkan.')
    if paid_date>timezone.localdate():raise ValidationError('Tanggal pembayaran tidak boleh di masa depan.')
    if expected_amount is not None and payroll.net_salary!=expected_amount:raise ValidationError('Nominal slip berubah sejak halaman dibuka. Muat ulang dan periksa nominal sebelum mencatat pembayaran.')
    if payroll.net_salary<=0:raise ValidationError('Gaji bersih harus lebih dari Rp0 untuk dicatat sebagai pembayaran.')
    payment=PayrollPayment(payroll=payroll,branch_id=payroll.branch_id,paid_date=paid_date,amount=payroll.net_salary,employee_name=payroll.employee.full_name,period_label=payroll.period_label,notes=notes,created_by=user)
    payment.full_clean();payment.save();return payment

@transaction.atomic
def cancel_payment(payment_id,reason):
    payment=PayrollPayment.objects.get(pk=payment_id)
    Payroll.objects.filter(pk=payment.payroll_id).update(updated_at=F('updated_at'))
    if not reason.strip():raise ValidationError('Isi alasan koreksi pembayaran.')
    return PayrollPayment.objects.filter(pk=payment_id,voided=False).update(voided=True,void_reason=reason.strip(),voided_at=timezone.now())

def payment_summary(start,end):
    rows=PayrollPayment.objects.filter(paid_date__range=(start,end))
    paid=rows.filter(voided=False)
    totals = paid.aggregate(
        total=Sum('amount', default=Decimal(0)),
        count=Count('pk'),
        employee_count=Count('payroll__employee_id', distinct=True),
    )
    return {**totals, 'rows': rows, 'month_value': start.strftime('%Y-%m')}
