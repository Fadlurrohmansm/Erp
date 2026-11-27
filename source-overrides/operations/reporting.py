import calendar
from datetime import date
from decimal import Decimal
from django.db.models import Count, Q, Sum
from django.utils import timezone
from core.models import Payroll
from .models import Sale, Expense, StockMovement, Ingredient, ZERO


def period(value):
    today = timezone.localdate()
    try:
        year, month = map(int, value.split('-'))
        if not (2000 <= year <= 2100 and 1 <= month <= 12):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        year, month = today.year, today.month
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def recorded_portions(manual_total, imports_with_quantities):
    """A manual count, including zero, takes precedence for the same recap."""
    return (manual_total or 0) + sum(
        quantities['portions']
        for record, quantities in imports_with_quantities
        if record.sale.manual_portions is None
    )


def summary(start, end, daily=False):
    sales = Sale.objects.filter(date__range=(start,end), status='posted')
    offline = Q(channel_name__iexact='offline')
    totals = sales.aggregate(
        subtotal=Sum('subtotal', default=ZERO),
        vouchers=Sum('voucher_amount', default=ZERO),
        commissions=Sum('commission_amount', default=ZERO),
        other_fees=Sum('other_fees', default=ZERO),
        revenue=Sum('net_revenue', default=ZERO),
        total_cost=Sum('cost', default=ZERO),
        orders=Sum('order_count', default=0),
        manual_portions=Sum('manual_portions', default=0),
        manual_cash=Sum('manual_cash', filter=offline, default=ZERO),
        manual_qris=Sum('manual_qris', filter=offline, default=ZERO),
        recaps=Count('pk', filter=Q(is_recap=True)),
        estimates=Count('pk', filter=Q(cost_estimated=True)),
        zero_hpp_sales=Count('pk', filter=Q(cost=0, net_revenue__gt=0)),
    )
    totals['cost'] = totals.pop('total_cost')
    totals['has_recaps'] = bool(totals.pop('recaps'))
    totals['estimated_hpp'] = bool(totals.pop('estimates'))
    manual = {key: totals.pop(f'manual_{key}') for key in ('cash', 'qris')}
    manual_portions = totals.pop('manual_portions')
    expenses = Expense.objects.filter(date__range=(start,end), voided=False)
    totals['expenses'] = expenses.aggregate(total=Sum('amount'))['total'] or ZERO
    payrolls = Payroll.objects.filter(year=start.year, month=start.month)
    if daily:
        month_end=date(start.year,start.month,calendar.monthrange(start.year,start.month)[1])
        payrolls=payrolls.filter(Q(end_date=start)|Q(end_date__isnull=True) if start==month_end else Q(end_date=start))
    # Net salary is a model property; prefetch its bonus rows to avoid one query per slip.
    totals['payroll'] = sum((payroll.net_salary for payroll in payrolls.order_by().prefetch_related('extras')), ZERO)
    # Selisih stok fisik positif mengurangi biaya; negatif menambah biaya.
    stock_totals = StockMovement.objects.filter(date__range=(start, end), kind__in=['count', 'purchase']).aggregate(
        adjustments=Sum('value', filter=Q(kind='count'), default=ZERO),
        purchases=Sum('value', filter=Q(kind='purchase'), default=ZERO),
    )
    adjustments = stock_totals['adjustments']
    # HPP POS sudah mencakup pemakaian bahan; opname pada periode rekap hanya mengoreksi stok.
    if totals['has_recaps']:
        adjustments = ZERO
    totals['inventory_adjustment'] = adjustments
    totals['gross_profit'] = totals['revenue'] - totals['cost']
    totals['profit'] = totals['gross_profit'] - totals['expenses'] - totals['payroll'] + adjustments
    from .models import POSCostImport
    imported=POSCostImport.objects.select_related('sale').filter(sale__date__range=(start,end),sale__status='posted')
    imported=list(imported)
    payments=[r.payments for r in imported if r.sale.channel_name.casefold()=='offline' and r.payments]
    totals['pos_payments']={key:{'orders':sum(p.get(key,{}).get('orders',0) for p in payments),'amount':sum((Decimal(p.get(key,{}).get('amount','0')) for p in payments),ZERO)} for key in ['cash','qris','transfer','noncash','mixed','unknown']}
    offline_imports = [
        record.payments for record in imported
        if record.sale.channel_name.casefold() == 'offline'
        and record.payments and not record.sale.has_manual_payments
    ]
    totals['offline_payments'] = {
        key: {
            'orders': sum(payment.get(key, {}).get('orders', 0) for payment in offline_imports),
            'amount': (manual.get(key) or ZERO) + sum(
                (Decimal(payment.get(key, {}).get('amount', '0')) for payment in offline_imports), ZERO,
            ),
        }
        for key in totals['pos_payments']
    }
    imports_with_quantities = [(record, record.quantities) for record in imported]
    quantities = [quantity for _, quantity in imports_with_quantities]
    totals['pos_portions']=sum(q['portions'] for q in quantities)
    totals['portions'] = recorded_portions(manual_portions, imports_with_quantities)
    totals['pos_toppings']=sum(q['toppings'] for q in quantities)
    totals['pos_others']=sum(q.get('others',0) for q in quantities)
    totals['pos_imports']=len(quantities)
    totals['margin'] = totals['profit'] / totals['revenue'] * 100 if totals['revenue'] else ZERO
    totals['purchases'] = stock_totals['purchases']
    totals['inventory_value'] = sum((i.stock_value for i in Ingredient.objects.all()), ZERO)
    totals['channels'] = list(sales.values('channel_name').annotate(total=Sum('net_revenue')).order_by('-total'))
    totals['expense_categories'] = list(expenses.values('category').annotate(total=Sum('amount')).order_by('-total'))
    totals['daily'] = list(sales.values('date').annotate(total=Sum('net_revenue'), cost=Sum('cost'), gross=Sum('subtotal'), orders=Sum('order_count')).order_by('date'))
    max_value = max((d['total'] for d in totals['daily']), default=Decimal(1)) or Decimal(1)
    for d in totals['daily']:
        d['height'] = max(2, round(d['total'] / max_value * 100))
    # Server-generated SVG geometry uses real daily figures, never illustrative data.
    from .templatetags.erp import rupiah
    values = [ZERO]
    for item in totals['daily']:
        item['gross_profit'] = item['total'] - item['cost']
        values.extend([item['total'],item['gross_profit']])
    low, high = min(values), max(values)
    if high == low:high = low + 1
    chart = {'ticks':[], 'points':[]}
    for n in range(5):
        value = low + (high-low)*Decimal(4-n)/4
        chart['ticks'].append({'y':str(25+n*50),'label':rupiah(value)})
    rev,profit = [],[]
    for n,item in enumerate(totals['daily']):
        x = Decimal(110) + Decimal(565)*n/max(1,len(totals['daily'])-1)
        y = Decimal(225)-(item['total']-low)/(high-low)*200
        py = Decimal(225)-(item['gross_profit']-low)/(high-low)*200
        rev.append(f'{x:.2f},{y:.2f}');profit.append(f'{x:.2f},{py:.2f}')
        chart['points'].append({'x':f'{x:.2f}','y':f'{y:.2f}','py':f'{py:.2f}','date':item['date'],'revenue':item['total'],'profit':item['gross_profit']})
    chart['revenue'] = ' '.join(rev);chart['profit'] = ' '.join(profit)
    totals['chart'] = chart
    colors = ['#a38ae8','#ffbf58','#50cbd0','#c0d742','#88a9d4']
    total = totals['revenue'];cursor = ZERO;parts=[]
    for n,channel in enumerate(totals['channels']):
        share = channel['total']/total*100 if total else ZERO
        channel['share']=share;channel['color']=colors[n%len(colors)]
        parts.append(f"{channel['color']} {cursor:.3f}% {cursor+share:.3f}%")
        cursor += share
    totals['channel_gradient'] = ','.join(parts) if parts else '#eaecf0 0% 100%'
    return totals


def delivery_daily(value=None):
    """Gross-sales shares: online subtotal is a summary, never a fifth channel."""
    import re
    try:
        day=date.fromisoformat(value)
        if not 2000<=day.year<=2100:raise ValueError
    except (TypeError,ValueError):day=timezone.localdate()
    definitions=[('shopeefood','ShopeeFood','#ee9556'),('grabfood','GrabFood','#59b996'),('gofood','GoFood','#d97880'),('offline','Offline','#9b88d1')]
    rows={key:dict(key=key,name=name,color=color,gross=ZERO,net=ZERO,orders=0) for key,name,color in definitions}
    others=dict(key='other',name='Kanal lainnya',color='#8c9daa',gross=ZERO,net=ZERO,orders=0)
    query=Sale.objects.filter(date=day,status='posted').values('channel_name').annotate(gross=Sum('subtotal'),net=Sum('net_revenue'),orders=Sum('order_count'))
    for item in query:
        key=re.sub(r'[^a-z0-9]','',item['channel_name'].casefold())
        target=rows.get(key,others)
        target['gross']+=item['gross'] or ZERO;target['net']+=item['net'] or ZERO;target['orders']+=item['orders'] or 0
    channels=list(rows.values())
    if others['gross'] or others['net'] or others['orders']:channels.append(others)
    total=sum((r['gross'] for r in channels),ZERO)
    net=sum((r['net'] for r in channels),ZERO)
    orders=sum(r['orders'] for r in channels)
    cursor=ZERO;segments=[]
    for row in channels:
        share=row['gross']/total*100 if total else ZERO
        row['share']=share;row['width']=f'{share:.3f}'
        segments.append(f"{row['color']} {cursor:.3f}% {cursor+share:.3f}%")
        cursor+=share
    online=sum((rows[k]['gross'] for k in ['shopeefood','grabfood','gofood']),ZERO)
    online_net=sum((rows[k]['net'] for k in ['shopeefood','grabfood','gofood']),ZERO)
    online_orders=sum(rows[k]['orders'] for k in ['shopeefood','grabfood','gofood'])
    leader=max(channels,key=lambda r:r['gross']) if total else None
    return dict(date=day,date_value=day.isoformat(),channels=channels,gross=total,net=net,orders=orders,
                online=online,online_net=online_net,online_orders=online_orders,online_share=online/total*100 if total else ZERO,
                offline=rows['offline']['gross'],offline_share=rows['offline']['share'],leader=leader,
                gradient=','.join(segments) if total else '#e9ecf0 0% 100%')
