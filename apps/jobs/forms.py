from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from apps.jobs import uploads
from apps.jobs.extract import ExtractionError, cv_hash, resolve_cv_text
from apps.jobs.models import Job

ACCEPTED = (".pdf", ".docx", ".txt", ".md")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class JobForm(forms.ModelForm):
    # Lo completa el JS cuando la subida directa a S3 termina bien.
    cv_s3_key = forms.CharField(required=False, widget=forms.HiddenInput())

    cv_file = forms.FileField(
        required=False,
        label="…o subí el archivo",
        help_text="PDF, DOCX o TXT, hasta 10 MB. Si el PDF está escaneado se usa OCR.",
        widget=forms.ClearableFileInput(attrs={"accept": ",".join(ACCEPTED)}),
    )

    class Meta:
        model = Job
        fields = ["target_role", "job_description", "language", "include_summary",
                  "cv_source_text"]
        labels = {
            "target_role": "Puesto al que aplicás",
            "job_description": "Descripción del puesto",
            "language": "Idioma del CV",
            "include_summary": "Incluir un resumen profesional arriba",
            "cv_source_text": "Tu CV actual",
        }
        help_texts = {
            "job_description": "Copiá y pegá el aviso completo de LinkedIn. El relleno "
                               "corporativo se recorta solo.",
            "include_summary": "El formato Harvard canónico no lleva resumen. Activalo "
                               "solo si lo querés igual.",
            "cv_source_text": "Pegalo tal cual. Si preferís, subí el archivo abajo.",
        }
        widgets = {
            "target_role": forms.TextInput(
                attrs={"placeholder": "ej. Senior Backend Engineer", "autofocus": True}
            ),
            "job_description": forms.Textarea(
                attrs={"rows": 10, "placeholder": "Pegá acá el aviso de LinkedIn…"}
            ),
            "cv_source_text": forms.Textarea(
                attrs={"rows": 10, "placeholder": "Pegá acá tu CV actual…"}
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_cv_file(self):
        uploaded = self.cleaned_data.get("cv_file")
        if uploaded and uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError("El archivo pesa más de 10 MB.")
        return uploaded

    def clean(self):
        cleaned = super().clean()
        uploaded = cleaned.get("cv_file")
        s3_key = (cleaned.get("cv_s3_key") or "").strip()

        filename, data = None, None
        if uploaded:
            filename, data = uploaded.name, uploaded.read()
        elif s3_key and self.user:
            try:
                filename, data = uploads.fetch(self.user.id, s3_key)
            except uploads.UploadError as exc:
                raise forms.ValidationError(str(exc)) from exc

        try:
            text, method = resolve_cv_text(cleaned.get("cv_source_text", ""), filename, data)
        except ExtractionError as exc:
            raise forms.ValidationError(str(exc)) from exc

        cleaned["cv_text"] = text
        cleaned["extraction_method"] = method
        cleaned["cv_hash"] = cv_hash(text)
        return cleaned

    def save(self, commit=True):
        job = super().save(commit=False)
        job.cv_text = self.cleaned_data["cv_text"]
        job.extraction_method = self.cleaned_data["extraction_method"]
        job.cv_hash = self.cleaned_data["cv_hash"]
        if self.cleaned_data.get("cv_file"):
            job.cv_upload = self.cleaned_data["cv_file"]
        if commit:
            job.save()
        return job


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True, label="Email")

    class Meta:
        model = User
        fields = ["username", "email"]
        labels = {"username": "Usuario"}

    def clean_email(self):
        email = self.cleaned_data["email"].lower().strip()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya hay una cuenta con ese email.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class FinalCvForm(forms.Form):
    """Editor del JSON final. Re-renderiza sin gastar una sola llamada al LLM."""

    payload = forms.CharField(widget=forms.Textarea(attrs={"rows": 26, "spellcheck": "false"}))

    def clean_payload(self):
        import json

        try:
            data = json.loads(self.cleaned_data["payload"])
        except ValueError as exc:
            raise forms.ValidationError(f"JSON inválido: {exc}") from exc
        if not isinstance(data, dict):
            raise forms.ValidationError("El JSON de raíz tiene que ser un objeto.")
        if not data.get("contact"):
            raise forms.ValidationError('Falta la clave "contact".')
        return data
