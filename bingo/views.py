from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, JsonResponse
from django.db import transaction
from .models import BingoCard, Game, SerialKey, VerificationLog
import random
import json
import hashlib
from django.utils import timezone
from django.contrib import messages
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import get_language, activate
from django.http import HttpResponseRedirect
from django.utils.translation import check_for_language
from django.urls import translate_url
from django.utils.translation import activate, check_for_language
from django.conf import settings


from django.views.decorators.http import require_GET

@require_GET
def card_status(request):
    """
    GET /bingo/api/card-status/?card_id=003
    -> {exists: true/false, used: true/false}
    """
    cid = (request.GET.get("card_id") or "").strip()
    try:
        card = BingoCard.objects.get(card_id=cid)
        return JsonResponse({"exists": True, "used": bool(card.used)})
    except BingoCard.DoesNotExist:
        return JsonResponse({"exists": False, "used": False})

@require_GET
def available_cards(request):
    """
    Returns all unused cards as a list of 3-digit strings, sorted.
    GET /bingo/api/available-cards/
    -> {"cards": ["001","002","003", ...]}
    """
    ids = list(
        BingoCard.objects.filter(used=False)
        .order_by("card_id")
        .values_list("card_id", flat=True)
    )
    return JsonResponse({"cards": ids})

def index(request):
    """
    Home page view for the Bingo app.
    """
    return render(request, "bingo/index.html")

@transaction.atomic
def generate_card(request):
    """
    Generate bingo cards gated by a SerialKey quota.
    Now supports both standalone mode (enter serial key) and cashier mode (use session).

    Flow:
    - GET: render form (serial_key + count, or just count if cashier logged in)
    - POST: validate serial_key (from form or session), enforce package/quota/expiry, generate up to allowed count
    """
    # Check if this is a cashier session
    is_cashier = request.session.get('cashier_logged_in', False)
    session_serial_key = request.session.get('cashier_serial_key') if is_cashier else None
    
    if request.method == "GET":
        context = {
            'is_cashier': is_cashier,
            'session_serial_key': session_serial_key,
        }
        return render(request, "bingo/generate_card.html", context)

    # POST
    if is_cashier and session_serial_key:
        # Use session serial key for cashier
        serial_key_str = session_serial_key
    else:
        # Use form input for standalone mode
        serial_key_str = (request.POST.get("serial_key") or "").strip()
    
    count_str = (request.POST.get("count") or "").strip()

    # Basic parse for count
    try:
        requested = max(1, int(count_str))
    except (TypeError, ValueError):
        requested = 1

    # Lock the serial key row to avoid race conditions on quota
    sk = SerialKey.objects.select_for_update().select_related("package").filter(key=serial_key_str).first()
    if not sk:
        context = {"error": _("Invalid serial key."), "prefill_key": serial_key_str if not is_cashier else None, "is_cashier": is_cashier, "session_serial_key": session_serial_key}
        return render(request, "bingo/generate_card.html", context)

    # First use: auto-activate if not yet activated
    if not sk.activated:
        sk.activated = True

    # Validate key by package type
    if sk.package.package_type == "fixed":
        remaining = sk.remaining_cards or 0
        if remaining <= 0:
            context = {"error": _("This serial key has no remaining card quota."), "prefill_key": serial_key_str if not is_cashier else None, "is_cashier": is_cashier, "session_serial_key": session_serial_key}
            return render(request, "bingo/generate_card.html", context)
        allowed = min(requested, remaining)
    else:
        # unlimited: must be within time window
        if not sk.is_valid_now():
            context = {"error": _("This serial key is expired or not valid at the moment."), "prefill_key": serial_key_str if not is_cashier else None, "is_cashier": is_cashier, "session_serial_key": session_serial_key}
            return render(request, "bingo/generate_card.html", context)
        allowed = requested  # no numeric cap while valid

    # Generate `allowed` cards
    cards_data = []
    for _ in range(allowed):
        numbers = list(range(1, 26))
        random.shuffle(numbers)
        numbers_str = ",".join(str(num) for num in numbers)

        # Retry a few times if we collide on unique constraints
        for _attempt in range(5):
            try:
                with transaction.atomic():
                    last_card = BingoCard.objects.select_for_update().order_by("created_at").last()
                    if last_card:
                        last_id = int(last_card.card_id)
                        new_id_int = last_id + 1 if last_id < 999 else 1
                    else:
                        new_id_int = 1
                    new_id = f"{new_id_int:03d}"
                    card = BingoCard.objects.create(card_id=new_id, numbers=numbers_str)
                break
            except Exception:
                if _attempt == 4:
                    raise

        nums = card.numbers.split(",")
        grid = [nums[i:i+5] for i in range(0, 25, 5)]
        cards_data.append({"card": card, "grid": grid})

    # Update usage counters
    if sk.package.package_type == "fixed":
        sk.generated_cards = (sk.generated_cards or 0) + allowed
    # Persist any activation
    sk.save(update_fields=["activated", "generated_cards"] if sk.package.package_type == "fixed" else ["activated"])

    # Build context
    info_msg = None
    if allowed < requested:
        info_msg = _("Generated %(allowed)d of %(requested)d requested card(s) due to serial key quota.") % {
            'allowed': allowed,
            'requested': requested
        }

    context = {
        "cards": cards_data,
        "serial_key": serial_key_str,
        "generated_count": allowed,
        "requested_count": requested,
        "info": info_msg,
        "remaining_after": (sk.remaining_cards if sk.package.package_type == "fixed" else None),
        "package_type": sk.package.package_type,
        "valid_until": sk.valid_until,
        "is_cashier": is_cashier,
        "session_serial_key": session_serial_key,
    }
    return render(request, "bingo/generate_card.html", context)

def _compute_winning_lines(grid, called_set):
    """
    grid: 5x5 list of ints
    called_set: set[int]
    returns dict with rows, cols, diagonals, and exact cells to highlight
    """
    winning = {"rows": [], "cols": [], "diagonals": [], "cells": []}

    # rows
    for r in range(5):
        if all(grid[r][c] in called_set for c in range(5)):
            winning["rows"].append(r)
            winning["cells"].extend([[r, c] for c in range(5)])

    # cols
    for c in range(5):
        if all(grid[r][c] in called_set for r in range(5)):
            winning["cols"].append(c)
            winning["cells"].extend([[r, c] for r in range(5)])

    # diagonals
    main_diag = all(grid[i][i] in called_set for i in range(5))
    anti_diag = all(grid[i][4 - i] in called_set for i in range(5))
    if main_diag:
        winning["diagonals"].append("main")
        winning["cells"].extend([[i, i] for i in range(5)])
    if anti_diag:
        winning["diagonals"].append("anti")
        winning["cells"].extend([[i, 4 - i] for i in range(5)])

    return winning

@transaction.atomic
def verify_card(request):
    """
    Verify a card against the *current* called numbers snapshot.
    - Requires `card_id` and `called_numbers` (comma-separated) in POST.
    - Optionally accepts `numbers_full` (JSON array of the full 1..25 shuffled sequence)
      to fingerprint the round and assign a speed-based rank (1st, 2nd, ...).
    - Logs every attempt to VerificationLog (winner or not).
    - Marks the card `used=True` only on the first successful verification.
    - Returns winning rows/cols/diagonals + exact cell coordinates for UI highlighting.
    """
    if request.method != "POST":
        return render(request, "bingo/verify_card.html")

    card_id = (request.POST.get("card_id") or "").strip()
    called_numbers_str = (request.POST.get("called_numbers") or "").strip()

    # Parse called numbers safely
    called_numbers = []
    if called_numbers_str:
        try:
            called_numbers = [int(x) for x in called_numbers_str.split(",") if x.strip()]
        except ValueError:
            called_numbers = []
    called_set = set(called_numbers)

    # Round fingerprint: full shuffled sequence from the client (JSON array). This groups claims.
    numbers_full_str = (request.POST.get("numbers_full") or "").strip()
    try:
        import json, hashlib  # local import to keep this function drop-in
        numbers_full = json.loads(numbers_full_str) if numbers_full_str else []
    except Exception:
        numbers_full = []

    # Compute a stable round hash (falls back to snapshot if full sequence missing).
    if numbers_full and len(numbers_full) == 25 and all(isinstance(n, int) for n in numbers_full):
        seq_str = ",".join(str(n) for n in numbers_full)
    else:
        # Fallback: less ideal, but keeps things working if client didn't send numbers_full.
        seq_str = "prefix:" + ",".join(str(n) for n in called_numbers)
    round_hash = hashlib.sha1(seq_str.encode("utf-8")).hexdigest()

    # Claim index = how many numbers had been called at claim time (lower = faster)
    claim_index = len(called_numbers)

    # Get card
    # Enforce that this card is part of the submitted round's allowed list (if provided)
    allowed_cards_raw = (request.POST.get("allowed_cards") or "").strip()
    allowed_cards = []
    if allowed_cards_raw:
        try:
            allowed_cards = json.loads(allowed_cards_raw)
        except Exception:
            allowed_cards = []

    if allowed_cards and card_id not in allowed_cards:
        message = _("This card is not registered for the current round.")
        payload = {"message": str(message), "card_id": card_id, "win": False}
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse(payload, status=400)
        return render(request, "bingo/verify_card.html", {"message": message, "win": False})

    # Get card
    card = get_object_or_404(BingoCard, card_id=card_id)

    # Parse 5x5 grid from stored layout
    try:
        nums = [int(n) for n in card.numbers.split(",")]
    except ValueError:
        nums = []
    if len(nums) != 25:
        message = _("Invalid card data.")
        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"message": str(message), "card_id": card.card_id, "win": False}, status=400)
        return render(request, "bingo/verify_card.html", {"card": card, "message": message, "win": False})

    grid = [nums[i:i+5] for i in range(0, 25, 5)]

    # Compute winning lines
    winning = _compute_winning_lines(grid, called_set)
    win = bool(winning["rows"] or winning["cols"] or winning["diagonals"])

    # Compute rank (1st, 2nd, ...) for this round on first success
    assigned_rank = None
    if win:
        existing_win = VerificationLog.objects.filter(
            card=card, round_hash=round_hash, is_winner=True
        ).order_by("created_at").first()
        if existing_win:
            assigned_rank = existing_win.assigned_rank
        else:
            # Best-effort atomicity using transaction; good enough for typical loads
            current_winners = VerificationLog.objects.filter(
                round_hash=round_hash, is_winner=True
            ).count()
            assigned_rank = current_winners + 1

    # Log attempt (audit trail)
    VerificationLog.objects.create(
        card=card,
        called_numbers=called_numbers,
        winning_lines=winning,
        is_winner=win,
        round_hash=round_hash,
        claim_index=claim_index,
        assigned_rank=assigned_rank,
    )

    # Mark used on first successful verification
    already_used_before = card.used
    if win and not card.used:
        card.used = True
        card.save(update_fields=["used"])

    message = _("Card Verified as Winner!") if win else _("Card is not a winning card.")

    payload = {
    "message": str(message),
    "card_id": card.card_id,
    "win": win,
    "winning_lines": winning,            # rows/cols/diagonals + cells
    "already_used": already_used_before and win,  # true only if redeemed before
    "rank": assigned_rank,               # 1 = first, 2 = second, ...
    "card_grid": grid,                   # 5x5 integers for UI highlighting
    }

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(payload)

    # Non-AJAX fallback (template needs `win`; include rank if you want to render it)
    return render(
        request,
        "bingo/verify_card.html",
        {"card": card, "message": message, "win": win, "rank": assigned_rank},
    )

def play_game(request):
    """
    Start a new client-driven round:
    - Shuffle numbers 1..25 and pass them to the template.
    - The client will send back both the *called snapshot* and the *full shuffled sequence*
      (`numbers_full`) with the verify request so the server can fingerprint the round and rank winners.
    """
    numbers = list(range(1, 26))
    random.shuffle(numbers)

    from django.utils.translation import get_language
    print(f"Current language: {get_language()}")
    print(f"Session language: {request.session.get('django_language', 'not set')}")

    context = {"numbers": numbers}
    return render(request, "bingo/play.html", context)

def cashier_login(request):
    """
    Cashier login - they enter their serial key to start a session.
    """
    if request.method == "GET":
        return render(request, "bingo/cashier_login.html")
    
    # POST
    serial_key_str = (request.POST.get("serial_key") or "").strip()
    
    if not serial_key_str:
        messages.error(request, _("Please enter a serial key."))
        return render(request, "bingo/cashier_login.html")
    
    # Validate the serial key exists and is usable
    sk = SerialKey.objects.select_related("package").filter(key=serial_key_str).first()
    if not sk:
        messages.error(request, _("Invalid serial key."))
        return render(request, "bingo/cashier_login.html")
    
    # Auto-activate if not yet activated
    if not sk.activated:
        sk.activated = True
        sk.save(update_fields=["activated"])
    
    # Check if key is still valid
    if sk.package.package_type == "fixed":
        remaining = sk.remaining_cards or 0
        if remaining <= 0:
            messages.error(request, _("This serial key has no remaining card quota."))
            return render(request, "bingo/cashier_login.html")
    else:
        # unlimited: must be within time window
        if not sk.is_valid_now():
            messages.error(request, _("This serial key is expired or not valid."))
            return render(request, "bingo/cashier_login.html")
    
    # Store in session
    request.session['cashier_serial_key'] = serial_key_str
    request.session['cashier_logged_in'] = True
    
    messages.success(request, _("Successfully logged in with serial key: %(serial_key)s") % {'serial_key': serial_key_str})
    return redirect('bingo:cashier_dashboard')

def cashier_logout(request):
    """
    Clear the cashier session.
    """
    request.session.pop('cashier_serial_key', None)
    request.session.pop('cashier_logged_in', None)
    messages.success(request, _("Successfully logged out."))
    return redirect('bingo:cashier_login')


def cashier_dashboard(request):
    """
    Dashboard showing current serial key status and quick actions.
    Now supports universal expiration for all key types.
    """
    if not request.session.get('cashier_logged_in'):
        messages.error(request, _("Please log in first."))
        return redirect('bingo:cashier_login')
    
    serial_key_str = request.session.get('cashier_serial_key')
    if not serial_key_str:
        messages.error(request, _("Session expired. Please log in again."))
        return redirect('bingo:cashier_login')
    
    # Get current serial key status
    sk = SerialKey.objects.select_related("package").filter(key=serial_key_str).first()
    if not sk:
        messages.error(request, _("Serial key no longer exists. Please log in again."))
        request.session.pop('cashier_serial_key', None)
        request.session.pop('cashier_logged_in', None)
        return redirect('bingo:cashier_login')
    
    # MODIFIED: Re-check validity using the new, smarter is_valid_now() method.
    is_valid = sk.is_valid_now()
    if not is_valid:
        messages.warning(request, _("Your serial key is no longer valid (expired or quota exhausted)."))
    
    # MODIFIED: Calculate time remaining for ALL packages, since all have an expiration date.
    time_remaining = None
    now = timezone.now()
    if sk.valid_until > now:
        time_remaining = sk.valid_until - now
    
    # MODIFIED: The context is now simpler and more consistent.
    context = {
        'serial_key': sk,
        'package': sk.package,
        'remaining_cards': sk.remaining_cards,
        'is_valid': is_valid,
        'time_remaining': time_remaining,
    }
    return render(request, "bingo/cashier_dashboard.html", context)

def set_language(request):
    """
    Set the user's language and redirect back to the same page.
    """
    language = request.POST.get('language') or request.GET.get('language')
    next_url = request.POST.get('next') or request.GET.get('next') or '/'
    
    if language and language in dict(settings.LANGUAGES):
        # Set the language in session
        request.session[settings.LANGUAGE_COOKIE_NAME] = language
        request.session.modified = True
        
        # Activate the language for this request
        activate(language)
        
        # Parse the current URL to replace the language code
        from django.urls import resolve, reverse
        from django.urls.exceptions import Resolver404
        
        # Remove the current language prefix from the URL
        path = next_url
        for lang_code, _ in settings.LANGUAGES:
            if path.startswith(f'/{lang_code}/'):
                path = path[len(f'/{lang_code}'):]
                break
        
        # Add the new language prefix
        if not path.startswith('/'):
            path = '/' + path
        new_url = f'/{language}{path}'
        
        return redirect(new_url)
    
    return redirect(next_url)




