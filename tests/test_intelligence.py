from datetime import timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.intelligence.ml.anomaly import robust_zscore
from apps.intelligence.ml.evaluation import holdout_evaluate, mae, wape
from apps.intelligence.ml.forecasters import MovingAverage, SeasonalNaive, candidate_models
from apps.intelligence.models import Alert, ForecastRun, ReorderRecommendation
from apps.intelligence.services.anomalies import screen_transaction
from apps.intelligence.services.data import build_demand_frames
from apps.intelligence.services.pipeline import run_intelligence
from apps.intelligence.services.recommendations import calculate_reorder, unsellable_before_expiry
from apps.inventory.models import InventoryTransaction, Product
from apps.inventory.services import post_transaction

from . import factories as f


class ReorderCalculationTests(SimpleTestCase):
    def test_matches_proposal_worked_example(self):
        """Proposal §10: 30 on hand, 8/day, 3-day lead time, 7-day review, safety stock 10 → order 60."""
        calc = calculate_reorder(current_stock=30, avg_daily_demand=8, lead_time_days=3, review_period_days=7, safety_stock=10)
        self.assertEqual(calc.expected_demand, 80)
        self.assertEqual(calc.recommended_quantity, 60)
        self.assertEqual(len(calc.steps), 3)

    def test_on_order_pack_size_and_moq(self):
        calc = calculate_reorder(current_stock=30, avg_daily_demand=8, lead_time_days=3, review_period_days=7,
                                 safety_stock=10, on_order=25, pack_size=12)
        self.assertEqual(calc.raw_quantity, 35)
        self.assertEqual(calc.recommended_quantity, 36)  # 3 packs of 12
        calc = calculate_reorder(current_stock=75, avg_daily_demand=8, lead_time_days=3, review_period_days=7,
                                 safety_stock=10, min_order_qty=50)
        self.assertEqual(calc.recommended_quantity, 50)

    def test_shelf_capacity_caps_order(self):
        calc = calculate_reorder(current_stock=30, avg_daily_demand=8, lead_time_days=3, review_period_days=7,
                                 safety_stock=10, shelf_capacity=70)
        self.assertEqual(calc.recommended_quantity, 40)

    def test_computed_safety_stock(self):
        calc = calculate_reorder(current_stock=0, avg_daily_demand=10, lead_time_days=2, review_period_days=2,
                                 demand_std=3, service_z=2.0)
        self.assertAlmostEqual(calc.safety_stock, 12.0)  # 2 × 3 × √4
        self.assertEqual(calc.safety_stock_method, "computed")

    def test_expiring_stock_is_not_counted_as_usable(self):
        # 10/day demand: batch A (30 units, expires in 1 day → 2 selling days) wastes 10;
        # batch B (50 units, 9 days) sells fully after A.
        self.assertAlmostEqual(unsellable_before_expiry([(30, 1), (50, 9)], 10), 10)
        self.assertAlmostEqual(unsellable_before_expiry([(40, -1)], 10), 40)  # already expired
        calc = calculate_reorder(current_stock=80, avg_daily_demand=10, lead_time_days=2, review_period_days=5,
                                 safety_stock=0, unsellable=10)
        self.assertEqual(calc.recommended_quantity, 0)  # 70 usable ≥ 70 needed
        calc = calculate_reorder(current_stock=80, avg_daily_demand=10, lead_time_days=2, review_period_days=5,
                                 safety_stock=10, unsellable=10)
        self.assertEqual(calc.recommended_quantity, 10)
        self.assertEqual(calc.current_stock, 80)

    def test_no_order_when_covered(self):
        calc = calculate_reorder(current_stock=500, avg_daily_demand=8, lead_time_days=3, review_period_days=7, safety_stock=10)
        self.assertEqual(calc.recommended_quantity, 0)


class MetricsAndModelsTests(SimpleTestCase):
    def test_metrics(self):
        self.assertAlmostEqual(mae([10, 0, 5], [8, 1, 5]), 1.0)
        self.assertAlmostEqual(wape([10, 0, 10], [8, 1, 10]), 15.0)
        self.assertIsNone(wape([0, 0], [1, 2]))

    def _weekly_series(self, days=140):
        idx = pd.date_range("2026-01-01", periods=days, freq="D")
        pattern = np.array([10, 10, 10, 10, 20, 30, 5], float)
        y = pd.Series(pattern[idx.dayofweek], index=idx)
        return y, pd.Series(True, index=idx)

    def test_seasonal_model_beats_flat_baseline_on_weekly_pattern(self):
        y, av = self._weekly_series()
        base = holdout_evaluate(MovingAverage(28), y, av, 28)
        seasonal = holdout_evaluate(SeasonalNaive(), y, av, 28)
        self.assertLess(seasonal.mae, 0.01)
        self.assertGreater(base.mae, 5)

    def test_holdout_does_not_leak_future(self):
        y, av = self._weekly_series()
        y.iloc[-28:] = 1000  # a regime change only in the holdout period
        res = holdout_evaluate(MovingAverage(28), y, av, 28)
        self.assertLess(res.forecast.max(), 40)  # model never saw the 1000s

    def test_stockout_days_are_excluded_from_fit(self):
        idx = pd.date_range("2026-01-01", periods=60, freq="D")
        y = pd.Series(10.0, index=idx)
        av = pd.Series(True, index=idx)
        y.iloc[-10:] = 0  # out of stock: zero sales do not mean zero demand
        av.iloc[-10:] = False
        m = MovingAverage(28).fit(y, av)
        self.assertAlmostEqual(m.level_, 10.0)

    def test_all_candidates_fit_and_predict(self):
        y, av = self._weekly_series()
        future = pd.date_range(y.index[-1] + timedelta(days=1), periods=14, freq="D")
        for model in candidate_models():
            pred = model.fit(y, av).predict(future)
            self.assertEqual(len(pred), 14, model.name)
            self.assertTrue(np.all(pred >= 0), model.name)

    def test_robust_zscore(self):
        self.assertGreater(robust_zscore(50, [1, 2, 1, 2, 1, 3, 2]), 3.5)
        self.assertLess(abs(robust_zscore(2, [1, 2, 1, 2, 1, 3, 2])), 1)


class PipelineTests(TestCase):
    def setUp(self):
        self.biz = f.business()
        self.sup = f.supplier(self.biz, lead=3)
        self.p = f.product(self.biz, stock=2000, preferred_supplier=self.sup)
        rng = np.random.default_rng(1)
        f.daily_sales(self.biz, self.p, list(rng.poisson(12, 90)))

    def test_demand_frames_mark_stockouts(self):
        thin = f.product(self.biz, stock=5, preferred_supplier=self.sup)
        f.daily_sales(self.biz, thin, [5])  # sells out yesterday
        end = timezone.localdate() - timedelta(days=1)
        sales, available = build_demand_frames(self.biz, end - timedelta(days=5), end)
        self.assertFalse(available[thin.pk].iloc[-1])
        self.assertTrue(available[self.p.pk].iloc[-1])

    def test_full_cycle_produces_forecasts_recommendations_and_alerts(self):
        Product.objects.filter(pk=self.p.pk).update(stock_on_hand=0)
        post_transaction(self.p, InventoryTransaction.Type.ADJUSTMENT_IN, 20, allow_negative=True)
        result = run_intelligence(self.biz)
        run = result["run"]
        self.assertEqual(run.status, ForecastRun.Status.SUCCESS)
        self.assertEqual(run.products_forecast, 1)
        self.p.refresh_from_db()
        self.assertGreater(self.p.forecast_daily_demand, Decimal("8"))
        self.assertLess(self.p.forecast_daily_demand, Decimal("16"))
        self.assertIn(self.p.stock_risk, {Product.Risk.CRITICAL, Product.Risk.LOW})
        rec = ReorderRecommendation.objects.get(product=self.p, status=ReorderRecommendation.Status.PENDING)
        self.assertGreater(rec.recommended_quantity, 0)
        self.assertTrue(rec.explanation)
        self.assertTrue(Alert.objects.filter(product=self.p, alert_type=Alert.Type.STOCKOUT_RISK).exists())
        self.assertIsNotNone(run.summary["baseline_wape"])

    def test_large_loss_is_flagged(self):
        self.p.forecast_daily_demand = Decimal("12")
        self.p.save()
        txn = post_transaction(self.p, InventoryTransaction.Type.LOSS, 100, notes="shortage")
        alert = screen_transaction(txn)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.alert_type, Alert.Type.ANOMALY)
        small = post_transaction(self.p, InventoryTransaction.Type.DAMAGE, 1, notes="broken")
        self.assertIsNone(screen_transaction(small))

    def test_risk_alert_raised_and_resolved_live(self):
        self.p.forecast_daily_demand = Decimal("10")
        self.p.save()
        post_transaction(self.p, InventoryTransaction.Type.ADJUSTMENT_OUT, self.p.stock_on_hand - 5, notes="move")
        self.assertTrue(Alert.objects.filter(product=self.p, status=Alert.Status.OPEN).exists())
        post_transaction(self.p, InventoryTransaction.Type.PURCHASE, 300)
        self.assertFalse(Alert.objects.filter(product=self.p, status=Alert.Status.OPEN,
                                              alert_type=Alert.Type.STOCKOUT_RISK).exists())
