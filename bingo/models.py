from django.db import models
from django.utils import timezone

class BingoCard(models.Model):
    # Each bingo card is represented as a 5x5 grid with unique numbers 1 to 25.
    # The card_id is a unique 3-digit identifier that resets after 999.
    card_id = models.CharField(max_length=3, unique=True)
    # Store a random arrangement of numbers (e.g., as a comma-separated string).
    numbers = models.CharField(max_length=50, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)

    def __str__(self):
        return f"Card {self.card_id}"

class Package(models.Model):
    PACKAGE_TYPE_CHOICES = [
        ('fixed', 'Fixed Count'),        # e.g. 100 games for $20, 200 games for $30
        ('unlimited', 'Monthly Unlimited')  # e.g. unlimited games for 30 days at a flat fee
    ]
    name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=6, decimal_places=2)
    # For fixed packages, this is the number of games included. For unlimited, it can be null.
    game_count = models.IntegerField(null=True, blank=True)
    package_type = models.CharField(max_length=10, choices=PACKAGE_TYPE_CHOICES)

    def __str__(self):
        return self.name


class SerialKey(models.Model):
    # A serial key generated when a package is purchased.
    key = models.CharField(max_length=50, unique=True)
    package = models.ForeignKey(Package, on_delete=models.CASCADE)
    activated = models.BooleanField(default=False)
    # MODIFIED: valid_until is now required for ALL serial keys.
    valid_until = models.DateTimeField()

    # Track how many cards have been generated using this key (for fixed packages)
    generated_cards = models.IntegerField(default=0)

    def __str__(self):
        return f"SerialKey {self.key} - {self.package.package_type}"

    @property
    def remaining_cards(self):
        """For fixed packages, how many cards are left; for unlimited, returns None."""
        if self.package.package_type == 'fixed':
            total = self.package.game_count or 0
            return max(0, total - (self.generated_cards or 0))
        return None

    # MODIFIED: This method now enforces universal expiration.
    def is_valid_now(self):
        """
        A key is valid if AND ONLY IF:
        1. The current time is before its valid_until date.
        2. AND, if it's a 'fixed' package, it must also have cards remaining.
        """
        # Universal check: Has it expired?
        if timezone.now() > self.valid_until:
            return False

        # If it's an unlimited package and hasn't expired, it's valid.
        if self.package.package_type == 'unlimited':
            return True
        
        # If it's a fixed package and hasn't expired, it must also have cards left.
        if self.package.package_type == 'fixed':
            return (self.remaining_cards or 0) > 0

        # Default to False if something is misconfigured
        return False

class Game(models.Model):
    # A record of a single bingo game played using a bingo card.
    serial_key = models.ForeignKey(SerialKey, on_delete=models.CASCADE)
    bingo_card = models.ForeignKey(BingoCard, on_delete=models.CASCADE)
    # Each game has an associated wager (for example, $5 or $10).
    wager_amount = models.DecimalField(max_digits=6, decimal_places=2)
    played_at = models.DateTimeField(auto_now_add=True)
    is_winner = models.BooleanField(default=False)

    def __str__(self):
        return f"Game {self.id} - Card {self.bingo_card.card_id}"
    
class VerificationLog(models.Model):
    """
    Every verification attempt gets logged:
    - the snapshot of called numbers at that moment
    - which rows/cols/diagonals matched
    - whether it was a winner
    """
    card = models.ForeignKey(BingoCard, on_delete=models.CASCADE, related_name="verifications")
    called_numbers = models.JSONField()   # list[int] at verify time
    winning_lines = models.JSONField()    # dict: {"rows":[...], "cols":[...], "diagonals":[...], "cells":[[r,c],...]}
    is_winner = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
        # Ranking fields (no separate Round model needed)
    # round_hash groups all claims that belong to the SAME shuffled sequence of numbers for a game.
    round_hash = models.CharField(max_length=64, db_index=True, default="")
    # How many numbers had been called at claim time (lower = faster).
    claim_index = models.IntegerField(default=0)
    # Assigned finishing place for this round (1 = first, 2 = second, ...). Null if not a winner.
    assigned_rank = models.IntegerField(null=True, blank=True)


    def __str__(self):
        state = "WIN" if self.is_winner else "NO WIN"
        return f"{self.card.card_id} - {state} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
