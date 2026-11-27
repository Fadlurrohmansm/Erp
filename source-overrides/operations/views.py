import csv
from decimal import Decimal
import io
import sqlite3
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q, F, Sum, Count
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.models import Employee, Attendance, Payroll
from .models import Ingredient, Product, Sale, Expense, StockMovement, BusinessSettings, Channel, SaleItem
from .forms import IngredientForm, ProductForm, RecipeFormSet, PurchaseForm, CountForm, SaleForm, SaleFormSet, ExpenseForm, BusinessForm, ChannelFormSet, ReasonForm
from .services import receive_stock, count_stock, post_sale, void_sale, post_daily_recap
from .forms import DailyRecapForm
from .reporting import period, summary, delivery_daily, recorded_portions
from .dashboard_display import dashboard_display


def page(request, objects, size=20):
    return Paginator(objects, size).get_page(request.GET.get('page'))


def errors(request, exc):
    text = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'Transaksi sedang diproses atau sudah tercatat. Periksa riwayat lalu coba kembali.'
    messages.error(request, text)


@login_required
def dashboard(request):
    start,end=period(request.GET.get('month'))
    data=summary(start,end)
    ranked_days = sorted(
        (row for row in data['daily'] if row['gross'] > 0),
        key=lambda row: (-row['gross'], -row['orders'], row['date']),
    )
    top_days = [
        {key: row[key] for key in ('date', 'gross', 'orders')} | {'net': row['total']}
        for row in ranked_days[:5]
    ]
    peak=top_days[0]['gross'] if top_days else 0
    for row in top_days:
        row['bar_width']=round(row['gross']/peak*100) if peak else 0
    data['top_days']=top_days

    from core.payments import payment_summary
    salary_paid=payment_summary(start,end)
    today=timezone.localdate()
    att_month=request.GET.get('attendance_month','')
    att_start,att_end=period(att_month) if att_month else (None,today)
    employees=list(Employee.objects.all())
    last=min(att_end,today)
    attendance_query=Attendance.objects.filter(date__lte=last, date__gte=F('employee__hire_date'))
    if att_start:
        attendance_query=attendance_query.filter(date__gte=att_start)
    attendance_totals = {
        item['employee_id']: item
        for item in attendance_query.order_by().values('employee_id').annotate(
            total=Count('pk'),
            present=Count('pk', filter=Q(status__in=['present','late'])),
            punctual=Count('pk', filter=Q(status='present', punctual=True)),
            leave=Count('pk', filter=Q(status='leave')),
            sick=Count('pk', filter=Q(status='sick')),
            off=Count('pk', filter=Q(status='off')),
            absent=Count('pk', filter=Q(status='absent')),
        )
    }
    rows=[]
    for employee in employees:
        first=max(employee.hire_date,att_start) if att_start else employee.hire_date
        counts=attendance_totals.get(employee.pk,{})
        total_days=max(0,(last-first).days+1)
        rows.append({'employee':employee,'start':first,'end':last,'present':counts.get('present',0),'punctual':counts.get('punctual',0),'leave':counts.get('leave',0),'sick':counts.get('sick',0),'off':counts.get('off',0),'absent':counts.get('absent',0),'missing':max(0,total_days-counts.get('total',0))})
    today_sales=Sale.objects.filter(date=today,status='posted')
    from .models import POSCostImport
    today_imports = POSCostImport.objects.select_related('sale').filter(
        sale__in=today_sales, sale__manual_portions__isnull=True,
    )
    today_totals = today_sales.aggregate(
        revenue=Sum('net_revenue'), orders=Sum('order_count'), manual_portions=Sum('manual_portions'),
    )
    data['today_portions'] = recorded_portions(
        today_totals['manual_portions'], ((record, record.quantities) for record in today_imports),
    )
    data.update(today_revenue=today_totals['revenue'] or 0, today_orders=today_totals['orders'] or 0)
    delivery = delivery_daily(request.GET.get('day'))
    display = dashboard_display(delivery['date'], today, start, end, data['daily'])
    return render(request,'core/dashboard.html', {'salary_paid':salary_paid,'attendance_rows':rows,'attendance_month':att_month,'stats':data,'delivery':delivery,'display':display,'month_value':start.strftime('%Y-%m'),'start':start,'recent_sales':Sale.objects.select_related('channel')[:5], 'low_stock':Ingredient.objects.filter(active=True,stock__lte=F('minimum_stock'))[:6], 'active_employees':sum(employee.status == 'active' for employee in employees), 'present_today':Attendance.objects.filter(date=today,status__in=['present','late']).count(), 'today':today})


@login_required
def product_list(request):
    import json
    from urllib.parse import urlencode
    from .models import MenuCategory,MenuLayoutPreference
    layout=MenuLayoutPreference.objects.filter(user=request.user).first()
    categories=list(MenuCategory.objects.all())
    layout_all=layout.show_all if layout else True
    visible_ids=layout.category_ids if layout else []
    layout_none=layout.show_uncategorized if layout else True
    clear_key=f'menu_selection_clear_{request.branch.pk}'
    if request.method=='POST':
        ids=request.POST.getlist('selected')
        try:
            if request.POST.get('selected_all'):
                raw=request.POST['selected_all']
                if len(raw)>100000:raise ValueError()
                saved=json.loads(raw)
                if not isinstance(saved,list) or len(saved)>5000:raise ValueError()
                ids+=saved
            if len(ids)>5020 or any(not isinstance(x,str) or not x.isdigit() or len(x)>18 for x in ids):raise ValueError()
        except (ValueError,TypeError):
            messages.error(request,'Daftar pilihan menu tidak valid. Silakan pilih ulang.')
            return redirect('product_list')
        selected=Product.objects.filter(pk__in=set(ids))
        name=request.POST.get('category_name','').strip()
        category_id=request.POST.get('category','')
        if not selected.exists():messages.error(request,'Centang minimal satu menu.')
        elif len(name)>100:messages.error(request,'Nama kategori maksimal 100 karakter.')
        elif name or category_id:
            if not name and not category_id.isdigit():
                messages.error(request,'Pilih kategori yang valid.');return redirect('product_list')
            with transaction.atomic():
                category=MenuCategory.objects.filter(name__iexact=name).first() if name else get_object_or_404(MenuCategory,pk=category_id)
                if category is None:category=MenuCategory.objects.create(name=name)
                count=selected.update(category=category)
            request.session[clear_key]=True
            messages.success(request,f'{count} menu dimasukkan ke kategori {category.name}.')
            return redirect(reverse('product_list')+'?'+urlencode({'category':category.pk}))
        else:messages.error(request,'Isi nama kategori atau pilih kategori yang tersedia.')
        return redirect('product_list')
    products=Product.objects.select_related('category').prefetch_related('recipes__ingredient').all()
    q=request.GET.get('q','').strip()
    if q:products=products.filter(name__icontains=q)
    category_value=request.GET.get('category','')
    selected_category=None
    if category_value.isdigit():
        selected_category=get_object_or_404(MenuCategory,pk=category_value)
        products=products.filter(category=selected_category)
    elif category_value=='none':products=products.filter(category__isnull=True)
    elif category_value!='all' and not layout_all:
        visible=Q(category_id__in=visible_ids)
        if layout_none:visible|=Q(category__isnull=True)
        products=products.filter(visible)
    counts={r['category_id']:r['n'] for r in products.values('category_id').annotate(n=Count('pk'))}
    ordered_products=products.order_by('category__name','name','pk')
    # Show-all means every matching menu, including the final categories.
    # Keep the Page interface for totals/templates, but ignore stale page links.
    show_all_menus=category_value=='all' or (not category_value and layout_all)
    products=(Paginator(ordered_products,max(sum(counts.values()),1)).get_page(1)
              if show_all_menus else page(request,ordered_products))
    groups={}
    for product in products:
        key=product.category_id
        if key not in groups:groups[key]={'name':product.category.name if product.category else 'Tanpa kategori','products':[],'total':counts.get(key,0)}
        groups[key]['products'].append(product)
    base_params={'q':q,'category':category_value}
    pagination={'previous':('?'+urlencode({**base_params,'page':products.previous_page_number()})) if products.has_previous() else '', 'next':('?'+urlencode({**base_params,'page':products.next_page_number()})) if products.has_next() else ''}
    return render(request,'operations/products.html',{'products':products,'groups':list(groups.values()),'q':q,'categories':categories,'layout_all':layout_all,'layout_none':layout_none,'layout_categories':[{'category':c,'checked':layout_all or c.pk in visible_ids} for c in categories],'layout_error':request.session.pop('menu_layout_error',False),'category_value':category_value,'selected_category':selected_category,'pagination':pagination,'clear_menu_selection':request.session.pop(clear_key,False)})


@login_required
@require_POST
def menu_layout_save(request):
    from .models import MenuCategory,MenuLayoutPreference
    show_all=request.POST.get('show_all')=='on'
    none=request.POST.get('show_uncategorized')=='on'
    ids=request.POST.getlist('visible_categories')
    valid=list(MenuCategory.objects.values_list('pk',flat=True))
    if len(ids)>500 or any(not i.isdigit() or len(i)>18 or int(i) not in valid for i in ids):
        messages.error(request,'Pilihan kategori layout tidak valid.')
        request.session['menu_layout_error']=True
        return redirect('product_list')
    chosen=sorted({int(i) for i in ids})
    if not show_all and not chosen and not none:
        messages.error(request,'Centang Tampilkan semua, minimal satu kategori, atau Tanpa kategori.')
        request.session['menu_layout_error']=True
        return redirect('product_list')
    MenuLayoutPreference.objects.update_or_create(user=request.user,defaults={'show_all':show_all,'category_ids':chosen,'show_uncategorized':none})
    messages.success(request,'Layout menu disimpan untuk akun dan cabang ini.')
    return redirect('product_list')


@login_required
def product_form(request,pk=None):
    product=get_object_or_404(Product,pk=pk) if pk else Product()
    form=ProductForm(request.POST or None,instance=product)
    recipes=RecipeFormSet(request.POST or None, instance=product, prefix='recipes')
    if request.method=='POST' and form.is_valid() and recipes.is_valid():
        with transaction.atomic():
            from .hpp import record_recipe
            form.save()
            recipes.save()
            record_recipe(product, request.user)
        messages.success(request,'Menu dan resep berhasil disimpan.')
        return redirect('product_list')
    return render(request,'operations/product_form.html',{'form':form,'recipes':recipes,'page_title':'Edit menu & resep' if pk else 'Tambah menu & resep'})


@login_required
def inventory(request):
    ingredients=Ingredient.objects.all()
    q=request.GET.get('q','').strip()
    if q: ingredients=ingredients.filter(name__icontains=q)
    if request.GET.get('low')=='1': ingredients=ingredients.filter(stock__lte=F('minimum_stock'))
    return render(request,'operations/inventory.html',{'ingredients':page(request,ingredients),'q':q,'low':request.GET.get('low','')})


@login_required
def ingredient_form(request,pk=None):
    ingredient=get_object_or_404(Ingredient,pk=pk) if pk else None
    form=IngredientForm(request.POST or None,instance=ingredient)
    if request.method=='POST' and form.is_valid():
        form.save();messages.success(request,'Data bahan disimpan. Tambah stok melalui pembelian.')
        return redirect('inventory')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Edit bahan' if pk else 'Tambah bahan','back_url':reverse('inventory'),'description':'Gunakan satuan dasar yang sama untuk resep dan pembelian. Contoh: 1 kg = 1.000 gram.'})


@login_required
def stock_entry(request,kind):
    cls=PurchaseForm if kind=='purchase' else CountForm
    form=cls(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            if kind=='purchase':receive_stock(user=request.user,**form.cleaned_data)
            else:count_stock(user=request.user,**form.cleaned_data)
        except (ValidationError,IntegrityError,OperationalError) as exc: errors(request,exc)
        else:
            messages.success(request,'Stok dan riwayat mutasi berhasil diperbarui.')
            return redirect('inventory')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Catat pembelian' if kind=='purchase' else 'Stok opname','description':'Masukkan jumlah dalam gram, ml, atau pcs sesuai satuan bahan. Nilai stok memakai biaya rata-rata saat pencatatan.','back_url':reverse('inventory')})


@login_required
def movements(request):
    entries=StockMovement.objects.select_related('ingredient','sale','created_by')
    ingredient=request.GET.get('ingredient','')
    if ingredient.isdigit():entries=entries.filter(ingredient_id=ingredient)
    return render(request,'operations/movements.html',{'entries':page(request,entries),'ingredients':Ingredient.objects.all(),'selected_ingredient':ingredient})


@login_required
def sale_list(request):
    start,end=period(request.GET.get('month'))
    sales=Sale.objects.select_related('channel','pos_cost_import').filter(date__range=(start,end))
    q=request.GET.get('q','').strip()
    if q:sales=sales.filter(Q(notes__icontains=q)|Q(channel_name__icontains=q))
    return render(request,'operations/sales.html',{'sales':page(request,sales),'q':q,'month_value':start.strftime('%Y-%m')})


@login_required
def sale_create(request):
    form=DailyRecapForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            sale=post_daily_recap(data=form.cleaned_data,user=request.user)
        except (ValidationError,IntegrityError,OperationalError) as exc:errors(request,exc)
        else:
            messages.success(request,'Rekap manual berhasil disimpan.')
            return redirect('sale_detail',pk=sale.pk)
    payment_names = ('manual_cash', 'manual_qris')
    return render(request, 'operations/daily_recap_form.html', {
        'form': form,
        'recap_fields': [field for field in form if field.name not in payment_names],
        'payment_fields': [form[name] for name in payment_names],
    })


@login_required
def sale_detail(request,pk):
    sale=get_object_or_404(Sale.objects.prefetch_related('items'),pk=pk)
    return render(request,'operations/sale_detail.html',{'sale':sale})


@login_required
def sale_cancel(request,pk):
    sale=get_object_or_404(Sale,pk=pk)
    form=ReasonForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try: void_sale(sale,user=request.user,reason=form.cleaned_data['reason'])
        except (ValidationError,OperationalError) as exc:errors(request,exc)
        else:
            messages.success(request,'Rekap dibatalkan.' if sale.is_recap else 'Penjualan dibatalkan dan stok resep dikembalikan.')
            return redirect('sale_detail',pk=pk)
    return render(request,'core/generic_form.html',{'form':form,'page_title':f'Batalkan {sale.number}','description':'Pembatalan mengoreksi laporan tanggal asli. Untuk koreksi rekap POS, batalkan lalu input ulang tanggal dan kanal yang sama. Rekap POS tidak mengubah stok.' if sale.is_recap else 'Batalkan hanya jika bahan belum terpakai; stok resep akan dikembalikan.','back_url':reverse('sale_detail',args=[pk])})


@login_required
def expense_list(request):
    start,end=period(request.GET.get('month'))
    entries=Expense.objects.filter(date__range=(start,end))
    return render(request,'operations/expenses.html',{'entries':page(request,entries),'month_value':start.strftime('%Y-%m')})


@login_required
def expense_create(request):
    form=ExpenseForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        expense=form.save(commit=False);expense.created_by=request.user
        Expense.objects.get_or_create(token=form.cleaned_data['token'],defaults={'date':expense.date,'category':expense.category,'description':expense.description,'amount':expense.amount,'created_by':request.user})
        messages.success(request,'Pengeluaran operasional disimpan.')
        return redirect('expense_list')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Catat pengeluaran','description':'Untuk sewa, listrik, promosi, dan biaya operasional lainnya. Pembelian bahan dicatat di Pembelian; gaji di Payroll agar biaya tidak dihitung dua kali.','back_url':reverse('expense_list')})


@login_required
def expense_cancel(request,pk):
    expense=get_object_or_404(Expense,pk=pk)
    form=ReasonForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        Expense.objects.filter(pk=pk,voided=False).update(voided=True,void_reason=form.cleaned_data['reason'])
        messages.success(request,'Pengeluaran dibatalkan; riwayat tetap tersimpan.')
        return redirect('expense_list')
    return render(request,'core/generic_form.html',{'form':form,'page_title':'Batalkan pengeluaran','description':expense.description,'back_url':reverse('expense_list')})


@login_required
def reports(request):
    from datetime import date
    start,end=period(request.GET.get('month'))
    daily=request.GET.get('mode')=='daily'
    if daily:
        try:start=end=date.fromisoformat(request.GET.get('day',''))
        except ValueError:start=end=timezone.localdate()
    data=summary(start,end,daily=daily)
    if request.GET.get('export')=='csv':
        response=HttpResponse(content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition']=f'attachment; filename="Laporan_{start:%Y-%m-%d}.csv"'
        response.write('\ufeff')
        writer=csv.writer(response)
        writer.writerow(['Laporan laba operasional',start.isoformat(),end.isoformat()])
        writer.writerow(['Tunai Offline POS',data['offline_payments']['cash']['amount'],data['offline_payments']['cash']['orders']])
        writer.writerow(['QRIS Offline POS',data['offline_payments']['qris']['amount'],data['offline_payments']['qris']['orders']])
        for key,label in [('transfer','Transfer'),('noncash','Non-tunai umum'),('mixed','Campuran'),('unknown','Belum diketahui')]:writer.writerow([label+' Offline POS',data['offline_payments'][key]['amount'],data['offline_payments'][key]['orders']])
        writer.writerow(['Porsi menu utama terjual', data['portions']])
        writer.writerow(['Unit produk utama dari XLSX POS',data['pos_portions']])
        writer.writerow(['Tambahan dari XLSX POS',data['pos_toppings']])
        writer.writerow(['Dasar HPP', 'Memuat estimasi HPP otomatis; laba masih estimasi' if data['estimated_hpp'] else 'HPP tercatat'])
        for label,key in [('Penjualan bruto','subtotal'),('Subsidi voucher','vouchers'),('Komisi platform','commissions'),('Biaya lain','other_fees'),('Pendapatan bersih','revenue'),('HPP terjual','cost'),('Laba kotor','gross_profit'),('Biaya operasional','expenses'),('Payroll periode','payroll'),('Penyesuaian persediaan (+/-)','inventory_adjustment'),('Laba operasional','profit'),('Pembelian persediaan (informasi)','purchases')]:writer.writerow([label,data[key]])
        return response
    return render(request,'operations/reports.html',{'stats':data,'month_value':start.strftime('%Y-%m'),'start':start,'categories':dict(Expense.CATEGORIES),'daily':daily,'day_value':start.isoformat()})


@login_required
def business_settings(request):
    instance=BusinessSettings.objects.filter(pk=1).first() or BusinessSettings(pk=1)
    form=BusinessForm(request.POST or None,instance=instance)
    channels=ChannelFormSet(request.POST or None,queryset=Channel.objects.all(),prefix='channels')
    if request.method=='POST' and form.is_valid() and channels.is_valid():
        with transaction.atomic():form.save();channels.save()
        messages.success(request,'Pengaturan usaha dan kanal diperbarui.')
        return redirect('business_settings')
    return render(request,'operations/settings.html',{'form':form,'channels':channels})


@login_required
@require_POST
def backup_download(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Backup hanya tersedia untuk pemilik akun superuser.')
    if settings.DATABASES['default']['ENGINE']!='django.db.backends.sqlite3':
        return HttpResponse('Backup ini tersedia untuk SQLite.',status=400)
    import tempfile
    import os
    handle,path=tempfile.mkstemp(suffix='.sqlite3');os.close(handle)
    try:
        with sqlite3.connect(str(settings.DATABASES['default']['NAME'])) as source,sqlite3.connect(path) as dest:
            source.backup(dest)
        with open(path,'rb') as f:response=HttpResponse(f.read(),content_type='application/octet-stream')
        response['Content-Disposition']=f'attachment; filename="ERP_FNB_{timezone.localdate():%Y%m%d}.sqlite3"'
        return response
    finally:os.unlink(path)


@login_required
def sale_import(request):
    from .forms import CSVUploadForm, ImportCostsForm
    from .csv_import import preview_delivery_batch, commit_delivery_batch
    if request.GET.get('template') == '1':
        response = HttpResponse('tanggal,kanal,id_pesanan,omzet,voucher,komisi,biaya_lain,pendapatan_bersih,status\n', content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="template_delivery.csv"'
        return response
    form = CSVUploadForm()
    draft = request.session.get('csv_import_draft')
    if draft and draft.get('branch_id') != request.branch.pk:
        draft = None
        request.session.pop('csv_import_draft', None)
    costs = None
    if request.method == 'POST' and request.POST.get('action') == 'preview':
        request.session.pop('csv_import_draft', None)
        draft = None
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                draft = preview_delivery_batch(form.cleaned_data['file'])
                draft['branch_id'] = request.branch.pk
                config = BusinessSettings.objects.filter(pk=1).first()
                draft['hpp_rate'] = str(config.import_hpp_rate) if config and config.import_hpp_rate is not None else None
                if draft['hpp_rate'] is not None:
                    from decimal import Decimal
                    from .services import money
                    for row in draft['rows']:
                        row['estimated_cost'] = str(money(Decimal(row['subtotal']) * Decimal(draft['hpp_rate']) / 100))
                import uuid
                draft['draft_id'] = str(uuid.uuid4())
                request.session['csv_import_draft'] = draft
            except ValidationError as exc:
                draft = None
                form.add_error('file', exc)
    elif request.method == 'POST' and request.POST.get('action') == 'commit':
        if not draft or request.POST.get('draft_id') != draft.get('draft_id'):
            messages.error(request, 'Pratinjau sudah tidak tersedia. Unggah file lagi.')
        else:
            costs = ImportCostsForm(request.POST, rows=draft['rows'], rate=draft.get('hpp_rate'))
            if costs.is_valid():
                try:
                    commit_delivery_batch(draft, [costs.cleaned_data[f'cost_{i}'] for i in range(len(draft['rows']))], request.user, estimated=costs.cleaned_data.get('auto_hpp', False))
                except (ValidationError, IntegrityError, OperationalError) as exc:
                    errors(request, exc)
                else:
                    request.session.pop('csv_import_draft', None)
                    messages.success(request, 'Impor berhasil. Rekap harian dan laporan sudah diperbarui; stok tidak diubah.')
                    return redirect('sale_list')
    if draft and costs is None:
        costs = ImportCostsForm(rows=draft['rows'], rate=draft.get('hpp_rate'))
    return render(request, 'operations/sale_import.html', {'form': form, 'draft': draft, 'costs': costs})


@login_required
def pos_import(request):
    from .forms import POSUploadForm, POSMappingForm
    from .pos_import import prepare_pos_batch, map_pos_batch, commit_pos_batch

    upload = POSUploadForm()
    mapping = None
    draft = request.session.get('pos_cost_draft')
    if draft and draft.get('branch_id') != request.branch.pk:
        request.session.pop('pos_cost_draft', None)
        draft = None

    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'upload':
                request.session.pop('pos_cost_draft', None)
                draft = None
                upload = POSUploadForm(request.POST, request.FILES)
                if upload.is_valid():
                    draft = prepare_pos_batch(upload.cleaned_data['file'], upload.cleaned_data['channel'])
                    draft['branch_id'] = request.branch.pk
                    request.session['pos_cost_draft'] = draft
            elif not draft or request.POST.get('draft_id') != draft['id']:
                raise ValidationError('Pratinjau kedaluwarsa. Unggah ulang file POS.')
            elif action == 'map' and draft['stage'] == 'mapping':
                mapping = POSMappingForm(request.POST, items=draft['items'])
                if mapping.is_valid():
                    draft = map_pos_batch(draft, mapping.cleaned_data)
                    request.session['pos_cost_draft'] = draft
            elif (action, draft['stage']) in {('commit', 'preview'), ('payments', 'payment_preview')}:
                if request.POST.get('confirm') != 'on':
                    raise ValidationError('Centang konfirmasi sebelum menyimpan.')
                records = commit_pos_batch(draft, request.user)
                request.session.pop('pos_cost_draft', None)
                messages.success(request, f'{len(records)} laporan POS berhasil disimpan.')
                if len(records) == 1:
                    return redirect('sale_detail', pk=records[0].sale_id)
                return redirect('sale_list')
        except (ValidationError, IntegrityError, OperationalError) as exc:
            if action == 'upload' and isinstance(exc, ValidationError):
                upload.add_error('file', exc)
            else:
                errors(request, exc)

    if draft and draft['stage'] == 'mapping' and mapping is None:
        mapping = POSMappingForm(items=draft['items'])
    return render(request, 'operations/pos_import.html', {
        'upload': upload, 'mapping': mapping, 'draft': draft,
        'reports': draft.get('reports', [draft]) if draft else (),
    })


@login_required
def product_bulk_create(request):
    from .forms import BulkProductFormSet
    rows=BulkProductFormSet(request.POST or None,prefix='menus')
    if request.method=='POST' and rows.is_valid():
        try:
            with transaction.atomic():
                count=0
                for row in rows:
                    if row.cleaned_data and not row.cleaned_data.get('DELETE'):
                        row.save();count+=1
        except (IntegrityError,OperationalError):
            messages.error(request,'Daftar belum disimpan. Ada nama menu yang baru terdaftar atau database sedang dipakai. Periksa daftar menu lalu coba lagi.')
        else:
            messages.success(request,f'{count} menu berhasil ditambahkan. Resep dapat dilengkapi lewat Edit & resep.')
            return redirect('product_list')
    return render(request,'operations/product_bulk_form.html',{'rows':rows})


@login_required
def product_import_branch(request):
    from tenancy.models import Branch
    from tenancy.scope import current_branch
    from .models import MenuCategory,Recipe
    sources=Branch.objects.exclude(pk=request.branch.pk)
    if request.method=='POST':
        source_id=request.POST.get('source','')
        source=get_object_or_404(sources,pk=int(source_id) if source_id.isdigit() else 0)
        if request.POST.get('confirm')!='on':
            messages.error(request,'Centang konfirmasi impor.')
        else:
            copied=skipped=0
            try:
                with transaction.atomic():
                    # Explicit source predicates bypass the active-branch manager only here.
                    menus=list(Product._base_manager.filter(branch=source).select_related('category').prefetch_related('recipes__ingredient'))
                    for menu in menus:
                        if Product.objects.filter(name__iexact=menu.name).exists():
                            skipped+=1;continue
                        category=None
                        if menu.category:
                            category=MenuCategory.objects.filter(name__iexact=menu.category.name).first() or MenuCategory.objects.create(name=menu.category.name)
                        values={f:getattr(menu,f) for f in ['name','price','manual_cost','hpp_shopeefood','hpp_grabfood','hpp_gofood','active','sales_kind','batch_yield','recipe_direct_cost']}
                        new=Product.objects.create(category=category,**values)
                        def local_ingredient(old):
                            ingredient = Ingredient.objects.filter(name__iexact=old.name).first()
                            if ingredient and ingredient.unit != old.unit:
                                raise ValidationError(f'Satuan bahan {old.name} berbeda. Samakan satuan sebelum mengimpor.')
                            return ingredient or Ingredient.objects.create(
                                name=old.name, unit=old.unit, average_cost=old.average_cost, minimum_stock=old.minimum_stock,
                                active=old.active, category_name=old.category_name,
                                purchase_unit=old.purchase_unit, purchase_factor=old.purchase_factor,
                            )
                        for recipe in Recipe._base_manager.filter(product=menu, branch=source).select_related('ingredient', 'substitute'):
                            Recipe.objects.create(
                                product=new, ingredient=local_ingredient(recipe.ingredient),
                                quantity=recipe.quantity, waste=recipe.waste, role=recipe.role,
                                substitute=local_ingredient(recipe.substitute) if recipe.substitute else None,
                            )
                        from .hpp import record_recipe
                        record_recipe(new, request.user)
                        copied+=1
                messages.success(request,f'{copied} menu diimpor; {skipped} nama yang sudah ada dilewati. Stok tidak disalin.')
                return redirect('product_list')
            except (ValidationError,IntegrityError) as exc:errors(request,exc)
    return render(request,'operations/import_branch.html',{'sources':sources})


@login_required
@require_POST
def product_bulk_delete(request):
    import json
    from core.forms import DeleteConfirmationForm
    from django.db.models.deletion import ProtectedError
    try:
        raw=request.POST.get('selected_all','')
        if len(raw)>100000:raise ValueError()
        ids=json.loads(raw)
        if not isinstance(ids,list) or not ids or len(ids)>5000 or any(not isinstance(i,str) or not i.isdigit() or len(i)>18 for i in ids):raise ValueError()
        ids=set(ids)
    except (ValueError,TypeError):
        messages.error(request,'Centang minimal satu menu yang valid.');return redirect('product_list')
    selected=Product.objects.filter(pk__in=ids)
    if selected.count()!=len(ids):
        return HttpResponseForbidden('Pilihan menu tidak tersedia di cabang aktif. Muat ulang daftar menu.')
    payload=json.dumps(sorted(ids))
    from django import forms
    form=DeleteConfirmationForm(request.POST if request.POST.get('preview')!='on' else None,initial={'selected_all':payload})
    form.fields['selected_all']=forms.CharField(widget=forms.HiddenInput,initial=payload)
    if request.POST.get('preview')!='on' and form.is_valid():
        deleted=archived=0
        with transaction.atomic():
            Product.objects.filter(pk__in=ids).update(name=F('name'))
            for product in selected.select_for_update():
                try:
                    with transaction.atomic():product.delete()
                except ProtectedError:
                    product.active=False;product.save(update_fields=['active']);archived+=1
                else:deleted+=1
        request.session[f'menu_selection_clear_{request.branch.pk}']=True
        messages.success(request,f'{deleted} menu dihapus permanen; {archived} menu dengan riwayat dinonaktifkan. Riwayat transaksi dipertahankan.')
        return redirect('product_list')
    names=list(selected.values_list('name',flat=True))
    label=', '.join(names[:12])+(f' dan {len(names)-12} lainnya' if len(names)>12 else '')
    return render(request,'core/delete_confirm.html',{'form':form,'page_title':f'Hapus {len(names)} menu dipilih','object_name':label,'description':'Menu yang belum dipakai dihapus permanen beserta resepnya. Menu yang masih dirujuk penjualan atau pemetaan POS dinonaktifkan agar riwayat tetap utuh. Stok dan transaksi tidak dihapus.','back_url':reverse('product_list')})


import traceback as _erp_traceback
_erp_dashboard_original = dashboard
def dashboard(request):
    try:
        return _erp_dashboard_original(request)
    except Exception:
        print('[ERP_DASHBOARD_EXCEPTION]', flush=True)
        _erp_traceback.print_exc()
        raise
