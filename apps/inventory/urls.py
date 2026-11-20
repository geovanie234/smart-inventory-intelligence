from django.urls import path

from . import views

app_name = "inventory"

urlpatterns = [
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/lookup/", views.ProductLookupView.as_view(), name="product_lookup"),
    path("products/<int:pk>/", views.ProductDetailView.as_view(), name="product_detail"),
    path("products/<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_update"),
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.CategoryCreateView.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.CategoryUpdateView.as_view(), name="category_update"),
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/new/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/", views.SupplierDetailView.as_view(), name="supplier_detail"),
    path("suppliers/<int:pk>/edit/", views.SupplierUpdateView.as_view(), name="supplier_update"),
    path("ledger/", views.LedgerView.as_view(), name="ledger"),
    path("adjust/", views.StockAdjustmentView.as_view(), name="adjust"),
    path("count/", views.StockCountView.as_view(), name="stock_count"),
    path("batches/", views.BatchListView.as_view(), name="batch_list"),
]
