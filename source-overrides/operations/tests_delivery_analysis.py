from datetime import date
from decimal import Decimal as D
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from .models import Sale,Channel
from .reporting import delivery_daily
class DeliveryDailyTests(TestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user('dailyanalyst',password='testing');self.client.force_login(self.user)
    def add(self,name,gross,orders=1,day=date(2026,10,28),status='posted'):
        channel,_=Channel.objects.get_or_create(name=name)
        Sale.objects.create(date=day,channel=channel,channel_name=name,subtotal=gross,net_revenue=D(gross)*D('.75'),order_count=orders,status=status,created_by=self.user)
    def test_contributions_and_online_not_double_counted(self):
        for name,gross in [('ShopeeFood',400000),('GrabFood',300000),('GoFood',200000),('Offline',100000)]:self.add(name,gross,orders=5)
        d=delivery_daily('2026-10-28')
        self.assertEqual(d['gross'],1000000);self.assertEqual(d['online'],900000);self.assertEqual(d['online_share'],90)
        self.assertEqual([r['share'] for r in d['channels']],[40,30,20,10]);self.assertEqual(d['orders'],20)
        self.assertEqual(d['leader']['name'],'ShopeeFood');self.assertEqual(d['net'],750000)
        self.assertContains(self.client.get(reverse('dashboard')+'?day=2026-10-28'),'Penjualan per kanal')
    def test_date_status_and_unknown_channels(self):
        self.add('ShopeeFood',100000)
        self.add('GrabFood',999999,status='void')
        self.add('GoFood',888888,day=date(2026,10,27))
        self.add('Event',100000)
        d=delivery_daily('2026-10-28');self.assertEqual(d['gross'],200000);self.assertEqual(d['channels'][-1]['name'],'Kanal lainnya');self.assertEqual(d['online_share'],50)
    def test_empty_day_safe_and_invalid_falls_back(self):
        d=delivery_daily('2026-10-20');self.assertEqual(d['gross'],0);self.assertEqual(d['online_share'],0)
        self.assertEqual(len(d['channels']),4);self.assertIsNone(d['leader'])
        self.assertContains(self.client.get(reverse('dashboard')+'?day=2026-10-20'),'Belum ada omzet yang tercatat pada tanggal ini')
        self.assertIsNotNone(delivery_daily('bad')['date'])

    def test_top_five_days_aggregate_and_rank_gross(self):
        for day in range(1,8):
            self.add('Offline',day*10000,orders=day,day=date(2026,10,day))
        self.add('GrabFood',50000,orders=2,day=date(2026,10,1))
        self.add('GrabFood',999999,status='void',day=date(2026,10,2))
        self.add('Offline',999999,day=date(2026,9,30))
        response=self.client.get(reverse('dashboard'),{'month':'2026-10'})
        rows=response.context['stats']['top_days']
        self.assertEqual([r['date'].day for r in rows],[7,6,1,5,4])
        self.assertEqual(rows[2]['gross'],60000)
        self.assertEqual(rows[2]['orders'],3)
        self.assertEqual(rows[0]['bar_width'],100)
        self.assertEqual(len(self.client.get(reverse('dashboard'),{'month':'2026-08'}).context['stats']['top_days']),0)
