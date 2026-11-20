from decimal import Decimal

from django import forms

from apps.common.forms import BootstrapFormMixin, BusinessModelForm, DateInput

from .models import Category, InventoryTransaction, Product, Supplier

T = InventoryTransaction.Type


class CategoryForm(BusinessModelForm):
    class Meta:
        model = Category
        fields = ["name", "description"]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Category.objects.filter(business=self.business, name__iexact=name).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A category with this name already exists.")
        return name


class SupplierForm(BusinessModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "contact_person", "phone", "email", "address", "lead_time_days", "notes", "is_active"]


class ProductForm(BusinessModelForm):
    business_scoped_fields = ("category", "preferred_supplier")
    opening_stock = forms.DecimalField(
        required=False, min_value=Decimal("0"), decimal_places=3,
        help_text="Optional opening balance, posted to the ledger when the product is created.",
    )

    class Meta:
        model = Product
        fields = [
            "name", "sku", "barcode", "category", "unit", "preferred_supplier",
            "cost_price", "selling_price",
            "lead_time_days", "safety_stock", "min_order_qty", "pack_size", "shelf_capacity",
            "track_expiry", "is_active",
        ]
        help_texts = {
            "lead_time_days": "Leave blank to use the supplier's lead time.",
            "shelf_capacity": "Optional upper limit for recommended stock levels.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            del self.fields["opening_stock"]

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip().upper()
        if Product.objects.filter(business=self.business, sku=sku).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This SKU is already used by another product.")
        return sku

    def clean_barcode(self):
        code = (self.cleaned_data.get("barcode") or "").strip()
        if code and Product.objects.filter(business=self.business, barcode=code).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This barcode is already assigned to another product.")
        return code


class StockAdjustmentForm(BusinessModelForm):
    ALLOWED_TYPES = [
        T.ADJUSTMENT_IN, T.ADJUSTMENT_OUT, T.DAMAGE, T.LOSS, T.EXPIRED,
        T.SALE_RETURN, T.SUPPLIER_RETURN, T.OPENING,
    ]
    business_scoped_fields = ("product",)
    quantity = forms.DecimalField(min_value=Decimal("0.001"), decimal_places=3)

    class Meta:
        model = InventoryTransaction
        fields = ["product", "txn_type", "quantity", "reference", "notes"]
        labels = {"txn_type": "Movement type"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["txn_type"].choices = [(t.value, t.label) for t in self.ALLOWED_TYPES]
        self.fields["product"].queryset = self.fields["product"].queryset.filter(is_active=True)
        self.fields["notes"].required = True
        self.fields["notes"].help_text = "Required: explain why the stock changed (audit trail)."


class StockCountEntryForm(BootstrapFormMixin, forms.Form):
    notes = forms.CharField(required=False, max_length=255)


class ProductFilterForm(BootstrapFormMixin, forms.Form):
    q = forms.CharField(required=False, label="Search", widget=forms.TextInput(attrs={"placeholder": "Name, SKU or barcode"}))
    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, empty_label="All categories")
    risk = forms.ChoiceField(choices=[("", "Any status")] + list(Product.Risk.choices), required=False)

    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        if business is not None:
            self.fields["category"].queryset = Category.objects.filter(business=business)
        self._apply_bootstrap()


class LedgerFilterForm(BootstrapFormMixin, forms.Form):
    product = forms.ModelChoiceField(queryset=Product.objects.none(), required=False, empty_label="All products")
    txn_type = forms.ChoiceField(choices=[("", "All types")] + list(T.choices), required=False, label="Type")
    date_from = forms.DateField(required=False, widget=DateInput())
    date_to = forms.DateField(required=False, widget=DateInput())

    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        if business is not None:
            self.fields["product"].queryset = Product.objects.filter(business=business)
        self._apply_bootstrap()
