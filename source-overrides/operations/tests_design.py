from decimal import Decimal
from datetime import date
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from .templatetags.erp import rupiah
from .reporting import summary
from .models import Sale,Channel

class DashboardDesignTests(TestCase):
    def test_currency_keeps_settlement_cents(self):
        self.assertEqual(rupiah(Decimal('459645.60')),'Rp459.645,60')
        self.assertEqual(rupiah(1000000),'Rp1.000.000')
        self.assertEqual(rupiah(Decimal('-1500.25')),'-Rp1.500,25')
        self.assertEqual(rupiah(Decimal('0.005')),'Rp0,01')
        self.assertEqual(rupiah(Decimal('NaN')),'Rp0')
    def test_chart_uses_real_values_and_money_form_labels(self):
        u=get_user_model().objects.create_user('designowner',password='testing');self.client.force_login(u)
        Sale.objects.create(date=date(2026,10,1),channel=Channel.objects.get(name='Offline'),channel_name='Offline',created_by=u,net_revenue=Decimal('459645.60'),cost=500000,subtotal=Decimal('459645.60'))
        stats=summary(date(2026,10,1),date(2026,10,31))
        self.assertEqual(stats['daily'][0]['gross_profit'],Decimal('-40354.40'))
        self.assertEqual(stats['channels'][0]['share'],100)
        self.assertTrue(stats['chart']['revenue']);self.assertTrue(stats['chart']['profit'])
        response=self.client.get(reverse('dashboard')+'?month=2026-10')
        self.assertContains(response,'Rp459.645,60');self.assertContains(response,'dash-grid');self.assertContains(response,'Lihat angka harian')
        for name in ['product_create','payroll_create','role_create']:
            self.assertContains(self.client.get(reverse(name)),'money-input')
