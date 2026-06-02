"""Authentication helper — extracts player_id from the JWT-authenticated request."""


def get_player_id(request):
    """Return the player_id (GameAccount pk as string) for the current request."""
    if request.user and request.user.is_authenticated:
        return str(request.user.id)
    return None
