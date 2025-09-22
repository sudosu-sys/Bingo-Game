"""
URL configuration for src project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf.urls.i18n import i18n_patterns

urlpatterns = [
    path("admin/", admin.site.urls),

    # Expose Django's i18n utilities (includes the built-in set_language if you ever want it)
    path("i18n/", include("django.conf.urls.i18n")),
]

# All app URLs live behind a language prefix (/en/, /am/) so the active language is explicit in the URL
urlpatterns += i18n_patterns(
    path("", include(("bingo.urls", "bingo"), namespace="bingo")),
)
