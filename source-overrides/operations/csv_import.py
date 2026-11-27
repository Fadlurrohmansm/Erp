"""Strict parsing of observed merchant exports; no inferred platform rates."""
import csv
import io
import hashlib
import uuid
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from .models import Channel, CSVImport, Sale
from .services import post_daily_recap


def amount(value):
    try:
        n = Decimal(str(value).strip() or '0')
        if not n.is_finite() or n < 0 or n > Decimal('999999999999') or n != n.quantize(Decimal('.01')):
            raise ValueError
        return n
    except (InvalidOperation, ValueError):
        raise ValidationError('Nominal tidak valid. Gunakan angka tanpa pemisah ribuan dan titik untuk desimal.')


def parse_csv(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValidationError('Ukuran CSV maksimal 2 MB.')
    try:
        text = raw.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text), delimiter=';' if text.splitlines()[0].count(';') > text.splitlines()[0].count(',') else ',')
        headers = set(reader.fieldnames or [])
    except (UnicodeDecodeError, IndexError, csv.Error):
        raise ValidationError('CSV kosong atau bukan UTF-8. Ekspor ulang sebagai CSV UTF-8.')
    gofood_transactions = False
    if {'Order ID', 'Order Create Time', 'Order Amount', 'Net Income', 'Commission', 'Order Status'} <= headers:
        channel = 'ShopeeFood'
        required = {'Store ID', 'Transaction Type', 'PB1', 'Merchant Service Charge', 'Merchant Surcharge Fee', 'Merchant Shipping Fee Voucher Subsidy', 'Platform Flash Sale Subsidy', 'Food Direct Discount', 'Merchant Food Voucher Subsidy', 'Merchant Voucher Deals Subsidy'}
    elif {'Nomor Pesanan', 'Waktu Transaksi', 'Penjualan', 'Total Biaya', 'Pendapatan Bersih', 'Refund', 'Jumlah Refund', 'Gopay Source'} <= headers:
        channel = 'GoFood'
        required = set()
    elif {'Serial No', 'Waktu Transaksi', 'Nomor Pesanan', 'Tipe Pesanan', 'Mata Uang', 'Penjualan', 'Total Biaya', 'Pendapatan Bersih'} <= headers:
        channel = 'GoFood'
        gofood_transactions = True
        required = {'Biaya Program', 'Nama Program', 'Biaya GoFood', 'Biaya Program Promo',
                    'Pengurangan biaya layanan GoFood', 'Promo yang ditanggung Mitra Usaha', 'Biaya pemberian voucher'}
    elif {'tanggal', 'kanal', 'id_pesanan', 'omzet', 'voucher', 'komisi', 'biaya_lain', 'pendapatan_bersih', 'status'} <= headers:
        channel = None
        required = set()
    else:
        raise ValidationError('Format belum didukung. Gunakan ekspor ShopeeFood/GoFood yang sesuai contoh atau template CSV standar untuk GrabFood.')
    if not required <= headers:
        raise ValidationError('Kolom ekspor tidak lengkap. Gunakan file asli tanpa menghapus kolom.')
    groups, seen, stores = {}, set(), set()
    skipped = 0
    try:
        for line, row in enumerate(reader, 2):
            if line > 10002:
                raise ValidationError('Maksimal 10.000 baris per file.')
            if None in row or any(v is None for v in row.values()):
                raise ValidationError(f'Baris {line}: jumlah kolom tidak sesuai header.')
            if not any(v.strip() for v in row.values()):
                continue
            try:
                if channel == 'ShopeeFood':
                    status = row['Order Status'].strip().lower()
                    if status in ['cancelled', 'canceled']:
                        skipped += 1; continue
                    transaction_type = row['Transaction Type'].strip().casefold()
                    if status != 'settled' or transaction_type not in {'delivery order', 'pick-up order'}:
                        raise ValidationError('Status/jenis transaksi belum didukung; gunakan laporan pesanan settled tanpa refund.')
                    stores.add(row['Store ID'])
                    time_column = 'Order Complete/Cancel Time'
                    if time_column not in headers:
                        time_column = 'Order Create Time'
                    try:
                        day = datetime.strptime(row[time_column].strip(), '%d/%m/%Y %H:%M:%S').date()
                    except ValueError as exc:
                        raise ValidationError(f'Waktu ShopeeFood di kolom {time_column} tidak valid.') from exc
                    ident = row['Order ID'].strip()
                    gross = amount(row['Order Amount'])
                    voucher = sum((amount(row[k]) for k in ['Merchant Shipping Fee Voucher Subsidy', 'Food Direct Discount', 'Merchant Food Voucher Subsidy', 'Merchant Voucher Deals Subsidy']), Decimal(0))
                    commission = amount(row['Commission'])
                    other = sum((amount(row[k]) for k in ['Merchant Service Charge', 'Merchant Surcharge Fee']), Decimal(0))
                    if amount(row['PB1']) or amount(row['Platform Flash Sale Subsidy']):
                        raise ValidationError('PB1/subsidi flash sale perlu ditinjau; gunakan rekap manual untuk file ini.')
                    net = amount(row['Net Income']); ch = channel
                elif channel == 'GoFood':
                    if gofood_transactions:
                        order_type = row['Tipe Pesanan'].strip()
                        if row['Mata Uang'].strip() != 'IDR' or order_type not in {'GoFood Pesan Antar', 'GoFood Hemat', 'GoFood Pickup'}:
                            raise ValidationError('Jenis atau mata uang transaksi tidak didukung. Gunakan GoFood Pesan Antar, GoFood Hemat, atau GoFood Pickup dalam IDR.')
                        timestamp = datetime.fromisoformat(row['Waktu Transaksi'].strip())
                        if timestamp.tzinfo is None:
                            raise ValidationError('Waktu transaksi GoFood harus menyertakan zona waktu.')
                        day = timestamp.astimezone(timezone.get_default_timezone()).date()
                        components = sum((amount(row[k]) for k in ['Biaya Program', 'Biaya GoFood', 'Biaya Program Promo', 'Promo yang ditanggung Mitra Usaha', 'Biaya pemberian voucher']), Decimal(0))
                        reduction = amount(row['Pengurangan biaya layanan GoFood'])
                        if components - reduction != amount(row['Total Biaya']):
                            raise ValidationError('Rincian biaya GoFood tidak cocok dengan Total Biaya.')
                    else:
                        if row['Refund'].strip().lower() not in ['false', '0'] or amount(row['Jumlah Refund']):
                            raise ValidationError('Refund perlu direkap manual agar tidak salah menghitung pendapatan.')
                        if row['Gopay Source'].strip() != 'goresto_online':
                            raise ValidationError('Sumber transaksi bukan GoFood online.')
                        day = datetime.strptime(row['Waktu Transaksi'], '%b %d %Y, %H:%M').date()
                    ident = row['Nomor Pesanan'].strip()
                    gross = amount(row['Penjualan']); net = amount(row['Pendapatan Bersih'])
                    voucher = commission = Decimal(0)
                    other = amount(row['Total Biaya']); ch = channel
                    # Total Biaya already includes deductions: do not add promo columns again.
                else:
                    ch = row['kanal'].strip()
                    if ch not in ['ShopeeFood', 'GoFood', 'GrabFood']:
                        raise ValidationError('Kanal template harus ShopeeFood, GoFood, atau GrabFood.')
                    if row['status'].strip().lower() == 'batal':
                        skipped += 1; continue
                    if row['status'].strip().lower() != 'selesai':
                        raise ValidationError('Status template harus selesai atau batal.')
                    day = date.fromisoformat(row['tanggal']); ident = row['id_pesanan'].strip()
                    gross, voucher, commission, other, net = [amount(row[k]) for k in ['omzet', 'voucher', 'komisi', 'biaya_lain', 'pendapatan_bersih']]
                if day > timezone.localdate() or day.year < 2000:
                    raise ValidationError('Tanggal transaksi tidak valid atau di masa depan.')
                if not ident or (ch, ident) in seen:
                    raise ValidationError('ID pesanan kosong atau duplikat di dalam file.')
                seen.add((ch, ident))
                if gross - voucher - commission - other != net:
                    raise ValidationError('Omzet dikurangi seluruh potongan tidak cocok dengan pendapatan bersih; tidak ada angka yang dipaksakan.')
                key = (day.isoformat(), ch)
                g = groups.setdefault(key, dict(date=key[0], channel=ch, order_count=0, subtotal=Decimal(0), voucher_amount=Decimal(0), commission_amount=Decimal(0), other_fees=Decimal(0), net_revenue=Decimal(0)))
                for field, value in [('subtotal', gross), ('voucher_amount', voucher), ('commission_amount', commission), ('other_fees', other), ('net_revenue', net)]:
                    g[field] += value
                g['order_count'] += 1
            except (ValueError, ValidationError) as exc:
                msg = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'Tanggal atau data tidak valid.'
                raise ValidationError(f'Baris {line}: {msg}')
    except csv.Error:
        raise ValidationError('Struktur CSV rusak.')
    if len(stores) > 1:
        raise ValidationError('File berisi beberapa outlet. Pisahkan per outlet terlebih dahulu.')
    if len(groups) > 366:
        raise ValidationError('Maksimal 366 rekap harian per file; pisahkan laporan menjadi beberapa file.')
    if not groups:
        raise ValidationError('Tidak ada transaksi selesai yang bisa diimpor.')
    return dict(fingerprint=hashlib.sha256(raw).hexdigest(), channel=channel or 'Template standar', skipped=skipped,
                rows=[{k: str(v) if isinstance(v, Decimal) else v for k, v in g.items()} for _, g in sorted(groups.items())])


@transaction.atomic
def commit_import(draft, costs, user, estimated=False):
    if CSVImport.objects.filter(fingerprint=draft['fingerprint']).exists():
        raise ValidationError('File ini sudah pernah diimpor. Periksa riwayat penjualan; jangan mengimpor ulang.')
    batch = CSVImport.objects.create(fingerprint=draft['fingerprint'], filename=draft['filename'], channel=draft['channel'], created_by=user)
    for row, cost in zip(draft['rows'], costs, strict=True):
        channel, _ = Channel.objects.get_or_create(name=row['channel'])
        data = {k: Decimal(row[k]) for k in ['subtotal', 'voucher_amount', 'commission_amount', 'other_fees']}
        data.update(token=uuid.uuid4(), date=date.fromisoformat(row['date']), channel=channel, order_count=row['order_count'], cost=amount(cost), notes='Impor CSV: ' + draft['filename'][:220])
        sale = post_daily_recap(data=data, user=user)
        if estimated:
            sale.cost_estimated = True
            sale.notes = sale.notes[:190] + f" · Estimasi HPP {draft.get('hpp_rate')}% bruto"
            sale.save(update_fields=['cost_estimated', 'notes'])
        batch.sales.add(sale)
    return batch


def parse_delivery_file(raw):
    return parse_grab_pdf(raw) if raw.startswith(b'%PDF-') else parse_csv(raw)


def preview_delivery_batch(uploads):
    files, rows, fingerprints, periods = [], [], {}, {}
    skipped = 0
    for upload in uploads:
        filename = upload.name[:255]
        try:
            draft = parse_delivery_file(upload.read())
        except ValidationError as exc:
            raise ValidationError([f'{filename}: {message}' for message in exc.messages]) from exc
        fingerprint = draft['fingerprint']
        if fingerprint in fingerprints:
            raise ValidationError(f'{filename} dan {fingerprints[fingerprint]} memiliki isi yang sama.')
        fingerprints[fingerprint] = filename
        for row in draft['rows']:
            key = (row['date'], row['channel'])
            if key in periods:
                raise ValidationError(f'{filename} dan {periods[key]} sama-sama memuat {key[1]} tanggal {key[0]}. Pilih satu laporan lengkap untuk tanggal dan kanal itu.')
            periods[key] = filename
            rows.append({**row, 'filename': filename, 'outlet': draft.get('outlet')})
        if len(rows) > 366:
            raise ValidationError('Maksimal 366 rekap harian per unggahan. Pisahkan kelompok file.')
        files.append({
            'filename': filename, 'fingerprint': fingerprint, 'channel': draft['channel'],
            'outlet': draft.get('outlet'), 'row_count': len(draft['rows']),
        })
        skipped += draft['skipped']

    imported = CSVImport.objects.filter(fingerprint__in=fingerprints).values_list('fingerprint', flat=True).first()
    if imported:
        raise ValidationError(f'{fingerprints[imported]}: file ini sudah pernah diimpor.')
    sales = Sale.objects.filter(
        status='posted', date__in={day for day, _ in periods}, channel__name__in={channel for _, channel in periods},
    ).values_list('date', 'channel__name')
    for day, channel in sales:
        key = (day.isoformat(), channel)
        if key in periods:
            raise ValidationError(f'{periods[key]}: {channel} tanggal {key[0]} sudah tercatat di cabang aktif.')
    return {'files': files, 'rows': rows, 'skipped': skipped}


@transaction.atomic
def commit_delivery_batch(draft, costs, user, estimated=False):
    if len(draft['rows']) != len(costs) or sum(file['row_count'] for file in draft['files']) != len(costs):
        raise ValidationError('Rincian impor sudah tidak sesuai. Unggah ulang file.')
    start = 0
    for file in draft['files']:
        end = start + file['row_count']
        commit_import(
            {**file, 'rows': draft['rows'][start:end], 'hpp_rate': draft.get('hpp_rate')},
            costs[start:end], user, estimated=estimated,
        )
        start = end


def parse_grab_text(text, raw):
    import re
    months = ['Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember']
    number = r'-?\s*\d[\d.]*,\d{2}'
    def values(block):
        return [Decimal(re.sub(r'\s+', '', n).replace('.', '').replace(',', '.')) for n in re.findall(number, block)]
    if 'Laporan Bisnis Harian' not in text or 'Pesanan Aplikasi & Web' not in text:
        raise ValidationError('PDF bukan format Laporan Bisnis Harian Grab yang didukung.')
    match = re.search(r'(\d{1,2}) ('+'|'.join(months)+r') (\d{4}),', text)
    if not match:
        raise ValidationError('Tanggal laporan Grab tidak terbaca.')
    try:
        day = date(int(match[3]), months.index(match[2])+1, int(match[1]))
    except ValueError:
        raise ValidationError('Tanggal laporan Grab tidak valid.')
    if day > timezone.localdate():
        raise ValidationError('Tanggal laporan masih di masa depan.')
    try:
        header, body = text.split('Pesanan Aplikasi & Web', 1)
        ring = header.split('Ringkasan', 1)[1]
        totals = values(ring.split('Total pembayaran', 1)[0])
        counts = re.findall(r'(\d+)\s+pesanan', ring.split('Total pembayaran',1)[0])
        table = header.split('Total pembayaran', 1)[1]
        nums = values(table)
        if len(totals) != 2 or len(counts) != 1 or len(nums) != 8:
            raise ValueError
        gross, pb1, service, promo, commission, shipping, net, debt = nums
    except (IndexError, ValueError):
        raise ValidationError('Ringkasan PDF berubah atau tidak lengkap; tidak ada angka yang ditebak.')
    if pb1 or service or debt or totals[1] or promo > 0 or commission > 0 or shipping > 0:
        raise ValidationError('PDF berisi pajak, layanan restoran, tagihan atau penyesuaian yang perlu ditinjau manual.')
    voucher, fee, other = -promo, -commission, -shipping
    if gross < 0 or net < 0 or gross-voucher-fee-other != net or totals[0] != net:
        raise ValidationError('Total pendapatan PDF tidak cocok dengan rincian potongan.')
    # Verify all delivered orders, including continuation pages, against summary.
    body = body.split('Cara Membaca Laporan Ini',1)[0]
    blocks = re.split(r'\b(GF-\d+)\b', body)
    if len(blocks) < 3:
        raise ValidationError('Rincian pesanan Grab tidak terbaca. Gunakan PDF asli, bukan hasil scan.')
    # Grab reuses short GF codes. Count validated rows and reconcile with the report.
    delivered = 0
    cancelled = 0
    sums = [Decimal(0)] * 5
    for i in range(1, len(blocks), 2):
        # The next time can follow the amount but cannot match decimal-currency regex.
        entries = values(blocks[i+1])
        # A cancelled order has an explicit status immediately before its ID
        # and seven dash-only amount cells. Never ignore incomplete paid rows.
        preceding = blocks[i-1]
        cancelled_label = re.search(r'Pesanan\s+dibatalkan\s*$', preceding, re.I)
        cancelled_cells = re.split(r'\d{1,2}:\d{2}\s*[AP]M|Harga Total|Pesanan\s+dibatalkan', blocks[i+1], maxsplit=1)[0]
        if cancelled_label and re.fullmatch(r'\s*(?:-\s*){7}', cancelled_cells):
            cancelled += 1
            continue
        delivered += 1
        # Last block includes the final total footer, so use first six/seven row values.
        # Restaurant service is a dash in the supported layout.
        if len(entries) < 6:
            raise ValidationError('Rincian pesanan PDF terpotong.')
        a, tax, discount, charge, ship, income = entries[:6]
        if tax or a < 0 or discount > 0 or ship < 0:
            raise ValidationError('Rincian pajak/biaya pesanan belum didukung.')
        charge = abs(charge)
        if a + discount - charge - ship != income:
            raise ValidationError('Rincian pesanan tidak cocok; format PDF perlu diperiksa.')
        for j, value in enumerate([a, -discount, charge, ship, income]):
            sums[j] += value
    if delivered + cancelled != int(counts[0]) or sums != [gross, voucher, fee, other, net]:
        raise ValidationError('Jumlah atau total rincian pesanan tidak cocok dengan ringkasan PDF.')
    outlet = header.split('Ringkasan',1)[0].strip().splitlines()[-1].strip()
    return dict(fingerprint=hashlib.sha256(raw).hexdigest(), channel='GrabFood', outlet=outlet, skipped=cancelled,
                rows=[dict(date=day.isoformat(), channel='GrabFood', order_count=delivered, subtotal=str(gross), voucher_amount=str(voucher), commission_amount=str(fee), other_fees=str(other), net_revenue=str(net))])


def parse_grab_pdf(raw):
    if len(raw) > 2 * 1024 * 1024:
        raise ValidationError('PDF maksimal 2 MB.')
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 30:
            raise ValidationError('PDF terenkripsi atau melebihi 30 halaman tidak didukung.')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    except ValidationError:
        raise
    except Exception:
        raise ValidationError('PDF tidak dapat dibaca. Gunakan PDF asli dari Grab, bukan foto/scan.')
    return parse_grab_text(text, raw)
