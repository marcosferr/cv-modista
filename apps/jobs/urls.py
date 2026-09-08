from django.contrib.auth import views as auth_views
from django.urls import path

from apps.jobs import views

urlpatterns = [
    path("", views.job_create, name="job_create"),
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/<uuid:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<uuid:pk>/status/", views.job_status, name="job_status"),
    path("jobs/<uuid:pk>/review/", views.job_review, name="job_review"),
    path("jobs/<uuid:pk>/retry/", views.job_retry, name="job_retry"),
    path("jobs/<uuid:pk>/download/<str:kind>/", views.job_download, name="job_download"),
    path("uploads/sign/", views.presign_upload, name="presign_upload"),
    path("cuenta/registro/", views.signup, name="signup"),
    path("cuenta/entrar/", auth_views.LoginView.as_view(
        template_name="jobs/login.html", redirect_authenticated_user=True), name="login"),
    path("cuenta/salir/", auth_views.LogoutView.as_view(), name="logout"),
]
