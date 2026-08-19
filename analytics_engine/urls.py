from django.contrib.auth import views as auth_views
from django.urls import path

from analytics_engine import views


app_name = "dashboard"


urlpatterns = [
    path(
        "healthz/",
        views.healthz,
        name="healthz",
    ),

    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),

    path(
        "logout/",
        auth_views.LogoutView.as_view(),
        name="logout",
    ),

    path(
        "",
        views.overview,
        name="overview",
    ),

    path(
        "website/",
        views.website_dashboard,
        name="website",
    ),

    path(
        "mailerlite/",
        views.mailerlite_dashboard,
        name="mailerlite",
    ),

    path(
        "search-console/",
        views.search_console,
        name="search_console",
    ),

    path(
        "system-health/",
        views.system_health,
        name="system_health",
    ),

    path(
        "weekly-reports/",
        views.weekly_reports,
        name="weekly_reports",
    ),
]