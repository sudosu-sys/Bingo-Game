from django.urls import path, include
from . import views

app_name = "bingo"

urlpatterns = [
    path("", views.index, name="index"),
    path("generate/", views.generate_card, name="generate_card"),
    path("verify/", views.verify_card, name="verify_card"),
    path("play/", views.play_game, name="play_game"),
    
    # Cashier dashboard
    path("cashier/", views.cashier_login, name="cashier_login"),
    path("cashier/dashboard/", views.cashier_dashboard, name="cashier_dashboard"),
    path("cashier/logout/", views.cashier_logout, name="cashier_logout"),

    # Language switching
    path("set-language/", views.set_language, name="set_language"),

    # APIs
    path("api/card-status/", views.card_status, name="card_status"),
    path("api/available-cards/", views.available_cards, name="available_cards"),
]

