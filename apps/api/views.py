from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.intelligence.models import Alert, DemandPrediction, ReorderRecommendation
from apps.inventory.models import Category, InventoryTransaction, Product, Supplier
from apps.iot.services import ingest_reading
from apps.sales.models import Sale
from apps.sales.services import SaleLine, record_sale

from . import serializers as s
from .authentication import DeviceKeyAuthentication, DeviceRateThrottle, IsAuthenticatedDevice


class HasBusiness(permissions.BasePermission):
    message = "Your account is not linked to a business."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        # Token-authenticated requests are resolved after middleware ran: bind the business here.
        if getattr(request, "business", None) is None and user.business_id and user.business.is_active:
            request.business = user.business
        return getattr(request, "business", None) is not None


class CatalogWritePermission(permissions.BasePermission):
    """Read for every team member; writes for managers and purchasing officers."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return request.user.has_role("purchasing")


class BusinessScopedViewSet(viewsets.GenericViewSet):
    permission_classes = [permissions.IsAuthenticated, HasBusiness]

    def get_queryset(self):
        return self.queryset.filter(business=self.request.business)

    def perform_create(self, serializer):
        serializer.save(business=self.request.business)


class CategoryViewSet(BusinessScopedViewSet, viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = s.CategorySerializer
    permission_classes = BusinessScopedViewSet.permission_classes + [CatalogWritePermission]


class SupplierViewSet(BusinessScopedViewSet, viewsets.ModelViewSet):
    queryset = Supplier.objects.all()
    serializer_class = s.SupplierSerializer
    permission_classes = BusinessScopedViewSet.permission_classes + [CatalogWritePermission]


class ProductViewSet(BusinessScopedViewSet, viewsets.ModelViewSet):
    queryset = Product.objects.select_related("category", "business", "preferred_supplier")
    serializer_class = s.ProductSerializer
    permission_classes = BusinessScopedViewSet.permission_classes + [CatalogWritePermission]

    def get_queryset(self):
        qs = super().get_queryset()
        if risk := self.request.query_params.get("risk"):
            qs = qs.filter(stock_risk=risk)
        if code := self.request.query_params.get("barcode"):
            qs = qs.filter(barcode=code)
        return qs

    def perform_destroy(self, instance):
        instance.is_active = False  # products with history are archived, never deleted
        instance.save(update_fields=["is_active"])

    @action(detail=True)
    def forecast(self, request, pk=None):
        product = self.get_object()
        latest = DemandPrediction.objects.filter(product=product).order_by("-run_id").values_list("run_id", flat=True).first()
        preds = DemandPrediction.objects.filter(product=product, run_id=latest) if latest else []
        return Response(s.PredictionSerializer(preds, many=True).data)


class TransactionViewSet(BusinessScopedViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin):
    queryset = InventoryTransaction.objects.select_related("product")
    serializer_class = s.TransactionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if pid := self.request.query_params.get("product"):
            qs = qs.filter(product_id=pid)
        return qs


class SaleViewSet(BusinessScopedViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin):
    queryset = Sale.objects.prefetch_related("items__product")
    serializer_class = s.SaleSerializer

    def create(self, request, *args, **kwargs):
        if not request.user.has_role("clerk"):
            raise PermissionDenied("Your role cannot record sales.")
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        try:
            sale = record_sale(
                request.business,
                [SaleLine(l["product"], l["quantity"], l.get("unit_price")) for l in d["lines"]],
                user=request.user, sold_at=d.get("sold_at") or timezone.now(),
                payment_method=d.get("payment_method", Sale.PaymentMethod.CASH),
                customer_name=d.get("customer_name", ""), notes=d.get("notes", ""),
            )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})
        return Response(s.SaleSerializer(sale, context={"request": request}).data, status=status.HTTP_201_CREATED)


class RecommendationViewSet(BusinessScopedViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin):
    queryset = ReorderRecommendation.objects.select_related("product")
    serializer_class = s.RecommendationSerializer

    def get_queryset(self):
        return super().get_queryset().filter(status=self.request.query_params.get("status", "pending"))


class AlertViewSet(BusinessScopedViewSet, mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin):
    queryset = Alert.objects.select_related("product")
    serializer_class = s.AlertSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if st := self.request.query_params.get("status"):
            qs = qs.filter(status=st)
        return qs


class IoTReadingIngestView(APIView):
    """
    POST /api/v1/iot/readings/
    Headers: X-Device-Id: <id>, Authorization: Device <key>
    Body:    {"message_id": "...", "measured_at": "ISO-8601", "weight_g": 1234.5}
    Duplicate message ids return 200 with "duplicate": true so devices can retry safely.
    """

    authentication_classes = [DeviceKeyAuthentication]
    permission_classes = [IsAuthenticatedDevice]
    throttle_classes = [DeviceRateThrottle]

    def post(self, request):
        try:
            result = ingest_reading(request.auth, request.data)
        except DjangoValidationError as exc:
            return Response({"accepted": False, "errors": exc.messages}, status=status.HTTP_400_BAD_REQUEST)
        r = result.reading
        return Response(
            {
                "accepted": True, "duplicate": result.duplicate, "reading_id": r.pk, "status": r.status,
                "estimated_quantity": r.estimated_quantity, "recorded_quantity": r.recorded_quantity,
            },
            status=status.HTTP_200_OK if result.duplicate else status.HTTP_201_CREATED,
        )


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated, HasBusiness])
def dashboard_summary(request):
    from apps.dashboard.services import kpis

    return Response(kpis(request.business))
