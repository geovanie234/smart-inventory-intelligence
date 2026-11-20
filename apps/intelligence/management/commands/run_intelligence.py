"""
Run the full intelligence cycle (forecast → recommendations → alerts → anomalies).

Schedule daily, e.g. Windows Task Scheduler or cron at 05:00 Kigali time:
    python manage.py run_intelligence
"""
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Business
from apps.intelligence.services.pipeline import run_intelligence


class Command(BaseCommand):
    help = "Forecast demand and refresh reorder recommendations and alerts for each active business."

    def add_arguments(self, parser):
        parser.add_argument("--business", help="Business slug (default: all active businesses)")
        parser.add_argument("--horizon", type=int, help="Forecast horizon in days")
        parser.add_argument("--holdout", type=int, help="Holdout days used for model evaluation")

    def handle(self, *args, **opts):
        qs = Business.objects.filter(is_active=True)
        if opts["business"]:
            qs = qs.filter(slug=opts["business"])
            if not qs.exists():
                raise CommandError(f"No active business with slug {opts['business']!r}")
        for business in qs:
            self.stdout.write(f"▶ {business.name}")
            result = run_intelligence(business, horizon=opts["horizon"], holdout=opts["holdout"])
            run = result["run"]
            s = run.summary
            self.stdout.write(
                f"  run #{run.pk}: {run.products_forecast} products modelled, {run.products_insufficient} with limited history "
                f"({run.duration_seconds:.1f}s)"
            )
            if s.get("baseline_wape") is not None and s.get("selected_wape") is not None:
                self.stdout.write(
                    f"  holdout WAPE: baseline {s['baseline_wape']:.1f}% → selected {s['selected_wape']:.1f}% "
                    f"({s['improvement_pct']:+.1f}% vs baseline)"
                )
            self.stdout.write(f"  models chosen: {s.get('selected_models')}")
            self.stdout.write(self.style.SUCCESS(
                f"  {result['recommendations']} recommendations · new alerts: stock {result['new_stock_alerts']}, "
                f"expiry {result['new_expiry_alerts']}, anomalies {result['new_anomalies']}, devices {result['new_device_alerts']}"
            ))
