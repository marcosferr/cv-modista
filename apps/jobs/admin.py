from django.contrib import admin

from apps.jobs.models import Artifact, Job
from apps.llm.models import CvParse, LlmCall


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("target_role", "user", "status", "pages", "created_at")
    list_filter = ("status", "language")
    search_fields = ("target_role", "user__username")
    readonly_fields = ("id", "created_at", "updated_at", "cv_hash")


@admin.register(LlmCall)
class LlmCallAdmin(admin.ModelAdmin):
    list_display = ("purpose", "model_id", "finish_reason", "completion_tokens",
                    "latency_ms", "created_at")
    list_filter = ("purpose", "model_id")


admin.site.register([Artifact, CvParse])
