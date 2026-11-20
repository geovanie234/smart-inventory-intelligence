from django import forms


class BootstrapFormMixin:
    """Applies Bootstrap 5 classes to every widget."""

    def _apply_bootstrap(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput,)):
                css = "form-check-input"
            elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
                css = "form-select"
            else:
                css = "form-control"
            existing = widget.attrs.get("class", "")
            widget.attrs["class"] = f"{existing} {css}".strip()
            if isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("rows", 3)


class BusinessModelForm(BootstrapFormMixin, forms.ModelForm):
    """ModelForm that receives the current business and limits related choices to it."""

    business_scoped_fields: tuple = ()

    def __init__(self, *args, business=None, **kwargs):
        self.business = business
        super().__init__(*args, **kwargs)
        for name in self.business_scoped_fields:
            if name in self.fields and business is not None:
                qs = self.fields[name].queryset
                self.fields[name].queryset = qs.filter(business=business)
        self._apply_bootstrap()


class DateInput(forms.DateInput):
    input_type = "date"


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%Y-%m-%dT%H:%M")
        super().__init__(**kwargs)
